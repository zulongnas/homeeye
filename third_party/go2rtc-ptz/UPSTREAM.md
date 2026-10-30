# 上游升级与回归手册（go2rtc 小米补丁版）

本仓库是 [AlexxIT/go2rtc](https://github.com/AlexxIT/go2rtc) 的**补丁分叉**，不是 fork
出来的独立项目：只在小米 miss 协议那几处动了刀，其余与上游逐字节一致。

| 引用 | 含义 |
|---|---|
| `upstream-v1.9.15` | 上游 tag `v1.9.15` 的源码（去掉 `website/`，那是文档站，编译不需要） |
| `v1.9.15-ptz.1` | 打满 HomeEye 补丁的当前版本，**生产镜像 `homeeye/go2rtc:1.0.6-ptz` 的源码基线** |
| `PATCHES.md` | 补丁内容、为什么必须打、踩过的协议坑 |
| `homeeye-build-image.sh` | 从本仓库源码一路做到镜像，并自带冒烟回归 |

看补丁到底改了什么，用这条：

```sh
git diff --stat upstream-v1.9.15..v1.9.15-ptz.1
```

应当恰好是 13 个文件（`pkg/xiaomi/miss/*`、`internal/xiaomi/*`、`cmd/heptz/*`、
`PATCHES.md`、`.gitattributes`）。多出来的就是上游漂移或误改。

---

## 一、什么时候需要升级

只有两种情况：

1. 上游修了我们也在受影响的 bug（尤其 `pkg/xiaomi/` 与 `pkg/webrtc/`）；
2. 我们要上游的新功能（新的容器格式、新的硬件转码支持）。

**不建议为了"跟新"而跟。** 这套补丁的价值全在 P2P 会话与 channel0 读取那条链路上，
上游每次重构 `miss` 包都可能把我们的假设打断（例如 `Dial()` 的返回值、
`dataChannel.Push` 的缓冲槽数）。没有明确收益就别动现网。

## 二、升级步骤

```sh
# 1. 拉上游新源码（直连 git 在境内不稳，用 codeload 的 tarball）
VER=v1.9.16
curl -sL -o /tmp/up.tgz https://codeload.github.com/AlexxIT/go2rtc/tar.gz/refs/tags/$VER
sha256sum /tmp/up.tgz            # 记进 commit message，可追溯
mkdir -p /tmp/up && tar xzf /tmp/up.tgz -C /tmp/up && cd /tmp/up/go2rtc-*/
rm -rf website

# 2. 新建基线分支与提交
cd <本仓库>
git checkout -b upgrade-$VER upstream-v1.9.15
find . -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
cp -a /tmp/up/go2rtc-$VER/. .
git add -A && git commit -m "upstream: go2rtc $VER（tarball sha256=...）"
git tag upstream-$VER

# 3. 把补丁重放到新基线
git checkout -b ptz-$VER
git cherry-pick v1.9.15-ptz.1      # 只有那一个补丁 commit，冲突集中且明确
```

### cherry-pick 冲突的判读顺序

按这个顺序处理，别一把梭 `--theirs`：

| 文件 | 冲突意味着 | 处理 |
|---|---|---|
| `pkg/xiaomi/miss/cs2/conn.go` | 上游改了 P2P 包封装。**必须逐行看**，`WritePacket` 的 payload 拷贝是我们的核心修复 | 保留上游结构，把 `copy(req[offset+hdrSize:], payload)`、`ReadCommandRaw()`、`pktMu` 三处重新贴上去 |
| `pkg/xiaomi/miss/producer.go` | `Dial()` 流程变了 | 只要会话建立成功点还在，就在同一点重新调用 `StartCommandReader()` |
| `pkg/xiaomi/miss/client.go` | Client 字段/生命周期变了 | 重新挂 `cr *cmdReader`，注意 close 时要停掉读取协程 |
| `internal/xiaomi/xiaomi.go` | 路由注册方式变了 | 重新注册那 4 个 `/api/xiaomi/*` |
| `cmd/heptz/*`、`pkg/xiaomi/miss/{cmdreader,registry,motor,speaker}.go`、`internal/xiaomi/ptz.go` | 全是新增文件，正常不冲突 | 直接保留 |

如果上游把 `miss` 包整体重命名或重写，**停下来先读上游 diff**，别硬贴补丁。

## 三、升级后必须跑的回归（顺序不能换）

### 1. 编译 + 冒烟（不需要真实摄像头，脚本自带）

```sh
sh homeeye-build-image.sh            # TAG=homeeye/go2rtc:1.0.7-ptz 换新版标签
```

脚本第 4 步会拿独立端口起一个临时容器，检查 `/api/xiaomi/ptz` 存在、
`/api/xiaomi/motor` 不是 404、`/api/streams` 有响应。任一条不过就 exit 1。
**这一步同时防住"补丁没进去"和"冒烟容器撞了现网 1984"两件事。**

### 2. 会话与通道（要真实摄像头，客厅那台常插电，用它在 NAS 上测）

- **取流**：`GET /api/streams` 里这一路 `source` 能起来，`/api/stream.mp4?src=...` 出画面；
- **云台角度回传**：`GET /api/xiaomi/motor?did=<did>&op=1` 后 `GET /api/xiaomi/ptz`
  里 `angle`/`elevation` 要变。**取不到角度 = channel0 没人读或解密后命令号偏移错了**
  （见 PATCHES.md「应答帧的布局」）；
- **不断连**：连发 **15 条** motor 命令（>10 个槽），中途不能出现 `pop buffer is full`
  或 broken pipe。原版就是在这里断的，这条是补丁的立身之本；
- **对讲有声**：`POST /api/xiaomi/speaker?did=<did>` 开喇叭，`POST /api/xiaomi/audio`
  送 **PCM16 LE 8kHz 单声道**，摄像头要播人声。又尖又快 = 当成 A-law 切帧的编码问题；
- **会话名额**：一台摄像头同时最多 3 条 P2P 会话。HomeEye 的看门狗巡检间隔下限因此
  锁在 15 秒。测云台/对讲时不要另开 `cmd/heptz` 侧车，两个读取协程会抢同一条 channel0。

### 3. 与 HomeEye 的联动（1.1.1 起）

- `PUT /api/streams?name=&src=` 纠源可用且写后复核 —— HomeEye 的 **IP 漂移自愈**靠它。
  手动把某条流的地址改成错的，再在 HomeEye 设置页点「手动修复全部异常」，
  应当看到流地址被改回且其它流没被动；
- `api.listen: 127.0.0.1:1984` + `api.username/password` 生效，HomeEye 带
  `ptz.g2r_user/g2r_pass` 能调通，不带凭据要 401；
- HomeEye 设置页「链路健康」没有把这两路判成 `broken`，`/api/health` 返回 200。

## 四、发布与回滚

```sh
# 发布：改 compose 的 image 再重建容器（二进制在镜像里，不能运行中覆盖）
docker compose up -d --force-recreate go2rtc
# 回滚：image 改回上一个 tag，同一条命令再来一次
```

镜像标签与 git tag 一一对应，命名 `v<上游>-ptz.<序号>`，镜像 `homeeye/go2rtc:<序号对应>-ptz`。
现网对应关系：

| 镜像 | 源码 tag |
|---|---|
| `homeeye/go2rtc:1.0.6-ptz` | `v1.9.15-ptz.1` |

复现性已验证（NAS，2026-10-29）：`STRIP=0 sh homeeye-build-image.sh` 从本仓库工作树编出的
二进制 sha256 = `d7cc80059062fd6fdba7a1a8d744df58968e8895de4e27384ed2225e3bc547a8`，
与现网镜像里 `/usr/local/bin/go2rtc` 完全一致，叠层出的镜像 ID 也与现网
`b67dd5cf9949` 相同 —— 也就是说现网跑的那份，确实就是这份源码。
以后升级重打时，只要拿这两个值当基线比对，就不会出现"镜像和源码不是一回事"。

## 五、别做的事

- 不要 `git push` 到上游，也不要给上游提 PR —— 我们改的 `channel0` 读取与
  `WritePacket` 是小米私有协议的行为修正，上游没有对应测试，合并风险由我们承担；
- 不要把 `website/` 捡回来，也不要提交编译产物（`go2rtc-new`、`/tmp/g2img` 那些）；
- 不要在运行中的容器里覆盖 `/usr/local/bin/go2rtc`（`Text file busy`，而且会让
  镜像与实际跑的字节不一致 —— 出问题查不到是哪份代码）。
