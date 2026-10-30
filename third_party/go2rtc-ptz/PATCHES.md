# go2rtc 小米摄像头补丁（云台 / 对讲）

上游：https://github.com/AlexxIT/go2rtc （本目录是在 `master-hardware` 对应源码上打的补丁）
基线版本：`1.9.15+dev`

HomeEye 的云台/对讲功能**依赖**这个补丁版 go2rtc。现网已切到本补丁构建的镜像
`homeeye/go2rtc:1.0.6-ptz`（源码基线 = git tag `v1.9.15-ptz.1`，见 `UPSTREAM.md`）；
官方原版 `alexxit/go2rtc:master-hardware` 只是它的基础层，没有下面这些接口。

---

## 为什么必须打补丁（三个硬约束）

### 1. 小米摄像头的 P2P 并发会话上限只有 3 条

云台（`cmdMotorReq 0x112`）和对讲（`cmdSpeakerStartReq 0x106`）的命令
**必须**跑在一条已经建立的 miss 会话上，另开一条会把摄像头的会话表挤满，
表现是所有人互相把对方踢掉（`cs2: EOF`）。

所以补丁让 go2rtc 把**自己取流用的那条会话**注册进一张全局表，
再暴露 HTTP 接口，直接在那条会话上下发命令 —— 摄像头那边一条会话都没多。

### 2. channel0（命令应答通道）必须有人读

摄像头对每条命令都会回一个应答，而 channel0 的接收缓冲区只有 **10 个槽**。
没人读的话灌满后 `cs2.dataChannel.Push` 返回 `pop buffer is full`，
worker 直接退出 —— **整条 P2P 连接被关闭**，之后所有命令都报 broken pipe。

原版 go2rtc 从来不读 channel0（只有媒体通道 channel2 被消费）。
补丁加了一个专职的命令读取协程（`pkg/xiaomi/miss/cmdreader.go`）。

实测：修复前连发第 10 条命令后连接必断；修复后连发 15 条依然正常。

### 3. channel3（上行音频通道）以前根本没发出去数据

`cs2/conn.go` 的 `WritePacket` 写错了：

```go
copy(req[offset+hdrSize:], hdr)   // ❌ 写的是头
```

payload 从来没被发出去，结果是 channel3 上全是空包 —— **对讲永远没有声音**。
改成 `copy(req[offset+hdrSize:], payload)` 之后才真正出声。

---

## 改动清单

| 文件 | 改动 |
|---|---|
| `pkg/xiaomi/miss/cs2/conn.go` | ① `WritePacket` 修正 payload 拷贝（channel3 上行音频）<br>② 新增 `ReadCommandRaw()`：返回未经拆分的原始命令缓冲区<br>③ `pktMu` 互斥锁：HTTP handler 并发写 channel3 要串行化 |
| `pkg/xiaomi/miss/cmdreader.go` | **新增**。命令通道读取协程 + 云台角度缓存（见下） |
| `pkg/xiaomi/miss/client.go` | Client 加 `cr *cmdReader` 字段 |
| `pkg/xiaomi/miss/producer.go` | `Dial()` 里调用 `StartCommandReader()`（**不在** `NewClient` 里，因为 `cmd/heptz` 侧车自带读取协程，两个协程抢同一条 channel0 会把回包分走） |
| `pkg/xiaomi/miss/registry.go` | 全局注册表 `Clients`：`Motor` / `MotorWait` / `MotorSeq` / `WaitMotor` / `MotorState` / `LastCommand` / `StartSpeaker` / `StopSpeaker` / `WriteAudio` / `SpeakerCodec` |
| `internal/xiaomi/ptz.go` | **新增**。4 个 HTTP 接口 + A-law 转换（见下） |
| `internal/xiaomi/xiaomi.go` | 注册 4 个路由：`/api/xiaomi/motor`、`/api/xiaomi/speaker`、`/api/xiaomi/audio`、`/api/xiaomi/ptz` |

## 新增的 HTTP 接口

```
GET  /api/xiaomi/motor?did=<did>&op=<0-15>   云台：0停 1右 2左 3上 4下（+5/6/10/11 快速档）
POST /api/xiaomi/speaker?did=<did>            打开喇叭通道（对讲）
POST /api/xiaomi/speaker?did=<did>&stop=1     关闭喇叭通道
POST /api/xiaomi/audio?did=<did>              body = PCM16 LE 8kHz 单声道
GET  /api/xiaomi/ptz                          会话状态 / 角度 / 对讲统计
```

`motor` 的返回带 `angle` / `elevation`（摄像头回传的真实朝向）。

## 两个容易踩坑的协议细节

### a) 应答帧的布局

channel0 弹出的缓冲区是 `[4 字节外层 cmd(大端)][加密的 body]`，
外层 cmd 一般是 `0x1001`（cmdEncoded，表示 body 是加密的）。

**解密后的 body 前面 4 个字节才是真正的命令号（大端）**，后面才是负载：

```
解密后：00 00 01 13 + {"ret":0, "angle":88,"elevation":100}
          ^^^^^^^^ cmdMotorRes(0x113)
```

所以 `cmdreader.go` 是先解密、再从前 4 字节取命令号。
一开始按固定偏移猜命令号，拿到的是 `0x1001`，角度一直取不到。

### b) 对讲音频的编码

摄像头要的是 **G.711 A-law（8bit/8kHz）**，而浏览器给的是 **PCM16**。
原版接口假设「调用方送来的已经是摄像头原生格式」，结果 PCM16 被当 A-law 切帧，
摄像头收到的是乱码（声音又尖又快）。

现在 `/api/xiaomi/audio` 的约定改成：**body 永远是 PCM16 LE 8kHz 单声道**，
由 go2rtc 按机型转换：
- codec 1027 (PCMA) → 转 A-law，一帧 40ms = 320 字节
- codec 1024 (PCM16) → 原样，一帧 40ms = 640 字节
- codec 1032 (OPUS) → 暂不支持

## 编译与出镜像

一条命令从本仓库源码到镜像（NAS 上跑，只需要 docker）：

```sh
sh homeeye-build-image.sh
```

它做四件事：容器内静态编译（`golang:1.26-alpine`，宿主机不用装 Go）→ 以
`alexxit/go2rtc:master-hardware` 为基座替换 `/usr/local/bin/go2rtc` → 打 tag →
**在独立端口起一个临时容器做冒烟回归**（`/api/xiaomi/ptz` 必须在、`motor` 不能 404），
任一不过就 exit 1。手工版等价命令：

```sh
docker run --rm -v "$SRC":/src -v /tmp/gocache:/go -w /src \
  -e GOPROXY=https://goproxy.cn,direct -e CGO_ENABLED=0 -e GOFLAGS=-mod=mod \
  golang:1.26-alpine go build -trimpath -ldflags="-s -w" -o /out/go2rtc ./
```

## 部署

现网走的是**镜像**，不是 bind mount 二进制：改 compose 的 `image:` 再
`docker compose up -d --force-recreate go2rtc`。

早先用过的 `nvr/he_build_g2r.sh`（停容器 → 覆盖 `/tmp/g2build` 里的二进制 → 起容器）
只适合临时试验：二进制是挂进去的，运行中覆盖会 `Text file busy`，而且镜像内容与实际
跑的字节会不一致，出事查不到是哪份代码。要以镜像为准。

升级与回归的完整流程见 `UPSTREAM.md`。
