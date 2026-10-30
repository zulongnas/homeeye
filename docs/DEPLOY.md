# Docker 部署

三种装法，按你的场景挑一种：

| 场景 | 装法 |
|---|---|
| NAS（飞牛/群晖/威联通）或任何 Linux 主机 | 本文的默认 compose（host 网络） |
| 只想在 Mac/Windows 上试一下 | `docker-compose.bridge.yml` |
| 飞牛 fnOS，想装进应用中心图标栏 | `.fpk`，见 [DEPLOY-FNOS.md](DEPLOY-FNOS.md) |

## 一、最小可用（Linux / NAS）

```sh
git clone https://github.com/zulongnas/homeeye.git homeeye && cd homeeye/docker
cp .env.example .env          # 选存放路径；直接回车不改也能跑（用仓库下的 data/ 与 rec/）
docker compose up -d          # 镜像从 ghcr.io 拉（公开，免登录）
docker compose ps
curl -s http://127.0.0.1:8088/api/health | head -c 300
```

两个镜像：

| 镜像 | 是什么 | 备注 |
|---|---|---|
| `ghcr.io/zulongnas/homeeye:1.2.1` | 本体（Python + ffmpeg） | **不含小米 SDK**（许可不允许再分发），RTSP/ONVIF 全都用它 |
| `ghcr.io/zulongnas/homeeye-go2rtc-ptz:1.9.15` | go2rtc 官方 `master-hardware` + 云台/对讲补丁 | tag 是 go2rtc 的上游版本（它只在换上游时变）；换回 `alexxit/go2rtc:master-hardware` 也能拉流硬转 |

要改代码、要小米 SDK、或者不想用别人构建的镜像，就用本地构建（镜像名不变，
本地镜像优先于远端同名镜像）：

```sh
docker compose build          # 或 up -d --build
```

浏览器打开 `http://<这台机器的IP>:8088` → 设登录密码 → 设置 → 摄像头 → 添加。

落盘位置（宿主机路径全在 `docker/.env` 里，默认在仓库根下，`.gitignore` 已排除，别提交）：

```
$HE_DATA_VOLUME/config.json     配置：摄像头、录像策略、通知、登录口令派生值
$HE_DATA_VOLUME/xiaomi_auth/    米家授权缓存（access/refresh token）
$HE_DATA_VOLUME/tls/            自签证书（8443 用）
$HE_REC_VOLUME/                 录像分段 MP4（按摄像头 ID 分目录）
go2rtc/go2rtc.yaml              go2rtc 的配置（只有用小米/硬转才需要）
```

## 二、要改的三个地方

1. **时区** `TZ`。录像文件名按它命名，填错回放时间轴就对不上。
2. **存放路径**（`docker/.env` 里两行，不用碰 compose）：
   ```sh
   HE_DATA_VOLUME=/mnt/nas1/homeeye-data   # 配置 + 授权缓存 + 证书
   HE_REC_VOLUME=/mnt/bigvol/homeeyeRec    # 录像（一天几十 GB，认真挑块盘）
   ```
   改完 `docker compose up -d` 重建容器生效；换目录后旧分段不会自动搬，自己拷。
   容器里这两个路径固定是 `/data` 和 `/rec`，所以设置页里看到的录像目录是 `/rec`
   —— 宿主机在哪儿由 `.env` 决定，这一层分工是刻意的：设置页管"容器里往哪写"，
   `.env` 管"这台机器的哪块盘"。
   ⚠️ 飞牛上必须写**真实**的 `/volN/...` 路径：`/fs/...` 只是文件管理器的显示层，
   bind mount 认不到它。挂进来以后设置页的下拉才会出现这一项；没挂就是没挂，
   页面会如实说。
   不想单独挂录像盘：把 compose 里 `/rec` 那行挂载和 `HE_REC_DIR=/rec` 一起删掉
   （只删一行会把录像写进容器可写层，重建就没了），录像就落在 `$HE_DATA_VOLUME/recordings/`。
3. **网络模式**。见 README「为什么默认 host 网络」。

## 三、加摄像头

设置 → 摄像头 → 「＋ 添加摄像头」，四种类型：

- **RTSP**：`rtsp://user:pass@192.168.1.20:554/...`。海康/大华/TP-LINK 这类走这条。
- **ONVIF**：填主机+端口，HomeEye 用标准 Profile 拉信息并取 RTSP 地址。
  局域网自动发现要组播，bridge 网络下不可用，请手工填地址。
- **小米**：扫码授权，自动带出 `did` 与 `model`。**需要自备 SDK**：
  ```sh
  sh tools/fetch-miloco-sdk.sh
  docker compose build --build-arg HE_WITH_MILOCO_SDK=1 homeeye
  docker compose up -d --force-recreate homeeye
  ```
  （构建出来的镜像名和公开镜像相同，只是本地这份多带 SDK；`docker compose images`
  能看到用的是哪个 ID。公开镜像是故意不带 SDK 的。）
- **其他流**：任何 ffmpeg 认的地址（HTTP-FLV、RTMP、`pipe:` 之类）。

### 想省 CPU / 省带宽：让 go2rtc 做一次硬转

一台摄像头被多个消费者看时，正确做法是**转一次**给大家分，而不是每个消费者各转一遍。
把 `docker/go2rtc.yaml.example` 复制成 `go2rtc/go2rtc.yaml`，按里面的注释填：

```yaml
streams:
  gatecam:      "rtsp://user:pass@192.168.1.20:554/Streaming/Channels/101"
  gatecam_play: "ffmpeg:gatecam#video=h264#audio=aac#hardware#bitrate=4M"
```

HomeEye 里就填 `rtsp://127.0.0.1:8555/gatecam_play`。命名约定是
`<名字>` = 原始流、`<名字>_play` = 转码流，录像源选 RAW 时 HomeEye 会顺着这个
约定回到不带 `_play` 的那一路。

核显硬转要放开 compose 里注释掉的 `devices: /dev/dri`，并确认
`ls -l /dev/dri` 有 `renderD128`。没有核显就把 `#hardware` 删掉退回软转。

**别把 HomeEye 的第二道转码开回来**：`live.transcode=auto` 探到源已是 H.264 时
直接 `-c:v copy`。走外网觉得卡，该改的是 go2rtc 那一路的 `bitrate`。
实测同一路从吃满一个核（57%～96.7%）降到 4.7%。

## 四、对讲与 HTTPS

见 README「对讲为什么要 HTTPS」。**反向代理**是唯一适合外人与上架的入口：

```nginx
server {
    listen 443 ssl http2;
    server_name nvr.example.com;
    # 证书用 Let's Encrypt（acme.sh / certbot / 飞牛自带的证书功能都行）
    ssl_certificate     /etc/letsencrypt/live/nvr.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/nvr.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8088;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        # 直播是 chunked 长连接，别让它被缓冲掐断
        proxy_set_header Connection        "";
        proxy_buffering                    off;
        proxy_read_timeout                 3600s;
        client_max_body_size               8m;   # 对讲上行分块要够宽
    }
}
```

反代理到 8088 之后，浏览器看到的是 `https://nvr.example.com` —— 安全上下文成立，
麦克风可用，8443 那个自签端口就不用暴露了。

## 五、故障排查

| 现象 | 先看这个 |
|---|---|
| 页面打得开但列表是空的 | 你还没设密码。这是认领窗口，不是 bug |
| 直播 200 但 0 字节（小米） | bridge 网络的 P2P 打洞问题 → 换 host；或没装 SDK |
| 直播黑屏、日志有 `PPCS_Connect errorcode: -3` | 同上，网络模式 |
| 画面卡顿、CPU 高 | 设置 → 实时画面 看 `live.video` 是不是走了 `libx264`；源已 H.264 却还在转 = `transcode` 被设成了 `always` |
| 听不到对方声音 | 设置 → 实时画面 的 `live.audio` 被关了；链路只剩半边 |
| 对讲按下去没反应 | 页面不是安全上下文（用 http://内网IP 打开）→ 见第四节 |
| 录像目录写不进去 | 健康检查会报 broken；确认挂载与目录属主（容器里是 root，飞牛 fpk 版是 `homeeye` 账号） |
| 摄像头被拔电后容器变红 | 不该发生。offline 只记流水；变红说明是 broken，看 `/api/health` 的 per-cam reason |
| 云台转不动（小米） | 先确认 go2rtc 用的是 `homeeye-go2rtc-ptz`（`docker inspect go2rtc --format '{{.Config.Image}}'`）；`/api/xiaomi/ptz` 404 说明在跑官方镜像 |

看健康检查（免登录，只回结论）：

```sh
curl -s http://127.0.0.1:8088/api/health
curl -s 'http://127.0.0.1:8088/api/health?fresh=1'     # 强制同步探一次链路，慢但准
```

带登录态能看到完整细节（存储占用、看门狗心跳、每路的 `video`/`video_note`）：

```sh
umask 077
read -rsp 'HomeEye 密码: ' HE_PW
printf '{"password":"%s"}' "$HE_PW" > /tmp/he_login.json
unset HE_PW
curl -s -c /tmp/he_c -H 'Content-Type: application/json' \
     -d @/tmp/he_login.json http://127.0.0.1:8088/api/login
rm -f /tmp/he_login.json
curl -s -b /tmp/he_c 'http://127.0.0.1:8088/api/health?fresh=1'
```

别把密码直接写进 `curl -d '{"password":"..."}'`：那会进 shell 历史和这条命令的
argv，同机器上任何用户 `ps` 一看就到眼。上面 `read -s` + 临时文件的做法
（`printf` 是 shell 内建，不会 exec，也就不进 argv）只是把口令留在你刚 chmod 过的那
个文件里，用完就删。

## 六、升级与回滚

```sh
git pull
cd docker && docker compose pull && docker compose up -d   # 换新镜像，data/ 不动
```

配置格式向后兼容：老配置里的 `live.transcode: true/false` 会自动翻成
`always/never`（升级不改行为）。回滚 = 换回旧 tag 的镜像再起一次；
`data/` 不需要动，也没有数据库迁移。新版镜像在
[Releases](https://github.com/zulongnas/homeeye/releases) 里能找到对应版本号
（tag 就是镜像 tag，例如 `1.2.1`）。

## 七、监控接入（Prometheus / Grafana）

`GET /api/metrics` 是 Prometheus 文本格式（0.0.4）的只读快照。抓取器没有浏览器
会话，所以给它一个令牌：

```sh
# docker/.env
HE_METRICS_TOKEN=$(openssl rand -hex 24)
docker compose up -d          # 让环境变量进容器
curl -s "http://127.0.0.1:8088/api/metrics?token=$HE_METRICS_TOKEN" | head
# 也可以走请求头：Authorization: Bearer <token>
```

```yaml
# prometheus.yml
scrape_configs:
  - job_name: homeeye
    scrape_interval: 30s
    metrics_path: /api/metrics
    static_configs:
      - targets: ["127.0.0.1:8088"]
    authorization:
      credentials: <HE_METRICS_TOKEN>
```

- 没设 `HE_METRICS_TOKEN` → 这个接口和其它 API 一样要登录会话，默认不多开口子。
- 令牌只放行 `/api/metrics`；不要拿它当登录口令，也不要用它去调别的接口。
- 抓取读的是内存快照，不触发链路探测；整段结果缓存 5 秒，多抓取器不会放大负载。
- 指标族与字段含义见 [README 的「监控接入」](../README.md#监控接入prometheus--grafana)。
- fpk 安装方式：在应用中心的「配置」里给 `HE_METRICS_TOKEN` 环境变量（或直接在
  `cmd` 启动脚本的 `export` 里加），效果与 Docker `.env` 相同。

## 八、发布包实测记录（2026-10-30）

这节只写"跑过的"，跑完没验证的东西写在最后的「没验证」里。文档其它地方的说法以这节为准。

执行环境：一台飞牛 NAS（x86_64、4 核、Docker 28.5.2），用**发布树**（= 本仓库根，
不含任何运行数据）直接跑，跑完容器、镜像、临时目录全部删掉。

| 项 | 命令 | 结果 |
|---|---|---|
| 公开 Dockerfile 构建 | `docker build -t homeeye:pubtest -f docker/Dockerfile .`（`HE_WITH_MILOCO_SDK` 默认 0） | 成功；最后一步自检打印 `[he] image ok, version 1.1.2` |
| 起容器 | `docker run -d -p 127.0.0.1:18099:8088 -v <tmp>/data:/data homeeye:pubtest` | 8 秒时状态已经是 `Up (healthy)`，走的是镜像里 `HEALTHCHECK` 的 `/api/health` |
| 零配置首启 | 看 `/api/state` | `cameras: []`、`storage.root=/data/recordings`、未设密码时 `auth.configured: false` |
| 认领窗口 | 无 session `POST /api/password` | 200 并直接发登录态；此时不带 cookie 的 `GET /api/state` → **401** |
| 口令存储 | 读 `data/config.json` | `auth` 里只有 `enabled / pw_hash / salt / session_hours / username`，`pw_hash` 是 64 位 hex，明文不落盘 |
| 错密码 | `POST /api/login` 带错口令 | 401 |
| compose 结构 | `docker compose -f docker-compose.yml config --quiet`，`-f docker-compose.bridge.yml` 同理 | 两个都过；卷按 `../data`、`../go2rtc` 解析到仓库根（不是 compose 文件同级） |
| 服务端源码 | 13 个 `app/server/*.py` 逐个 `py_compile` | 全部通过（Python 3.12） |
| fpk 出包 | 官方 `fnpack build -d .` | 成功；manifest 末尾自动注入 `checksum = md5(app.tgz)` |
| 脚本执行位 | 对比旧版包安装后的 `/var/apps/homeeye/cmd/` | 包里 tar 成员是 `0666`，装完是 `0755 root:root` —— 执行位由安装端补，细节见 `docs/DEPLOY-FNOS.md` |
| GHCR 匿名可拉 | 同一台 NAS 上先 `docker logout ghcr.io`，再 `docker pull ghcr.io/zulongnas/homeeye@sha256:2e0961…` | 无需任何凭据；含下载 3 分 36 秒（境外源，慢但通） |
| GHCR 镜像能跑 | 用拉下来的镜像起临时容器（`-p 127.0.0.1:18077:8088`，`/tmp` 数据卷） | 8 秒 `healthy`；`/api/health` → `{"ok":true,"version":"1.1.3"}`；未认领时 `/api/state` → 401 |
| GHCR 云台版 go2rtc | Actions 日志里脚本第 5 步的冒烟输出 | `/api/xiaomi/ptz` 有返回、`/api/xiaomi/motor` 不是 404 —— 补丁确实在镜像里 |

**1.1.4（云台修复 + 运行环境自检）同一套复核，结果如下：**

| 项 | 命令 | 结果 |
|---|---|---|
| fpk 出包 | `FNPACK=…/fnpack.exe sh tools/pack-fpk.sh`（发布树根） | 成功；5,751,700 B；`checksum = 57870f91e1b6fbb75725933fef50c2a4`；`app.tgz` 里有 `server/nw_env.py` |
| fpk 匿名下载 | Release 附件 Range 请求 | 206，`Content-Range: 0-1023/5751700` |
| GHCR 匿名 manifest | 不带凭据请求 `homeeye:1.1.4` 的 manifest（`Docker-Content-Digest`） | 200；digest `sha256:59d47b19828c…`；云台版 `homeeye-go2rtc-ptz:1.9.15` digest `sha256:0124d7df6ee0…` |
| GHCR 启动冒烟 | Actions 工作流里的冒烟步骤（起容器后 `curl /api/health`） | 200 才允许推送；run 41035881751 两个 job 全绿 |
| 云台修复真机验证（NAS，1.1.4） | 帧相关 + `/api/xiaomi/motor` | 点右画面右移 36 px、点左左移 36 px；上/下仰角 27↔39；按住 2 秒松手后停在原地（租约自停）；接口 0.4–0.9 ms |
| 运行环境自检 | 容器日志 + 登录后 `/api/health` | 日志 `[env] install=docker python=3.12.16 ffmpeg=7.1.5` / `xiaomi=ok`；`health.env.warnings` 为空 |

镜像由仓库的 `.github/workflows/ghcr.yml` 手动触发构建（`permissions: packages: write`，
用仓库自带 `GITHUB_TOKEN`，不需要任何个人令牌）。本体按 HomeEye 版本打 tag，
云台版 go2rtc 按上游版本打 tag（`1.9.15`）。

**1.2.0（事件时间轴 / 通知联动 / Prometheus 指标）复核，2026-10-31：**

| 项 | 命令 | 结果 |
|---|---|---|
| 本地逻辑回归 | `_verify_events.py` / `_verify_notify.py` / `_verify_metrics.py` | 全过；指标套件 38/38（令牌矩阵、`?token=`、标签转义、HELP/TYPE 配对、行格式） |
| 容器内 HTTP 回归 | 三套脚本在 `homeeye` 容器里各起一次性实例（隔离数据目录） | 事件 9/9、通知 17/17、指标全过（含网关前缀路径与 HEAD） |
| 生产抓取（真机） | 真实 NAS 容器 + `.env` 里的 `HE_METRICS_TOKEN` | 200；`text/plain; version=0.0.4`；23/23 项通过；无令牌/错令牌 401；5 秒内快照一致、6 秒后 `uptime` 增长 |
| 扫描器心跳 | 生产 `/api/metrics` 的 `homeeye_scanner_beat_age_seconds` | 3.08 秒（正在扫真实录像目录），`events_today` 按路输出 |
| 分屏切换 | 浏览器点 1/2/4/6/9 | 画面数 1/2/4/6/9 全部正确（修掉"点 4 及以后都只显示 2 个"） |
| 事件时间轴 | 本地一次性实例（假分段 + 假事件） | 4 个录像块 + 6 个事件标记 + 6 张事件卡，点标记跳转 |
| 设置页 | 通知多渠道 / 布防 / 日报 / 事件保留 | 全部渲染、保存回环成功；未生效项只剩移动侦测的分层参数 |

1.2.0-A（按 IDR 起播修绿带）与 1.2.0-B（双码流）在开发阶段已在真机主/子码流上
验证过；本次回归没有再采样画面，只确认这两块的代码路径没被后续改动碰到。

**1.2.0 发版与上线（2026-10-31）：**

| 项 | 结果 |
|---|---|
| 分支 | `main` @ `c04b845`（20 文件，+2102/−214） |
| tag / Release | `v1.2.0`；Release 附件 `homeeye.fpk` 5,774,114 B，md5 `37e71d2a052f539a144c1b83b95be4e5` |
| GHCR 构建 | run `41132909101`（`workflow_dispatch`，version=1.2.0、go2rtc=v1.9.15）；`homeeye 本体` 与 `go2rtc（云台补丁版）` 两个 job 全绿 |
| GHCR 匿名 manifest | `homeeye:1.2.0` → `sha256:d1b4f7d3…`；`homeeye-go2rtc-ptz:1.9.15` → `sha256:a91bb0c2…`（不带凭据可取，镜像公开） |
| NAS 切换 | 源换为 tag v1.2.0 → 本地构建 `homeeye-nas:1.2.0`（`HE_WITH_MILOCO_SDK=1`）→ `docker compose up -d homeeye`；容器 6 秒到 `healthy` |
| 切换后自检 | `/api/health` → `version 1.2.0`，两路摄像头 `ok`；生产 `/api/metrics` 23/23 全过；扫描器心跳 0.19 s |
| 回滚路径 | NAS 上 `/tmp/homeeye-src-1.1.4.tgz` + 同目录 `src.old`；`homeeye-nas:1.1.4` 镜像仍在，compose 的 image 改回 1.1.4 再 `up -d` 即回滚 |
| Release 附件回读 | 在 NAS 上 `curl` 下载附件（本机直连被网络拦，NAS 重试后成功）：5,774,114 B、md5 `37e71d2a…`，与本地出包一致 |

**1.2.1（直播闪现后黑屏 / 时间戳定格）修复，2026-10-31：**

用户反馈：实时画面"能瞬间出来、瞬间又没了"，有时黑屏等一会儿直接报错；主卧摄像头
左上角时间戳定住不动。复现与定位结论：**两道服务端竞态 + 一道前端静默失败**。

| 根因 | 代码位置 | 现象 |
|---|---|---|
| `_spawn()` 无互斥：`add_sub()` 与 `tick()` 都会起 ffmpeg | `app/server/nw_live.py` | 单个观看端就能起 2 路 ffmpeg（实测间隔 0.9 s、ppid=1 常驻 435 s），两条 PTS 时间线交错喂同一路，解码器报 `PIPELINE_ERROR_DECODE` |
| 读线程不校验"自己是否还是当前发布进程" | `nw_live.py::_read_loop/_feed` | 被顶替的旧 ffmpeg 继续往订阅者写，孤儿进程永不退出 |
| `add_sub()` 先挂订阅、后补历史段 | `nw_live.py::add_sub` | 实时批次抢在历史段前面，时间戳倒着走 |
| 前端 ERROR 只显示"自动重试中…"，实际没有重试 | `app/www/app.js::attachStream` | 解码器一旦被坏时间线搞死，格子永久黑屏 |
| MSE 在时间戳跳变后静默停住（不抛 ERROR） | `app/www/app.js` | 断电/重连后画面定格、时间戳不动，只能手动刷新 |
| tile 登记被 `player:null` 覆盖 | `app/www/app.js::makeTile` | 切页面时播放器不销毁，服务端订阅泄漏、ffmpeg 永不休息 |

证据（1.2.0 镜像 / NAS 真机）：同一路 `zwcam_low` 存在 2 个 ppid=1 的 ffmpeg；
抓 `/live/c1.ts?q=low` 15 秒，ffprobe 报 6 处 PTS 回退、ffmpeg 解码 16 行错误
（`mmco: unref short failure`）；修复后同条件 **0 回退、0 解码错误**。

修复内容：
- `_spawn()` 两段式加锁（先便宜判断、再持锁复检后提交），已有存活发布进程时直接返回；
- `_read_loop` / `_feed` / `_err_loop` 绑定进程身份，被顶替的进程立即停止喂流并被 kill；
- 每次新起 ffmpeg 清空环形缓冲、老订阅者重新等 IDR（两条时间线不混）；
- `add_sub()` 在锁内先灌历史段再挂订阅；首段历史最多可到整环 4 MB（主码流大 GOP 不再被 2 MB 上限清空）；
- `_live_stream()` 短暂无数据不再立刻收尾（进程活 48 s / 进程死 24 s 才断），避免浏览器看到假 NetworkError；
- 前端：ERROR 时真正重建播放器（1→8 s 指数退避）；新增 2 秒轮询的"静默卡死"看门狗（currentTime 连续 6 秒不动即重建）；
- 前端：修掉 tile 条目被覆盖（播放器现在会随视图切换销毁，服务端订阅归零）。

**1.2.1 三轮验证（2026-10-31）：**

| 轮次 | 内容 | 结果 |
|---|---|---|
| 本地逻辑 | `test_single_publisher.py`（8 线程并发 `add_sub` + 拉长 `cmdline` 复现竞态）、`test_keyframe_offset.py`、`py_compile`、`node --check` | 1.2.0 代码起 **8 路** ffmpeg（断言失败，成功复现）；修复后 **1 路**；崩溃重启、停止、关键帧对齐全过 |
| 容器 A/B | 同机 `homeeye-nas:1.2.0`（未修）与补丁版，同源同配置，抓 15 秒 TS 比对 | 未修：53 包、6 处 PTS 回退、16 行解码错误；修复：180 包、0 回退、0 错误 |
| 并发与恢复 | 6 观看端同时开播；kill 发布进程；切视图 | ffmpeg 峰值 1 路；kill 后 tick 2 秒内重起，浏览器约 10 秒内自动恢复；切视图后服务端订阅归 0 |
| 浏览器真机 | Chrome 两路网格 / 单画面主码流，连续播放 60 秒 + 中断恢复 | 无 MediaError / NetworkError，currentTime 持续前进，控制台无一条 warn/error |

### 没验证的（别照着这些吹）



- **公开镜像整镜像二次拉取（1.1.4）**：NAS 上匿名 `docker pull` 这次卡在到 ghcr.io 的
  IPv6 路由上（层显示下载完、连接随后无进展，15 分钟放弃），所以没做"拉下来起临时容器"。
  1.1.4 以 **Actions 冒烟（起容器 `/api/health` 200）+ 匿名 manifest 200** 为准；
  1.1.3 那次在 IPv4 上整拉成功（3 分 36 秒）。网络好的环境拉 1.1.4 应无差别。
- **小米那一路**：这次构建 `HE_WITH_MILOCO_SDK=0`，SDK 没装，`app/sdk/` 只有一个占位
  README。容器日志里的 `ptz disabled: 未配置...` 就是这条路径缺依赖时的正常降级，
  **不代表小米功能可用**。要走小米得自己跑 `sh tools/fetch-miloco-sdk.sh`，再
  `--build-arg HE_WITH_MILOCO_SDK=1` 重新构建 —— 这条完整链路这一轮没有重跑。
- **对讲**：只验证了服务能起、`data/tls/` 生成了自签证书、日志打印 8443 在听；
  浏览器端到端（`getUserMedia` 要 HTTPS 或 localhost）没做。
- **核显硬转**：compose 里 `/dev/dri` + `privileged` 默认注释状态，没按发布包的路径实测。
- **fpk 1.1.2 全新安装**：只做到"官方工具接受 + 包内复核"，没在真机点安装。
- **bridge 变体**：只做了 `config` 结构校验，没真起过容器。
