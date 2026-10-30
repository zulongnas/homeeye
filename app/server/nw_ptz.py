# -*- coding: utf-8 -*-
"""HomeEye · 云台 / 对讲后端

小米摄像头云台不走官方 Miloco SDK（.so 只导出了取流接口），而是走 Mi Home 的
**miss 协议**私有命令：

    cmdMotorReq(0x112) + {"operation":N}    N: 1 右 / 2 左 / 3 上 / 4 下
    回包 cmdMotorRes(0x113) 带 {"ret":0,"angle":..,"elevation":..}

这条通道必须由**持有 P2P 会话的进程**发送，且 channel0 的响应必须持续读走，
否则缓冲区（10 个槽）灌满会把整条连接关掉。

🔴 会话上限是这个功能的真正约束
--------------------------------
小米摄像头的并发 P2P 会话上限实测只有 **3 条**。而 miss 协议的云台/对讲命令
**必须**跑在一条已建立的 miss 会话上（另开一条会挤爆摄像头的会话表，表现为
所有客户端互相把对方踢掉，错误是 `cs2: EOF`）。

所以有两种后端：

* **go2rtc 模式（推荐，零新增会话）**
  在 go2rtc 里给 miss producer 加了 `/api/xiaomi/motor`、`/api/xiaomi/speaker`、
  `/api/xiaomi/audio` 三个接口，直接在它**已经为这路流建立的那条会话**上下发命令。
  HomeEye 只需要调用这些 HTTP 接口——摄像头那边一条会话都没多。

* **sidecar 模式（没有 go2rtc 时的兜底）**
  另起一个常驻小进程 `heptz`（Go，静态二进制）自己维护一条会话做云台/对讲。
  它会占用 3 条额度里的 1 条，所以必须**空闲自动释放**，否则和取流互相踢。

凭据说明：miss 协议要的是米家账号的 `userId + passToken`（用于换 P2P 参数），
和 SDK 扫码登录拿到的 OAuth access_token 不是一回事，所以这里单独配置。
已经装了 go2rtc 的机器可以直接从它的配置里导入。
"""
import base64
import glob
import http.client
import json
import math
import os
import re
import struct
import subprocess
import sys
import threading
import time
import urllib.parse

import nw_config as C

_lock = threading.RLock()
_proc = None
_last_error = ""

# 正在对讲的 did 集合（避免闲置回收把对讲掐断）
_talking = {}

# go2rtc 会话（preload）相关
_preload_lock = threading.RLock()
_preloaded = {}          # did -> {"stream": 流名, "at": 建立时间}
_cfg_cache = {}          # go2rtc 配置原文缓存（30 秒）

# 没配 g2r_addr 时才需要探测常见地址。探测一轮最坏要几个 3 秒，而巡检/看门狗每轮
# 都要问一次地址，所以结果要缓存：探到了存 5 分钟，探不到只存 15 秒
# （go2rtc 可能马上就被起来，太久不把人挡在门外）。
_api_cache = {"addr": "", "at": 0.0, "found": False}
_API_TTL_FOUND = 300.0
_API_TTL_MISS = 15.0
# 探测结果本身也要缓存：go2rtc 停机时，健康巡检/云台判定每一轮都会问一次
# "这是不是带云台接口的 go2rtc"，没有缓存就是每次白等一个连接超时。
_probe_cache = {}        # base -> (时刻, 是否可用)
_PROBE_TTL_OK = 300.0
_PROBE_TTL_BAD = 15.0


class PtzError(RuntimeError):
    """云台/对讲错误。

    `need_session=True` 表示「这路摄像头现在没有可用的取流会话」——
    go2rtc 模式下云台命令必须复用取流那条 P2P 会话，前端看到这个标记就
    应该先把画面打开、等会话建立，然后重试一次命令。
    """

    def __init__(self, msg, need_session=False):
        RuntimeError.__init__(self, msg)
        self.need_session = need_session


# go2rtc 侧「没有活跃会话」的原始报错（见 pkg/xiaomi/miss/registry.go）
_NO_SESSION_MARK = "没有活跃的取流会话"


def _is_no_session(text):
    return _NO_SESSION_MARK in str(text or "")

# go2rtc 配置里常见的落盘位置（飞牛 docker 目录 / HA add-on 等）
_G2R_GLOBS = [
    "/vol1/*/docker/*go2rtc*/go2rtc.yaml",
    "/vol1/*/docker/*go2trc*/go2rtc.yaml",
    "/vol1/*/docker/go2rtc/go2rtc.yaml",
    "/config/go2rtc.yaml",
]


def _cfg():
    """云台配置。Docker 里容器看不到宿主机的 go2rtc 配置，所以额外支持
    用环境变量 HE_PTZ_USER / HE_PTZ_TOKEN 注入凭据（挂在 compose 里最省事）。"""
    c = dict(C.load().get("ptz") or {})
    eu = os.environ.get("HE_PTZ_USER") or ""
    et = os.environ.get("HE_PTZ_TOKEN") or ""
    if eu and et and not c.get("token"):
        c.update({"enabled": True, "user": eu, "token": et})
    return c


def backend():
    """当前用哪个后端：'go2rtc'（复用取流会话，零新增）还是 'sidecar'（自建会话）。"""
    mode = (_cfg().get("mode") or "").strip().lower()
    if mode in ("go2rtc", "sidecar"):
        return mode
    # 没显式配置时，能连上 go2rtc 的新接口就用 go2rtc（它不占会话名额）
    return "go2rtc" if _g2r_probe(g2r_api()) else "sidecar"


def g2r_api():
    """go2rtc 的 API 地址。

    配了就用配的；没配才去探常见地址 —— 探测是有代价的（两个地址 × 有无凭据
    两轮，每轮几秒），健康巡检和看门狗每一轮都要问这个地址，所以探到的结果要缓存
    一小会儿。缓存只在"没配地址"时生效，配了地址的现场（生产就是配的）不受影响。
    """
    c = _cfg()
    addr = (c.get("g2r_addr") or "").strip()
    if addr:
        return addr.rstrip("/")
    now = time.time()
    ttl = _API_TTL_FOUND if _api_cache["found"] else _API_TTL_MISS
    if _api_cache["at"] and now - _api_cache["at"] < ttl:
        return _api_cache["addr"]
    # 从本机探一下常见的几个端口
    for a in ("http://127.0.0.1:1984", "http://go2rtc:1984"):
        if _g2r_probe(a):
            _api_cache.update(addr=a, at=now, found=True)
            return a
    _api_cache.update(addr="http://127.0.0.1:1984", at=now, found=False)
    return "http://127.0.0.1:1984"


def _g2r_probe(base, force=False):
    """探测 go2rtc 是否带我们需要的云台接口（结果缓存，见 `_probe_cache`）。

    带了凭据就先试鉴权路径；go2rtc 那侧 401（说明凭据不对或还没配）再退回
    不带凭据试一次 —— 这样"先部署 HomeEye、后部署 go2rtc"两段切换期间，
    云台不会因为探测失败而整体退化成 sidecar（sidecar 要多占一条会话名额）。
    """
    now = time.time()
    hit = _probe_cache.get(base)
    if hit and not force:
        at, ok = hit
        if now - at < (_PROBE_TTL_OK if ok else _PROBE_TTL_BAD):
            return ok
    tries = [_g2r_auth_headers()]
    if tries[0]:
        tries.append({})        # 没配凭据时不必把同一个请求发两遍
    ok = False
    for auth in tries:
        try:
            conn = http.client.HTTPConnection(
                base.split("//", 1)[-1], timeout=2)
            conn.request("GET", "/api/xiaomi/ptz", headers=auth)
            res = conn.getresponse()
            body = res.read()
            conn.close()
            if res.status != 200:
                continue
            json.loads(body.decode("utf-8"))
            ok = True
            break
        except Exception:
            continue
    _probe_cache[base] = (now, ok)
    return ok


def forget_endpoints():
    """改过 go2rtc 的地址/凭据，或重启过 go2rtc —— 探测结果一律作废。

    缓存是为了少等连接超时，但设置页刚改完地址就看到"未就绪"，那是把缓存变成
    新的坑；所以这两件事发生时必须主动清掉。
    """
    _probe_cache.clear()
    _api_cache.update(addr="", at=0.0, found=False)


def g2r_ready():
    """go2rtc 后端是否可用（带云台接口且能连上）。"""
    if backend() == "sidecar":
        return False
    return _g2r_probe(g2r_api())


def enabled():
    """用户是否开启了云台功能。

    go2rtc 后端不需要单独配米家凭据（go2rtc 自己有），只需要探测到接口；
    sidecar 后端则必须有 user + token。
    """
    c = _cfg()
    if not c.get("enabled"):
        return False
    if (c.get("mode") or "").strip().lower() == "go2rtc":
        return True
    return bool(c.get("user")) and bool(c.get("token"))


def addr():
    return _cfg().get("addr") or "127.0.0.1:8899"


def binary():
    """heptz 可执行文件：优先随包分发，其次环境变量指定。"""
    p = os.environ.get("HE_HEPTZ_BIN") or os.path.join(C.APPDEST, "bin", "heptz")
    return p if os.path.exists(p) else ""


def available():
    """云台功能是否可用。"""
    if sys.platform.startswith("win"):
        return False, "云台仅在 NAS(Linux) 上运行"
    if backend() == "go2rtc":
        # go2rtc 后端不需要本机的 heptz 二进制，也不需要单独配凭据
        if not g2r_ready():
            return False, "连不上带云台接口的 go2rtc（需要在 go2rtc 里用打过补丁的版本）"
        return True, ""
    if not binary():
        return False, "缺少 heptz 可执行文件"
    if not enabled():
        return False, "未配置米家账号凭据"
    return True, ""


# ── 从 go2rtc 配置里导入凭据 ────────────────────────────────────────────
def detect_go2rtc():
    """扫描本机 go2rtc 配置，返回 (yaml_path, user, token) 或 None。

    go2rtc 的配置片段形如：

        xiaomi:
          "999999999": V1:xxxxxxxxxxxxxxxx

    go2rtc 用同一套 miss 协议，所以它的凭据可以原样复用。
    """
    for pat in _G2R_GLOBS:
        for path in glob.glob(pat):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            if "xiaomi:" not in text:
                continue
            m = re.search(
                r"^\s*[\"']?(\d{5,})[\"']?\s*:\s*(V1:[A-Za-z0-9+/=]+)",
                text, re.MULTILINE)
            if m:
                return path, m.group(1), m.group(2)
    return None


def find_g2r_stream(did):
    """在 go2rtc 配置里找到能代表这台摄像头的流名。

    go2rtc 的配置长这样：

        streams:
          ktcam: xiaomi://999999999:cn@192.168.1.24?did=988888888&model=...
          ktcam_play: ffmpeg:ktcam#video=h264#audio=aac#hardware...

    找到之后，HomeEye 就可以直接用 rtsp://<go2rtc>:8555/<流名> 取这一路，
    云台/对讲也复用 go2rtc 已经建立的那条会话——不额外占摄像头的会话名额。

    注意 `ktcam_play` 这种转码流：它的 URL 里没有 did，要顺着 `ffmpeg:<名字>`
    往回找到基础流才能定位到摄像头。
    """
    for text, path in _g2r_config_sources():
        hit = _match_stream_in(text, did)
        if hit:
            hit["config"] = path
            return hit
    return None


def find_g2r_session_stream(did):
    """按需建 P2P 会话时，应该挂 go2rtc 的哪条流。

    和「播给浏览器看」的选择不同，这里要的是**最稳**：
    挂**基础原始流**（ktcam），不挂 H.264 转码流（ktcam_play）。

    原因：转码流要拉起 ffmpeg + 硬件加速（核显没挂上就直接失败），
    而建会话只需要一条活的 miss 连接，原始流最直接。
    实测两者都只占 1 条 P2P 会话（转码流内部也是消费基础流），
    所以挂原始流不会多占名额。
    """
    hit = find_g2r_stream(did)
    if not hit:
        return None
    return hit.get("base") or hit["name"]


def _g2r_config_sources():
    """go2rtc 配置的来源：先问 go2rtc 自己（最准），再读本机可能存在的副本。

    顺序很重要：HomeEye 可能跑在容器里，看不到宿主机的 go2rtc.yaml；
    而 go2rtc 的 HTTP API 一定可达（云台命令就是走它）。
    """
    out = []
    text = _g2r_config_text()
    if text:
        out.append((text, "api"))
    for pat in _G2R_GLOBS:
        for path in glob.glob(pat):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    out.append((f.read(), path))
            except OSError:
                continue
    return out


def _g2r_config_text():
    """从 go2rtc 的 API 取它当前的配置（**YAML 原文**，不是 JSON）。

    注意：go2rtc 的 `GET /api/config` 返回的是原始 YAML 文本
    （Content-Type: application/yaml），必须按文本读，走 _g2r_raw；
    之前用 _g2r_call（JSON 解析）会把它变成一个 dict，后面 splitlines 就炸了。

    带 30 秒缓存：建会话、迁移这些路径会反复问配置，没必要每次都打一次 HTTP。
    """
    now = time.time()
    with _preload_lock:
        if _cfg_cache.get("text") and now - _cfg_cache.get("at", 0) < 30:
            return _cfg_cache["text"]
    try:
        text = _g2r_raw("GET", "/api/config", timeout=8)
    except Exception:
        return ""
    if not isinstance(text, str) or "streams:" not in text:
        return ""
    with _preload_lock:
        _cfg_cache["text"] = text
        _cfg_cache["at"] = now
    return text


def _parse_g2r_streams(text):
    """从 YAML 原文里抽出 streams 段，返回 {流名: 源}。

    两种写法都要认 —— go2rtc 自己会把单条源改写成列表形式，所以列表不是可选的：

        ktcam: xiaomi://...            # 人手写的单条源
        ktcam:                         # PUT /api/streams 写回后的样子
          - xiaomi://...

    一个流挂多条源时取第一条：这里只用它反查设备地址、判断会话归属，
    多源属于人工配置的高级用法，自动链路不在上面做判断。
    """
    out = {}
    if not text:
        return out
    in_streams = False
    pending = None          # 刚读完一行"只有名字、值在接下来几行"的流
    for line in text.splitlines():
        if re.match(r"^streams\s*:", line):
            in_streams = True
            continue
        if not in_streams:
            continue
        # 遇到下一个顶层键就结束
        if line.strip() and not line.startswith((" ", "\t")):
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if pending is not None:
            m = re.match(r"^\s*-\s*(.+)$", line)
            if m:
                out.setdefault(pending, m.group(1).strip().strip('"').strip("'"))
                continue
            pending = None
        m = re.match(r"^\s+[\"']?([A-Za-z0-9_\-.]+)[\"']?\s*:\s*(.*)$", line)
        if not m:
            continue
        name, val = m.group(1), m.group(2).strip()
        if val:
            out[name] = val.strip('"').strip("'")
        else:
            pending = name
    return out


def g2r_stream_sources():
    """go2rtc 的 {流名: 源}（从配置原文解析）。

    健康检查要用它按 did 反查摄像头的局域网地址，来判断"设备是没上电还是
    链路坏了"。
    """
    out = {}
    for text, _path in _g2r_config_sources():
        out.update(_parse_g2r_streams(text))
    return out


# ── 改流 / 查云侧地址（看门狗用）─────────────────────────────────────────
def patch_stream_src(name, src, timeout=20):
    """替换 go2rtc 里一条流的源，立刻生效、不重启进程。

    为什么不用 POST /api/config：那个接口**只写文件不重载**，要生效只能调
    /api/restart（syscall.Exec 原地重启），那会把所有摄像头的 P2P 会话一起
    打断。PUT /api/streams 走的是 `New(name, src)` + `PatchConfig(["streams",
    name], src)`：内存里换掉这一条、磁盘上只动这一个键，别的流和 `xiaomi:`
    的 passToken 段原样保留（go2rtc 1.9.15 实测结论）。

    src 必须整体转义：它带 `?did=…&model=…`，`&` 不编码会被切成另一个参数
    （实测丢了 model 参数）。
    """
    query = urllib.parse.urlencode({"name": name, "src": src})
    _g2r_raw("PUT", "/api/streams?" + query, timeout=timeout)
    _cfg_cache.clear()      # 配置刚变，别再用 30 秒缓存里的旧地址
    return True


def xiaomi_accounts():
    """go2rtc 配置里 `xiaomi:` 段的账号 ID 列表（查云侧设备表要用）。"""
    out = []
    in_block = False
    for line in (_g2r_config_text() or "").splitlines():
        if re.match(r"^xiaomi\s*:", line):
            in_block = True
            continue
        if not in_block:
            continue
        if line.strip() and not line.startswith((" ", "\t")):
            break
        m = re.match(r"^\s+[\"']?([^\"':\s]+)[\"']?\s*:", line)
        if m:
            out.append(m.group(1))
    return out


def xiaomi_cloud_devices(user, region="cn", timeout=25):
    """米家云侧的设备表：`{did: {"ip":..,"name":..,"url":..}}`。

    这是 IP 漂移**唯一**的判据。go2rtc 源里绑的是写配置那一刻的局域网地址，
    而 tutk 直接对那个地址发 UDP（pkg/tutk/conn.go:13），设备重新 DHCP 之后
    地址错了就连不通 —— 只会每 5 秒无限重试，永不自愈。云侧的 localip 是活的。
    """
    query = "/api/xiaomi?id=%s&region=%s" % (
        urllib.parse.quote(str(user or "")), urllib.parse.quote(str(region or "cn")))
    try:
        rows = _g2r_call(query, timeout=timeout)
    except Exception:
        return {}
    out = {}
    for d in (rows or {}).get("sources") or []:
        url = d.get("url") or ""
        try:
            parsed = urllib.parse.urlparse(url)
            did = (urllib.parse.parse_qs(parsed.query).get("did") or [""])[0]
        except ValueError:
            did = ""
        m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", d.get("info") or "")
        if did:
            out[did] = {"ip": m.group(1) if m else "", "name": d.get("name") or "",
                         "url": url}
    return out



def _stream_base_name(src):
    """`ffmpeg:ktcam#video=h264` -> `ktcam`；不是转码流就返回 None。"""
    if not src.startswith("ffmpeg:"):
        return None
    rest = src[len("ffmpeg:"):]
    name = re.split(r"[#?]", rest, 1)[0].strip()
    return name or None


def _match_stream_in(text, did):
    """在配置文本里找能代表这台摄像头的流。

    优先挑 H.264 的（浏览器能解、录像能直接 copy）；
    `ktcam` 这种 H.265 原始流只作为找不到 H.264 时的备选。
    """
    streams = _parse_g2r_streams(text)
    if not streams:
        return None

    def base_of(name, depth=0):
        """顺着 ffmpeg: 引用找到最底层的源。"""
        if depth > 4:
            return None, None
        src = streams.get(name)
        if src is None:
            return None, None
        b = _stream_base_name(src)
        if b and b in streams:
            deep_src, deep_name = base_of(b, depth + 1)
            return (deep_src or src), (deep_name or b)
        return src, name

    h264 = []
    raw = []
    for name in streams:
        src, base = base_of(name)
        if not src or ("did=%s" % did) not in src:
            continue
        item = {"name": name, "url": src, "base": base}
        # 转码流里指定了 h264 输出的算 H.264
        if "h264" in src or re.search(r"_(h264|play)$", name):
            h264.append(item)
        else:
            raw.append(item)

    if h264:
        return h264[0]
    if raw:
        return raw[0]
    return None


def g2r_rtsp_base():
    """go2rtc 的 RTSP 监听地址。"""
    for text, _ in _g2r_config_sources():
        m = re.search(r"^rtsp:\s*\n\s+listen:\s*(\S+)", text, re.MULTILINE)
        if m:
            port = m.group(1).strip().strip('"').strip("'")
            if port.startswith(":"):
                return "rtsp://127.0.0.1" + port
            if ":" in port:
                return "rtsp://" + port
    return "rtsp://127.0.0.1:8555"


def migrate_to_go2rtc():
    """把小米摄像头改成走 go2rtc 取流。

    为什么要这一步：小米摄像头的 P2P 并发会话上限只有 3 条，而云台/对讲命令
    必须跑在某条已建立的会话上。如果 HomeEye 用官方 SDK 取流（占 1 条）、
    侧车再开一条云台会话（占 1 条）、go2rtc 再占 1 条，就正好 3 条顶满，
    任何一方重连失败就会互相把对方踢掉（cs2: EOF）。

    改成走 go2rtc 取流后，这台摄像头全场只占 1 条会话（go2rtc 的），
    云台/对讲复用它，取流也用它——彻底不占额外名额。
    """
    cfg = C.load()
    cams = cfg.get("cameras") or []
    changed = []
    skipped = []
    base = g2r_rtsp_base()

    for cam in cams:
        if (cam.get("type") or "").lower() != "xiaomi":
            skipped.append((cam.get("id"), "不是小米类型"))
            continue
        did = str(cam.get("did") or "")
        if not did:
            skipped.append((cam.get("id"), "没有 DID"))
            continue
        hit = find_g2r_stream(did)
        if not hit:
            skipped.append((cam.get("id"), "go2rtc 配置里没找到这台摄像头"))
            continue
        cam["type"] = "rtsp"
        cam["url"] = "%s/%s" % (base, hit["name"])
        cam["ptz_stream"] = hit["name"]
        changed.append((cam.get("id"), cam.get("name"), cam["url"]))

    if changed:
        cfg["cameras"] = cams
        ptz = dict(cfg.get("ptz") or {})
        ptz.update({"enabled": True, "mode": "go2rtc"})
        cfg["ptz"] = ptz
        C.save(cfg)
    return changed, skipped


def import_from_go2rtc():
    """把 go2rtc 里的米家凭据写进 HomeEye 配置。"""
    hit = detect_go2rtc()
    if not hit:
        return False, "未找到 go2rtc 配置（或其中没有 xiaomi 账号）"
    path, user, token = hit
    cfg = C.load()
    ptz = dict(cfg.get("ptz") or {})
    ptz.update({"enabled": True, "user": user, "token": token})
    cfg["ptz"] = ptz
    C.save(cfg)
    restart()
    return True, "已从 %s 导入（账号 %s）" % (path, user)


# ── 子进程管理 ────────────────────────────────────────────────────────
def _spawn():
    global _proc, _last_error
    binp = binary()
    if not binp:
        _last_error = "缺少 heptz 可执行文件"
        return None
    c = _cfg()
    env = dict(os.environ)
    env.update({
        "HEPTZ_USER": str(c.get("user") or ""),
        "HEPTZ_TOKEN": str(c.get("token") or ""),
        "HEPTZ_REGION": str(c.get("region") or "cn"),
        "HEPTZ_ADDR": addr(),
        # 下行音频默认关（069a01 在子码流下开音频会把整条会话顶掉）；
        # 想用「音量回采」自检喇叭有没有响，可以配 ptz.media_audio=true。
        "HEPTZ_MEDIA_QUALITY": "2" if c.get("media_audio") else "1",
        "HEPTZ_MEDIA_AUDIO": "1" if c.get("media_audio") else "0",
        # 摄像头 P2P 会话上限实测 3 条，HomeEye 自己取流就占 1 条，
        # 侧车必须闲置即释放，否则会和取流互相把对方踢掉（cs2: EOF）。
        "HEPTZ_IDLE_SECONDS": str(int(c.get("idle_seconds") or 45)),
    })
    try:
        os.chmod(binp, 0o755)
    except OSError:
        pass
    # 侧车日志落到数据目录：它平时很安静，出问题时这几行是唯一线索
    # （之前直接丢 /dev/null，等于把现场证据扔了）。
    logf = None
    try:
        os.makedirs(C.PKGVAR, exist_ok=True)
        logf = open(os.path.join(C.PKGVAR, "heptz.log"), "ab", buffering=0)
        logf.write(("\n=== %s start ===\n" % time.strftime("%Y-%m-%d %H:%M:%S")).encode())
    except OSError:
        logf = None
    try:
        p = subprocess.Popen(
            [binp],
            env=env,
            stdout=logf or subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            close_fds=True,
        )
    except Exception as e:
        _last_error = "启动 heptz 失败: %s" % e
        return None
    finally:
        if logf is not None:
            try:
                logf.close()
            except Exception:
                pass
    _last_error = ""
    return p


def ensure_running():
    """确保侧车在跑；进程意外退出时按需重启。"""
    global _proc
    with _lock:
        if backend() == "go2rtc":
            # go2rtc 后端不需要侧车：命令直接在 go2rtc 已有的会话上下发
            return None
        if not enabled():
            return None
        if _proc is not None and _proc.poll() is None:
            return _proc
        _proc = _spawn()
        if _proc is not None:
            # 给进程一点时间绑端口
            for _ in range(20):
                if _healthy():
                    break
                time.sleep(0.15)
        return _proc


def restart():
    global _proc
    with _lock:
        if _proc is not None:
            try:
                _proc.terminate()
                _proc.wait(timeout=3)
            except Exception:
                try:
                    _proc.kill()
                except Exception:
                    pass
        _proc = None
    return ensure_running()


def shutdown():
    global _proc
    with _lock:
        if _proc is not None:
            try:
                _proc.terminate()
                _proc.wait(timeout=3)
            except Exception:
                try:
                    _proc.kill()
                except Exception:
                    pass
        _proc = None


# ── HTTP 到侧车 ───────────────────────────────────────────────────────
def _request(path, timeout=8):
    host, _, port = addr().partition(":")
    conn = http.client.HTTPConnection(host or "127.0.0.1", int(port or 8899), timeout=timeout)
    try:
        conn.request("GET", path)
        res = conn.getresponse()
        body = res.read()
        if res.status != 200:
            raise RuntimeError(body.decode("utf-8", "replace")[:200])
        return json.loads(body.decode("utf-8"))
    finally:
        conn.close()


def _post(path, body=None, timeout=8):
    host, _, port = addr().partition(":")
    conn = http.client.HTTPConnection(host or "127.0.0.1", int(port or 8899), timeout=timeout)
    try:
        conn.request("POST", path, body=body or b"",
                     headers={"Content-Type": "application/octet-stream"})
        res = conn.getresponse()
        data = res.read()
        if res.status != 200:
            raise RuntimeError(data.decode("utf-8", "replace")[:200])
        try:
            return json.loads(data.decode("utf-8"))
        except ValueError:
            return {"ok": True, "raw": data.decode("utf-8", "replace")[:200]}
    finally:
        conn.close()


def _healthy():
    try:
        _request("/health", timeout=2)
        return True
    except Exception:
        return False


def _g2r_auth_headers():
    """go2rtc 的 BasicAuth 头（配置里没填凭据就返回空 dict）。

    go2rtc 从 1.x 起支持 api.username / api.password。我们把它的 API 收到
    127.0.0.1 上，但**光收地址不算防护** —— go2rtc 的 local_auth 默认关闭，
    环回请求会直接跳过鉴权；反过来一旦宿主上别的进程访问它也是"合法"的。
    所以凭据要真的配上、这里要真的带上。
    """
    c = _cfg()
    user = (c.get("g2r_user") or "").strip()
    if not user:
        return {}
    raw = ("%s:%s" % (user, c.get("g2r_pass") or "")).encode("utf-8")
    return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}


def _g2r_request(method, path, body=None, timeout=15, ctype=None):
    """向 go2rtc 发一次请求，成功返回响应字节，失败抛 RuntimeError（带状态码）。"""
    base = g2r_api()
    hostport = base.split("//", 1)[-1]
    host, _, port = hostport.partition(":")
    conn = http.client.HTTPConnection(host or "127.0.0.1", int(port or 1984),
                                      timeout=timeout)
    try:
        headers = dict(_g2r_auth_headers())
        if body is not None:
            headers["Content-Type"] = ctype or "application/octet-stream"
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        data = res.read()
        if res.status != 200:
            text = data.decode("utf-8", "replace")[:200]
            if res.status in (401, 403):
                text = ("go2rtc 拒绝了请求（HTTP %d）：设置页里的 go2rtc 用户名/密码"
                        "和它自己的 api.username/password 不一致" % res.status)
            raise RuntimeError("go2rtc %d: %s" % (res.status, text))
        return data
    finally:
        conn.close()


def _g2r_call(path, body=None, method=None, timeout=15):
    """调用 go2rtc 的云台接口，返回 JSON（非 JSON 的按 {"ok": true} 处理）。"""
    data = _g2r_request(method or ("POST" if body is not None else "GET"),
                        path, body=body, timeout=timeout)
    try:
        return json.loads(data.decode("utf-8"))
    except ValueError:
        return {"ok": True}


def _g2r_raw(method, path, timeout=15, body=None):
    """调用 go2rtc 的非 JSON 接口（preload 之类），返回原始文本。"""
    return _g2r_request(method, path, body=body, timeout=timeout).decode(
        "utf-8", "replace")


# ── 按需建立 / 释放 go2rtc 会话 ────────────────────────────────────────
# 摄像头 P2P 会话上限只有 3 条，所以「云台要会话」这件事必须做到：
#   需要时按需拉起一条，不用了立刻还回去。
#
# go2rtc 自带 preload 机制（api/preload）：给某个流挂一个常驻消费者，
# producer 就会一直保持连接。用完 DELETE 掉，producer 随之停止，
# P2P 会话当场释放。实测：无人看画面时 preload 能在 2 秒内建好会话，
# 云台/对讲命令立刻可用；DELETE 后连接数回到 0。
#
# 关键点：preload 挂在**同一条** producer 上（不是新开一条会话），
# 所以哪怕画面正在播，preload 也不会多占名额 —— 但我们仍然在用完后
# 删掉它，避免"没人看画面却常驻一条会话"。
# （_preload_lock / _preloaded 定义在文件顶部，这里直接用。）


def _g2r_sessions():
    """go2rtc 当前活跃的 miss 会话表 {did: {...}}。"""
    try:
        return _g2r_call("/api/xiaomi/ptz", timeout=6) or {}
    except Exception:
        return {}


def g2r_session_ready(did):
    """这台摄像头现在有没有可用的取流会话。"""
    try:
        return did in _g2r_sessions()
    except Exception:
        return False


def ensure_g2r_session(did, timeout=25.0):
    """确保这台摄像头有一条可用的取流会话，返回是否成功。

    优先复用已经存在的会话（有人在看画面时就是这样）；
    没有就借 go2rtc 的 preload 按需建一条，建好后记住它，用完释放。
    """
    if g2r_session_ready(did):
        return True

    with _preload_lock:
        hit = _preloaded.get(did)

    if not hit:
        stream = find_g2r_session_stream(did)
        if not stream:
            return False
        try:
            # video 参数 = 挂一个视频消费者，保持 producer 活着
            _g2r_raw("PUT", "/api/preload?src=%s&video" % stream, timeout=20)
        except Exception:
            return False
        with _preload_lock:
            _preloaded[did] = {"stream": stream, "at": time.time()}
        hit = _preloaded[did]

    # 等会话真的建立（preload 是异步的，实测 2 秒内）
    deadline = time.time() + timeout
    while time.time() < deadline:
        if g2r_session_ready(did):
            return True
        time.sleep(0.6)
    return False


def release_g2r_session(did):
    """把这条按需建立的会话还回去（preload 删掉 -> producer 停 -> 会话释放）。"""
    with _preload_lock:
        hit = _preloaded.pop(did, None)
    if not hit:
        return False
    try:
        _g2r_raw("DELETE", "/api/preload?src=%s" % hit["stream"], timeout=15)
        return True
    except Exception:
        return False


def release_all_g2r_sessions():
    with _preload_lock:
        dids = list(_preloaded.keys())
    for did in dids:
        release_g2r_session(did)


# ── 自动停的租约 ──────────────────────────────────────────────────────
# 按住方向盘时前端会每 0.3 秒续一条命令，松手立刻补一条 op=0。这里给每路
# 摄像头**只留一条**看护线程：命令到了就延后到期时间，到期没续上再发停止。
#
# 以前是「每条命令各起一个 hold 秒后停止的线程」，按住时停止命令和转动命令
# 交错下发，云台一顿一顿地挪，看着就像没反应 —— 这是必须修掉的那一版。
_stop_lock = threading.RLock()
_stop_leases = {}        # did -> {"until": 到期时刻}


def _send_motor(did, op, timeout=8):
    if backend() == "go2rtc":
        return _g2r_call("/api/xiaomi/motor?did=%s&op=%d" % (did, int(op)),
                         timeout=timeout)
    return _request("/ptz?cam=%s&op=%d" % (did, int(op)))


def _stop_worker(did):
    while True:
        with _stop_lock:
            st = _stop_leases.get(did)
            if st is None:
                return
            left = st["until"] - time.time()
        if left > 0:
            time.sleep(min(left, 0.5))
            continue
        with _stop_lock:
            st = _stop_leases.get(did)
            if st is None:
                return
            if st["until"] > time.time():
                continue
            _stop_leases.pop(did, None)
        try:
            _send_motor(did, 0)
        except Exception:
            pass
        return


def _lease(did, hold):
    now = time.time()
    with _stop_lock:
        st = _stop_leases.get(did)
        if st is not None:
            st["until"] = now + float(hold)
            return
        _stop_leases[did] = {"until": now + float(hold)}
    threading.Thread(target=_stop_worker, args=(did,), daemon=True).start()


def _lease_cancel(did):
    with _stop_lock:
        _stop_leases.pop(did, None)


def move(cam_id, op, hold=None):
    """转动云台。op: 0 停 / 1 右 / 2 左 / 3 上 / 4 下。

    `hold` 为按住时长（秒）：在到期前不断续租，松手（op=0）或浏览器掉线
    都会让这条命令停下来。
    """
    ok, why = available()
    if not ok:
        raise PtzError(why)
    cam = C.get_camera(cam_id)
    if not cam:
        raise PtzError("摄像头不存在: %s" % cam_id)
    did = str(cam.get("did") or "")
    if not did:
        raise PtzError("该摄像头没有 DID，仅小米摄像头支持云台")

    if backend() == "go2rtc":
        # 会话不在就按需建一条（借 go2rtc 的 preload），建好后命令立刻可用。
        # 这样用户不用先"打开这路画面"才能转云台。
        if not ensure_g2r_session(did):
            raise PtzError(
                "无法建立这路摄像头的取流会话，请确认设备在线",
                need_session=True)
        # 刷新闲置计时：正在用云台时不能被回收线程收走
        with _preload_lock:
            if did in _preloaded:
                _preloaded[did]["at"] = time.time()

    try:
        if backend() != "go2rtc":
            ensure_running()
        res = _send_motor(did, op)
    except Exception as e:
        if _is_no_session(e):
            raise PtzError("尚未建立取流会话，请稍后重试", need_session=True)
        raise PtzError(str(e))

    if not op:
        _lease_cancel(did)
    elif hold:
        _lease(did, hold)
    return res


# ── 对讲（上行语音）────────────────────────────────────────────────────
# 浏览器把麦克风采成 8kHz/16bit 单声道裸 PCM 送上来，由侧车转成 G.711 A-law
# 并按 40ms 一包写到 miss 的 channel3。我们只做搬运和限速，不做额外转码。

TALK_RATE = 8000


def _did_of(cam_id):
    cam = C.get_camera(cam_id)
    if not cam:
        raise PtzError("摄像头不存在: %s" % cam_id)
    did = str(cam.get("did") or "")
    if not did:
        raise PtzError("该摄像头没有 DID，仅小米摄像头支持对讲")
    return did


def talk_start(cam_id):
    ok, why = available()
    if not ok:
        raise PtzError(why)
    did = _did_of(cam_id)
    if backend() == "go2rtc":
        # 同样按需建会话：用户不用先打开画面就能开麦说话
        if not ensure_g2r_session(did):
            raise PtzError("无法建立这路摄像头的取流会话，请确认设备在线",
                            need_session=True)
        # go2rtc 已经在同一条会话上开好了喇叭通道
        try:
            res = _g2r_call("/api/xiaomi/speaker?did=%s" % did, timeout=20)
        except Exception as e:
            if _is_no_session(e):
                raise PtzError("尚未建立取流会话，请稍后重试", need_session=True)
            raise PtzError(str(e))
        _talking[did] = time.time()
        return res
    ensure_running()
    # 打开喇叭通道要三步握手（0x106 -> 0x107），给足超时
    try:
        return _post("/talk/start?cam=%s" % did, timeout=20)
    except Exception as e:
        raise PtzError(str(e))


def talk_push(cam_id, pcm):
    """推一段 16bit/8kHz 单声道 PCM。返回统计。"""
    if not pcm:
        return {"ok": True, "frames": 0}
    did = _did_of(cam_id)
    if backend() == "go2rtc":
        # go2rtc 侧负责转 A-law 并切帧
        try:
            return _g2r_call("/api/xiaomi/audio?did=%s" % did, body=pcm, timeout=10)
        except Exception as e:
            if _is_no_session(e):
                raise PtzError("尚未建立取流会话，对讲无法发送", need_session=True)
            raise PtzError(str(e))
    try:
        return _post("/talk?cam=%s" % did, body=pcm, timeout=10)
    except Exception as e:
        raise PtzError(str(e))


def talk_stop(cam_id):
    did = _did_of(cam_id)
    if backend() == "go2rtc":
        try:
            res = _g2r_call("/api/xiaomi/speaker?did=%s&stop=1" % did, timeout=10)
        except Exception as e:
            # 关闭通道失败不致命（会话可能已经断了），别把异常抛给前端
            res = {"ok": False, "error": str(e)}
        _talking.pop(did, None)
        # 对讲结束：如果这条会话是我们按需建的，没人看了就还回去
        if not g2r_session_ready(did):
            release_g2r_session(did)
        return res
    try:
        return _post("/talk/stop?cam=%s" % did, timeout=10)
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── 会话闲置回收 ──────────────────────────────────────────────────────
# 按需建立的会话不能一直占着摄像头名额（上限 3 条），闲置超时就得还回去。
# 回收线程由 nw_api 在启动时拉起。
def _idle_reaper():
    while True:
        time.sleep(5.0)
        try:
            timeout = float(_cfg().get("idle_seconds") or 45)
        except Exception:
            timeout = 45.0
        now = time.time()
        with _preload_lock:
            stale = [did for did, hit in _preloaded.items()
                     if now - hit.get("at", now) > timeout]
        for did in stale:
            # 还在对讲中就别收（会把用户的语音掐断）
            if _g2r_sessions().get(did, {}).get("session") and _talking.get(did):
                with _preload_lock:
                    if did in _preloaded:
                        _preloaded[did]["at"] = now
                continue
            release_g2r_session(did)


def make_beep(freqs=(800, 1200, 1600), dur=0.6, gap=0.4, amp=20000):
    """生成一串测试提示音（PCM16 / 8kHz / 单声道）。"""
    out = bytearray()
    for f in freqs:
        n = int(TALK_RATE * dur)
        for i in range(n):
            out += struct.pack("<h", int(amp * math.sin(2 * math.pi * f * i / TALK_RATE)))
        out += b"\x00\x00" * int(TALK_RATE * gap)
    return bytes(out)


def beep(cam_id):
    """播一段提示音，用来确认摄像头喇叭真的会响。后台跑，立即返回。"""
    ok, why = available()
    if not ok:
        raise RuntimeError(why)
    did = _did_of(cam_id)

    def _run():
        try:
            talk_start(cam_id)
            time.sleep(0.8)
            pcm = make_beep()
            step = TALK_RATE * 2 // 10   # 0.1 秒一片
            t0 = time.time()
            sent = 0
            for i in range(0, len(pcm), step):
                talk_push(cam_id, pcm[i:i + step])
                sent += step
                drift = sent / float(TALK_RATE * 2) - (time.time() - t0)
                if drift > 0:
                    time.sleep(drift)
            time.sleep(0.3)
            talk_stop(cam_id)
        except Exception as e:
            sys.stderr.write("[ptz] beep failed: %s\n" % e)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "cam": did, "seconds": round(len(make_beep()) / (TALK_RATE * 2.0), 2)}


_ANGLE_RE = re.compile(r'"angle":\s*(-?\d+)')
_ELEV_RE = re.compile(r'"elevation":\s*(-?\d+)')


def parse_angle(raw):
    """从 cmdMotorRes 的 JSON 里取 (angle, elevation)。"""
    a = _ANGLE_RE.search(raw or "")
    e = _ELEV_RE.search(raw or "")
    return (int(a.group(1)) if a else None, int(e.group(1)) if e else None)


def status():
    """返回 {cam_id: {state, angle, elevation, ...}}，按 HomeEye 的摄像头 id 归并。"""
    ok, why = available()
    if not ok:
        return {"available": False, "reason": why, "cams": {}}

    if backend() == "go2rtc":
        return _status_g2r()

    try:
        ensure_running()
        raw = _request("/status", timeout=6)
    except Exception as e:
        return {"available": True, "error": str(e), "cams": {}}

    out = {}
    for cam in C.load().get("cameras") or []:
        did = str(cam.get("did") or "")
        if not did:
            continue
        info = raw.get(did) or {}
        angle, elev = parse_angle(info.get("motorRes") or "")
        out[str(cam.get("id"))] = {
            "state": info.get("state", "idle"),
            "online": info.get("online"),
            "angle": angle,
            "elevation": elev,
            "model": info.get("model", ""),
            "talking": bool(info.get("talking")),
            "talkFrames": info.get("talkFrames") or 0,
            "talkOut": info.get("talkOut") or 0,
            "audioLevel": info.get("audioLevel"),
            "audioPeak": info.get("audioPeak"),
            "error": info.get("error"),
        }
    return {"available": True, "cams": out}


def _status_g2r():
    """go2rtc 后端的状态。

    go2rtc 那边只有「这台摄像头当前有没有活跃的 miss 会话」这一个信号。
    没有会话不代表不可用 —— HomeEye 可以按需建一条（借 go2rtc 的 preload），
    所以这里标成 "idle" 并说明"可按需建立"，前端据此提示用户。
    """
    try:
        raw = _g2r_call("/api/xiaomi/ptz", timeout=6)
    except Exception as e:
        return {"available": True, "error": str(e), "cams": {}}

    out = {}
    for cam in C.load().get("cameras") or []:
        did = str(cam.get("did") or "")
        if not did:
            continue
        info = raw.get(did) or {}
        live = bool(info.get("session"))
        out[str(cam.get("id"))] = {
            "state": "connected" if live else "idle",
            "online": None,
            # 摄像头回传的**真实朝向**（cmdMotorRes 0x113）
            "angle": info.get("angle"),
            "elevation": info.get("elevation"),
            "model": cam.get("model", "") or cam.get("name", ""),
            "talking": bool(_talking.get(did)),
            "talkFrames": info.get("talkFrames") or 0,
            "talkOut": info.get("talkBytes") or 0,
            "codec": info.get("codec"),
            # 没有会话时仍可云台/对讲：后端会按需建一条
            "error": None,
            "hint": None if live else "会话未建立，操作时会自动建立",
        }
    return {"available": True, "cams": out}
