# HomeEye

自建 NVR：把摄像头画面留在自己的 NAS 上。

主链路只用 **Python 标准库**（不装数据库、不要消息队列、没有 npm 构建；
可选旁路除外：对象识别要 `onnxruntime`，小米摄像头要你自己取的那份 SDK），
取流/转封装/切片录像交给系统自带的 **ffmpeg**，画面**不上传任何第三方服务器**
—— 除非你自己接了一个视觉大模型或归档远端，那两个地址是你填的，默认全部关着。
一台 NAS + 几条摄像头，换掉几百块的云存储订阅。

```
摄像头 ──RTSP/ONVIF/小米云──▶ HomeEye ──▶ 浏览器直播（MPEG-TS，秒开）
                                 │
                                 └──▶ 分段 MP4 录像 + 时间轴回放 + 本地事件/AI 标注
```

- 许可：**MIT**（第三方组件见 [NOTICE.md](NOTICE.md)）
- 平台：Linux（NAS）；飞牛 fnOS 有原生 `.fpk`，其它发行版走 Docker
- 不联网校验、无云端账号（用户与口令只存在这台机器的配置里）、无付费功能，代码全在这里

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

要给家人或保姆只开某几路：**设置 → 用户与授权** 建一个只读账号并勾上授权的摄像头；
谁在什么时候看了哪一路、谁取过完整地址，**设置 → 审计日志** 里能看到（本地 JSONL，留 90 天）。

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
| 对象识别（可选） | **镜像不带 `onnxruntime`**，需自己装或重建 | 宿主机 `pip3 install onnxruntime` + 自备 `.onnx` | 同 fpk |
| 音频事件 | 开箱可用（纯标准库，不要模型） | 开箱可用 | 开箱可用 |
| rclone（冷层归档，可选） | 镜像不带，自行放入或 `HE_RCLONE` 指路径 | 宿主机装 | 装 |

小米那一路的依赖（fpk / 裸跑在宿主机执行一次，Docker 不用）：

```sh
pip3 install aiohttp aiofiles requests cryptography av numpy \
             pydantic PyYAML psutil qrcode Pillow
```

**本地 AI 的三条能力各自独立**，缺哪条页面就点名缺什么，不会假装在跑：

- `ai.audio_events`（打开总开关 `ai.enabled` 后默认生效）：纯标准库，对分段做电平突变检测，
  只标"有声响"，不猜是玻璃碎还是婴儿哭 —— 那需要分类模型。
- `ai.objects`：还要 `numpy` + `onnxruntime` + 一个 COCO 类别的 YOLO 级 `.onnx`
  模型文件（许可与体积都不适合放进仓库，和小米 SDK 一样由你自取）。
  填 `ai.model_path`（**容器内**的绝对路径）后打开开关。
- `ai.vlm_*`：把事件截图发给一个 OpenAI 兼容的多模态接口换一句话描述。
  **默认关闭**；关闭时一帧画面都不会离开这台机器，开了也只走你自己填的那个地址。

**装完怎么确认**：登录后进 **设置 → 链路健康**，顶部会有一行「运行环境」自检，
缺什么直接写在那里（缺失的、需要你自己装的都点名）；服务日志里也有
`[env]` 开头的几行。Docker 用户不用管这一节 —— 除了小米 SDK 和上面这三条可选旁路，
镜像是自包含的。

## 功能

| 能力 | 状态 | 备注 |
|---|---|---|
| 多分屏实时画面（1/2/4/6/9） | ✅ | 单击放大、双击全屏；切布局**秒开**（复用已在跑的取流，不等重新起进程） |
| 双码流（主 / 子） | ✅ | 多分屏小窗口自动走子码流，省带宽也省 CPU；单屏仍取主码流 |
| 按需转码 | ✅ | `live.transcode = auto/always/never`；源已 H.264 就 `-c:v copy` |
| 连续录像 + 按日时间轴回放 | ✅ | 分段 MP4，断电重启不会出现索引与文件对不上 |
| 回放倍速与按事件跳转 | ✅ | 0.5×/1×/2×/4×；键盘在事件块之间跳，播放头自动对到事件前 2 秒 |
| 片段导出 + 免登录分享链接 | ✅ | 服务端 ffmpeg 拼接，默认套用遮蔽；链接带有效期与命中计数，可撤销 |
| 事件时间轴 | ✅ | 分段写完后本地离线侦测动静，时间轴上标事件块，点一下跳到那一刻 |
| 本地 AI 检测（人 / 车 / 宠物） | ⚠️ 可选旁路 | 进程内 `onnxruntime` + 用户自取的 `.onnx` 模型；只在已判定有动静的窗口抽帧，线程数钳到 ≤2。没装依赖就只显示"未安装"，不影响录像 |
| 音频事件（玻璃碎 / 婴儿哭 / 门铃这类响动） | ✅ 需开 `ai.enabled` | 8 kHz PCM 电平突变离线检测，与像素侦测同一批抽帧窗口；不识别具体是什么声音，只标"有声响" |
| 可选 VLM 一句话描述 + 自然语言检索 | ⚠️ 默认关闭 | 开了才把单帧（缩到 640 宽）交给你自己填的地址；关闭时**一帧都不出网**。之后可以问"昨天下午谁在客厅" |
| 录像源可选 PLAYED / RAW | ✅ | 录原始流省一半体积；**配了隐私遮蔽的路会自动改成重编码**，否则遮蔽只是播放器上的贴纸 |
| 保留天数 / 容量上限自动清理 | ✅ | 两个都填时**同时生效**，任一命中就删；被事件引用的分段留到最后 |
| 隐私遮蔽（画面坐标，最多 8 块） | ✅ | `drawbox` 烧进像素，直播 / 录像 / 小米取流 / 导出四条链路统一生效 |
| 录像完整性自检 | ✅ | 异步扫描空洞、0 字节段、时长不符；给结论也给每路明细 |
| 冷层归档（rclone） | ✅ | S3 / B2 / GDrive / 另一台 NAS 都行；远端校验通过才删本地，缺 `rclone` 就直说 |
| 访问审计 | ✅ | 登录、越权、取完整地址、看回放、导出、分享、改设置各记一行；本地 JSONL，保留 90 天，页面上可查 |
| 多用户与按摄像头授权 | ✅ | `admin` / `read` 两档；只读账号带摄像头白名单，未授权的那几路在列表与时间轴里直接不出现 |
| PWA（添加到主屏幕） | ✅ | 只缓存外壳与静态资源，视频流一律 network-first；通知点开直达那一路那一刻。**没有 Web Push**，关掉浏览器就不推 |
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
- 摄像头口令只存在 `data/config.json` 里；接口、状态、告警正文（邮件 / Webhook）
  里的流地址一律抹成 `user:***@`。要看原文只有管理员的一条专用接口，
  并且每次调用都进审计。
- 多用户：`admin` / `read` 两档，只读账号带摄像头白名单。权限落在接口层，
  不是藏按钮 —— 未授权的那一路在列表与时间轴里不出现，直接拼 URL 返回 **404**
  （不用 403，免得别人数出你家有几路）。改了谁的授权，他手上那条 Cookie 立刻按新范围生效；
  停用即刻踢下线 —— 否则授权就是个摆设。
- 访问审计：登录、越权、取完整地址、看回放、导出、分享、改设置都记一行
  （谁 / 哪个 IP / 什么时候），本地 JSONL 保留 90 天，字段名带 `pass/token/secret`
  的值写成 `***`。这份文件本身比口令敏感，别共享。
- 隐私遮蔽是**烧进像素**的，不是播放器上的贴纸：直播、录像、小米取流、导出
  四条链路都挂，配了遮蔽的那一路会自动改成重编码（否则 `-c:v copy` 会绕过它）。
- 录像保留期落盘前钳到 0–365 天（0 = 明确不自动删），免得"设 3650 天"
  变成事实上的永久留存。
- **分享链接是这道门上唯一主动开的洞**：免登录、带有效期、可撤销、有命中计数，
  但拿到链接的人不需要账号，而且页面会显示摄像头真名。详见
  [docs/SECURITY.md](docs/SECURITY.md) 里那一节。

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
- 事件侦测的**像素那一半**只解关键帧做差分，判断"有没有动静"；要区分人/车/宠物
  得开 `ai.objects`，而它需要你自己装 `onnxruntime` 并自备 `.onnx` 模型（镜像不带）。
  本项目**不给准确率数字**：那需要标注素材，我们没有，所以只说"检出了什么标签 +
  置信度"，不声称召回率。
- 音频事件只回答"这一刻有响动"，不分类（玻璃碎 / 婴儿哭 / 门铃是三个不同的模型，
  不是一个阈值）。
- VLM 描述要你自己提供一个 OpenAI 兼容的多模态接口；默认关闭，关闭时不发出任何请求。
- 通知是"浏览器还开着"才有的本地通知：**没有 Web Push**（VAPID 需要真证书 +
  公网可达，做完只覆盖少数部署形态）。系统级提醒请走邮件 / 企业微信 / 钉钉 / Bark / Webhook。
- 回放有倍速与按事件跳转，**没有逐帧步进**；时间轴有事件缩略图，**没有任意时刻的
  悬停预览**；没有 2~4 路同时刻对齐的多路同步回放。
- 完整性自检能扫出空洞 / 0 字节段 / 时长不符，但只报不修（不隔离坏段）；
  滚删是直接真删，**没有回收站**，也没有 SMART 体检。
- 云台/对讲机型相关，见上表。
- SD 卡回补要求摄像头自己开了录像且时间戳可信。
- 没有 LDAP / OAuth、没有按时间段授权、没有密码找回邮件（自建场景没有可靠邮件通道，
  管理员重置更诚实）。

## 许可与合规

- 代码 MIT，见 [LICENSE](LICENSE)。
- 第三方组件许可、商标与"兼容性"声明、**为什么不含小米 SDK**：
  [NOTICE.md](NOTICE.md)
- 拍摄他人涉及个人信息保护义务，责任在使用者：[docs/PRIVACY.md](docs/PRIVACY.md)

## 免责声明

本软件按"现状"提供。作者不对因使用本软件造成的数据丢失、设备损坏、隐私泄露、
账号封禁或任何间接损失负责。摄像头是敏感设备 —— 在把它接到公网之前，
请先确认那台设备被扫到并弱口令命中时你会失去什么。
