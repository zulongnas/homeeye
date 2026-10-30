# HomeEye

自建 NVR：把摄像头画面留在自己的 NAS 上。

后端只用 **Python 标准库**（不装数据库、不要消息队列、没有 npm 构建），
取流/转封装/切片录像交给系统自带的 **ffmpeg**，画面**不上传任何第三方服务器**。
一台 NAS + 几条摄像头，换掉几百块的云存储订阅。

```
摄像头 ──RTSP/ONVIF/小米云──▶ HomeEye ──▶ 浏览器直播（MPEG-TS，秒开）
                                 │
                                 └──▶ 分段 MP4 录像 + 时间轴回放
```

- 许可：**MIT**（第三方组件见 [NOTICE.md](NOTICE.md)）
- 平台：Linux（NAS）；飞牛 fnOS 有原生 `.fpk`，其它发行版走 Docker
- 不联网校验、无账号体系、无付费功能，代码全在这里

---

## 30 秒上手（Docker）

```sh
git clone https://github.com/zulongnas/homeeye.git && cd homeeye
cd docker
cp .env.example .env          # 只问一件事：配置和录像各放哪个目录（不改也能跑）
docker compose up -d          # 镜像从 ghcr.io 拉，公开免登录，不用先构建
# 浏览器打开 http://<这台机器的IP>:8088
```

**飞牛 fnOS 不想碰命令行的**：到 [Releases](https://github.com/zulongnas/homeeye/releases/latest)
下载 `homeeye.fpk`，应用中心 → 手动安装 → 选这个文件；安装向导会问你录像往哪儿写
（留空就用飞牛给本应用开的共享目录），装完和 Docker 一样是零配置状态。
（手动安装按飞牛官方的定位是「本地调试用途」，正式分发走应用中心上架，
对照表在 [docs/DEPLOY-FNOS.md](docs/DEPLOY-FNOS.md)。）

第一件事是**设登录密码**：装完是零配置，没有摄像头、没有账号、没有录像，
在设密码之前页面上任何数据接口都是空的（见下方「安全模型」）。
之后到 **设置 → 摄像头** 添加你的设备。

需要小米摄像头的话，SDK 不在本仓库里（许可原因），先跑一次
`sh tools/fetch-miloco-sdk.sh` 再构建。RTSP / ONVIF 用户不用管它。

详细步骤、硬件转码、反向代理、故障排查：[docs/DEPLOY.md](docs/DEPLOY.md)
飞牛应用中心安装：[docs/DEPLOY-FNOS.md](docs/DEPLOY-FNOS.md)

## 部署要求

| 项 | 要求 | 说明 |
|---|---|---|
| 系统 | Linux + Docker | 群晖/威联通/飞牛/普通 x86 主机都行 |
| CPU | 每路 1 核以内 | 源已是 H.264 时直接拷贝不转码；需要软转时才是这个量级。有核显走硬转可忽略 |
| 内存 | ≥ 1 GB | 后端本身很轻，开销主要在 ffmpeg 子进程 |
| 磁盘 | 每路每天约 4–8 GB（1080p/10fps/H.264） | 录原始流（`storage.source=RAW`）同画质约省一半 |
| Python | 3.12+（仅裸跑不装 Docker 时） | Docker 方式不需要你在宿主机装 Python |
| ffmpeg | 4.x+ | Docker 镜像里已带 |
| 端口 | `8088` HTTP、`8443` HTTPS | 对讲另需 go2rtc 的 `1984/8554/8555`（默认只绑回环） |
| 网络 | 建议 host 网络 | 小米 P2P 打洞与 ONVIF 组播发现穿不过 NAT，见下 |

**为什么默认 host 网络**：小米取流走 P2P(PPCS)，依赖 UDP 广播/组播与打洞，
Docker 默认 bridge 的 NAT 会把打洞打歪 —— 表现为 `PPCS_Connect errorcode: -3`，
直播接口 HTTP 200 但 0 字节。ONVIF 局域网发现同理（组播收不到回应）。
macOS/Windows 的 Docker Desktop 没有可用 host 网络，那种环境用
`docker/docker-compose.bridge.yml`，代价是上面两项功能不可用。

### 运行环境由谁装（Docker / fpk / 裸跑 不一样）

| 依赖 | Docker | fpk（飞牛） | 裸跑 |
|---|---|---|---|
| Python | 镜像自带 3.12 | **宿主机的 python3.9+**（不附带，缺了服务起不来） | 自己装 3.9+ |
| ffmpeg | 镜像自带 | **宿主机自带**（飞牛系统里有 4.x；缺了就没画面） | 自己装 4.x+ |
| RTSP / ONVIF | 开箱可用 | 开箱可用 | 开箱可用 |
| 小米 SDK | 用户自取 + `HE_WITH_MILOCO_SDK=1` 重建镜像 | 用户自取 SDK，**Python 依赖要自己在宿主机装** | 同 fpk |
| 小米 SDK 的 Python 依赖 | 重建时自动装（`docker/requirements-xiaomi.txt`） | 不装，需手动 pip | 手动 pip |

小米那一路的依赖（fpk / 裸跑在宿主机执行一次，Docker 不用）：

```sh
pip3 install aiohttp aiofiles requests cryptography av numpy \
             pydantic PyYAML psutil qrcode Pillow
```

**装完怎么确认**：登录后进 **设置 → 链路检查**，顶部会有一行「运行环境」自检，
缺什么直接写在那里（缺失的、需要你自己装的都点名）；服务日志里也有
`[env]` 开头的几行。Docker 用户不用管这一节 —— 除了小米 SDK，镜像是自包含的。

## 功能

| 能力 | 状态 | 备注 |
|---|---|---|
| 多分屏实时画面（1/2/4/6/9） | ✅ | 单击放大、双击全屏 |
| 双码流（主 / 子） | ✅ | 多分屏小窗口自动走子码流，省带宽也省 CPU；单屏仍取主码流 |
| 按需转码 | ✅ | `live.transcode = auto/always/never`；源已 H.264 就 `-c:v copy` |
| 连续录像 + 按日时间轴回放 | ✅ | 分段 MP4，断电重启不会出现索引与文件对不上 |
| 事件时间轴 | ✅ | 分段写完后本地离线侦测动静，时间轴上标事件块，点一下跳到那一刻 |
| 录像源可选 PLAYED / RAW | ✅ | 录原始流省一半体积 |
| 保留天数 / 容量上限自动清理 | ✅ | 两个都填时**同时生效**，任一命中就删；被事件引用的分段留到最后 |
| 移动侦测 | ✅ | 本机离线侦测（只解关键帧，不与录像/直播抢资源）+ 摄像头自报事件 |
| 断网空洞由 SD 卡回补 | ✅ | 摄像头侧兜底，回补后与本地分段合并 |
| 看门狗：断链重建 / IP 漂移自愈 / 退避 | ✅ | 摄像头被拔电只记流水、不告警、不让容器变红 |
| 云台（PTZ） | ⚠️ 机型相关 | 小米走 `miss` 私有命令；ONVIF 机型走标准 Profile T |
| 语音对讲 | ⚠️ 机型相关 | **必须 HTTPS**（浏览器麦克风的规矩，不是本项目的限制） |
| 通知：邮件 / 企业微信 / 钉钉 / Bark / Webhook | ✅ | 多渠道可同时开；支持布防时段与每日日报；未配置就只记日志，不会静默失败 |
| 监控接入 `GET /api/metrics` | ✅ | Prometheus 文本格式 0.0.4，可选 `HE_METRICS_TOKEN` 只读令牌 |
| 米家扫码授权批量加摄像头 | ⚠️ 需自取 SDK | 见 NOTICE.md 第三节 |

实测过的机型：创米 `chuangmi.camera.039a01`、`chuangmi.camera.069a01`、
`madv.cateye.miowlv2`（都是小米生态），以及若干标准 RTSP 摄像头。
**没在这三份样本上验证过的机型不保证可用**，尤其是云台与对讲 ——
它们的差异全在 `miss` 私有命令的实现细节里。

## 监控接入（Prometheus / Grafana）

`GET /api/metrics` 输出 Prometheus 文本格式（`text/plain; version=0.0.4`）。
抓取器不是浏览器，拿不到登录会话，所以给它发一个只读令牌：

```sh
# docker/.env
HE_METRICS_TOKEN=<openssl rand -hex 24 的输出>
# 改完 docker compose up -d
curl -s "http://127.0.0.1:8088/api/metrics?token=$HE_METRICS_TOKEN" | head
```

`prometheus.yml`：

```yaml
scrape_configs:
  - job_name: homeeye
    scrape_interval: 30s
    metrics_path: /api/metrics
    static_configs:
      - targets: ["127.0.0.1:8088"]      # NAS 上直连；跨机就写 NAS 的地址
    # 令牌也可以走 Authorization 头，二选一
    # authorization:
    #   credentials: <HE_METRICS_TOKEN>
```

指标族：`homeeye_up` / `homeeye_info` / `homeeye_uptime_seconds` /
`homeeye_process_rss_bytes`、每路 `homeeye_camera_up` / `homeeye_camera_state` /
`homeeye_recording_up` / `homeeye_camera_segment_age_seconds` /
`homeeye_events_today`、`homeeye_live_frames_total` / `homeeye_live_subscribers`、
`homeeye_storage_ok` / `_free_bytes` / `_disk_total_bytes` / `_budget_bytes`、
`homeeye_recordings_bytes`、`homeeye_scanner_beat_age_seconds` /
`homeeye_scanner_pending`、`homeeye_watchdog_beat_age_seconds` /
`homeeye_watchdog_loops_total` / `homeeye_watchdog_actions_total{action}`。

- 没设 `HE_METRICS_TOKEN` 时，这个接口和其它 API 一样要登录会话 —— **默认不多开一个口子**。
- 令牌只放行 `/api/metrics` 这一个路径，别拿它当登录口令用。
- 每次抓取读的是内存里的状态快照，不会触发链路探测；整段结果缓存 5 秒，
  多个抓取器不会把 NAS 的目录遍历放大成负载。
- 不想让局域网随便抓：令牌本身就是门槛；再严一点就在反向代理上按来源 IP 限制。

## 安全模型

装完到设密码之间是**认领窗口**：此时只放行"设密码"这一个接口，
其它数据接口（配置、录像、直播流）全部要登录态。这不是"默认无密码"，
而是"默认谁都不能看，除了设密码这条路"。

- 密码只存 PBKDF2-SHA256（12 万轮）+ 随机盐，**明文不落盘**；备份配置拿不到口令。
- 会话是内存里的随机 token，Cookie 带 `HttpOnly + SameSite=Strict`（顺带挡掉 CSRF）。
- 登录失败按 `IP+用户名` 限流：15 分钟内 8 次，命中后冷却 30 秒。
- **8088 上口令是明文穿过局域网的**，所以设完密码请改用 `https://…:8443`
  （自签证书，点一次"继续访问"）。要给外人访问，前面放带真证书的反向代理，
  别把 8088 直接透到公网。
- go2rtc 的 API 默认只绑 `127.0.0.1` **并且**要开 BasicAuth：
  `local_auth: false` 时"只绑回环"等于没鉴权，同机任何进程都能读到米家 passToken、
  直接调你的云台。详见 [docs/SECURITY.md](docs/SECURITY.md)。

## 对讲为什么要 HTTPS

浏览器判的是**页面来源**是不是安全上下文，不是声音用什么方式传出去。
所以"把对讲换成 WebRTC"并不能免掉这一条。三个通用入口，按你的场景挑：

1. `http://127.0.0.1:8088` —— 在 NAS 本机浏览器上直接用，localhost 算安全上下文
2. `https://<内网IP>:8443` —— 内置自签证书，点一次"继续访问"，局域网日常够用
3. 真证书域名 + 反向代理 —— 要给外人看、要上架分发，只有这条是正路

设置页的「对讲」卡片会把当前页面实际走的是哪条、以及本次采集用的是
AudioWorklet 还是退化的 ScriptProcessor **如实显示出来**，不让你猜。

## 设置项诚实化

这个项目的界面有一条硬规矩：**页面显示的值 = 磁盘上的值 = 实际生效的值**。
没实现的功能不藏开关 —— 设置页里那些"未生效"的条目会明确标注为什么没生效
（例如移动侦测的 `sensitivity` 全局值：摄像头只有低/高两档，1~100 无处映射）。
范围校验放在落盘前，钳过的值就是跑的值。

## 仓库结构

```
app/server/     Python 标准库后端（nw_*.py，一个功能一个模块）
app/www/        前端：ES5 单页，无框架、无构建步骤
app/bin/heptz   云台侧车（Go，源码 = go2rtc 上游 + 本仓库的补丁）
app/sdk/        空占位 —— 小米 SDK 需用户自取，见 NOTICE.md
docker/         Dockerfile、compose（host 与 bridge 两份）、go2rtc 配置模板
third_party/    go2rtc 补丁版：补丁文件 + MIT 许可 + 逐条改动说明
tools/          fetch SDK、构建定制版 go2rtc 镜像、出 fpk
cmd/ config/ manifest  飞牛 fpk 生命周期脚本与包清单（仓库根即 fnpack 源）
wizard/        飞牛安装/配置向导：装的时候选录像目录
docs/           部署、安全、隐私、fpk 上架注意
```

## 从源码跑（不用 Docker）

```sh
python3.12 app/server/main.py        # 默认 127.0.0.1:17071
HE_HOST=0.0.0.0 HE_PORT=8088 python3.12 app/server/main.py
```

后端零第三方 Python 依赖（小米那一路除外）。录像与配置落的位置按优先级是
`TRIM_PKGVAR`（飞牛应用中心注入）→ `HE_DATA_DIR`（Docker 镜像里设为 `/data`）→
兜底 `/tmp/homeeye-var`；从源码直接跑就是最后这个，想换目录请显式设置
`HE_DATA_DIR=/your/path`。

## 构建与发布

```sh
sh docker/build.sh                    # 构建并起容器
sh tools/build-go2rtc-ptz.sh          # 定制版 go2rtc 镜像 + 重建 heptz
sh tools/pack-fpk.sh                  # 出 homeeye.fpk（名字来自 manifest 的 appname，用官方 fnpack）
```

发布包的实测记录（构建、起容器、零配置首启、认领窗口、fpk 出包各自跑成什么样、
哪些没跑）在 `docs/DEPLOY.md` 第八节。

## 已知限制

- 对讲上行是 HTTP 分块推给 go2rtc，不是 WebRTC：延迟约 120 ms 一批，
  够用但不如原生 WebRTC 顺滑。改 WebRTC 需要 go2rtc 侧先有 WHIP 服务端
  （上游 `internal/webrtc/server.go` 里那条 PATCH 还是 `TODO`）。
- 事件侦测是**离线后处理**：分段写完（默认 5 分钟切片）后几分钟内才标出来，
  不是秒级告警；要更快的实时提醒，靠摄像头自己上报的设备事件。
- 事件侦测只解关键帧做像素差分，判断"有没有动静"，**不区分人/车/宠物** ——
  分类要模型，见 [docs/ROADMAP.md](docs/ROADMAP.md) 的 1.3 计划。
- 云台/对讲机型相关，见上表。
- 没有用户体系：一个密码管全部。要多人分级请放在反向代理后面。
- SD 卡回补要求摄像头自己开了录像且时间戳可信。

## 许可与合规

- 代码 MIT，见 [LICENSE](LICENSE)。
- 第三方组件许可、商标与"兼容性"声明、**为什么不含小米 SDK**：
  [NOTICE.md](NOTICE.md)
- 拍摄他人涉及个人信息保护义务，责任在使用者：[docs/PRIVACY.md](docs/PRIVACY.md)

## 免责声明

本软件按"现状"提供。作者不对因使用本软件造成的数据丢失、设备损坏、隐私泄露、
账号封禁或任何间接损失负责。摄像头是敏感设备 —— 在把它接到公网之前，
请先确认那台设备被扫到并弱口令命中时你会失去什么。
