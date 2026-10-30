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
docker compose up -d --build
docker compose ps
curl -s http://127.0.0.1:8088/api/health | head -c 300
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
| 云台转不动（小米） | 需要定制版 go2rtc：`sh tools/build-go2rtc-ptz.sh`，然后换 compose 里的 image |

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
cd docker && docker compose up -d --build          # 镜像重建，data/ 不动
```

配置格式向后兼容：老配置里的 `live.transcode: true/false` 会自动翻成
`always/never`（升级不改行为）。回滚 = 换回旧 tag 的镜像再起一次；
`data/` 不需要动，也没有数据库迁移。

## 七、发布包实测记录（2026-10-30）

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

### 没验证的（别照着这些吹）

- **小米那一路**：这次构建 `HE_WITH_MILOCO_SDK=0`，SDK 没装，`app/sdk/` 只有一个占位
  README。容器日志里的 `ptz disabled: 未配置...` 就是这条路径缺依赖时的正常降级，
  **不代表小米功能可用**。要走小米得自己跑 `sh tools/fetch-miloco-sdk.sh`，再
  `--build-arg HE_WITH_MILOCO_SDK=1` 重新构建 —— 这条完整链路这一轮没有重跑。
- **对讲**：只验证了服务能起、`data/tls/` 生成了自签证书、日志打印 8443 在听；
  浏览器端到端（`getUserMedia` 要 HTTPS 或 localhost）没做。
- **核显硬转**：compose 里 `/dev/dri` + `privileged` 默认注释状态，没按发布包的路径实测。
- **fpk 1.1.2 全新安装**：只做到"官方工具接受 + 包内复核"，没在真机点安装。
- **bridge 变体**：只做了 `config` 结构校验，没真起过容器。
