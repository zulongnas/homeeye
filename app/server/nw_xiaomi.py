# -*- coding: utf-8 -*-
"""HomeEye · 小米摄像头接入（Miloco SDK 桥接）

把小米 Miloco SDK（asyncio 回调）桥接到 HomeEye 的同步 HTTP 服务。

## 架构
  * **OAuth2 扫码登录**：拆成两步 ——
      1) get_qr() 调小米 loginUrl 接口拿「二维码 URL + 长轮询地址 lp」；
      2) wait_code() 在后台线程长轮询 lp，用户手机扫码后拿 userId/location，
         再完成 user_authorization + 换 access_token，存盘。
    （原 SDK 的 get_code_url 是同步阻塞 120 秒，这里拆开以便网页显示二维码。）
  * **拉流**：run_stream(did, channel, on_raw_video) 是 asyncio 协程，回调吐
    Annex-B NAL。起独立线程跑专属 event loop，回调把 NAL 写进 ffmpeg stdin，
    ffmpeg `-f h264|hevc -i pipe:0 -c:v copy -f mpegts` 转封装，再复用
    nw_live.LiveHub 的扇出广播给前端（前端零改动）。

依赖：NAS Linux + python3.12 + /tmp/miloco-sdk-main（Windows 本地无法真机取流）。
"""
import asyncio
import collections
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.parse

SDK_PATH = os.environ.get("HE_MILOCO_SDK") or os.path.join(
    os.environ.get("TRIM_APPDEST") or "/vol1/@appcenter/homeeye", "sdk", "miloco-sdk-main")


def _data_root():
    """数据根目录：必须和 nw_config.PKGVAR 一致。

    Docker 版没有 TRIM_PKGVAR，只有 HE_DATA_DIR=/data；如果这里继续硬编码
    /vol1/@appdata/homeeye，容器里就会读到一个不存在的目录，表现为
    「明明挂载了 auth.json，却一直 has_token=false、拉不了流」。
    """
    try:
        import nw_config as _C
        return _C.PKGVAR
    except Exception:
        return (os.environ.get("TRIM_PKGVAR")
                or os.environ.get("HE_DATA_DIR")
                or "/vol1/@appdata/homeeye")


# 登录态必须持久化：放数据目录（/tmp 会在重启后清空，表现为"重启就要重新扫码"）
AUTH_DIR = os.environ.get("HE_MILOCO_AUTH") or os.path.join(_data_root(), "xiaomi_auth")

# 小米 OAuth2 常量（与 SDK 一致）
OAUTH2_CLIENT_ID = "2882303761520431603"
MICO_REDIRECT_URI = "https://mico.api.mijia.tech/login_redirect"


def _ensure_sdk_cwd():
    """SDK 的 DATA_PATH = os.getcwd()/"data"，且 import 时就 os.makedirs。

    若服务的工作目录不可写（飞牛以应用账号启动时 cwd 常在 root 拥有的 target 下），
    import SDK 会直接 PermissionError。这里统一切到可写的认证目录再导入。
    """
    try:
        os.makedirs(AUTH_DIR, exist_ok=True)
        os.chdir(AUTH_DIR)
    except Exception:
        pass


def _sdk():
    """惰性导入 SDK（只在 Linux 上有意义）。"""
    if SDK_PATH not in sys.path:
        sys.path.insert(0, SDK_PATH)
    _ensure_sdk_cwd()
    from miloco_sdk import XiaomiClient
    from miloco_sdk.plugin.authorize import OAUTH2_CLIENT_ID as _CID
    return XiaomiClient, _CID


def xiaomi_available():
    if sys.platform.startswith("win"):
        return False, "小米接入仅在 NAS(Linux) 上运行"
    try:
        XiaomiClient, _ = _sdk()
        return True, ""
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------- 授权状态机
class XiaomiAuth(object):
    """OAuth2 登录：idle -> pending(已出二维码) -> authorized / error。"""

    def __init__(self):
        self.state = "idle"
        self.qr_url = ""
        self.error = ""
        self.token = None
        self._client = None          # XiaomiClient 实例
        self._login_json = None      # loginUrl 接口返回（含 loginUrl + lp）
        self._lock = threading.RLock()
        self._waiter = None
        # 启动时立刻回填已保存的登录态：否则服务重启后，只要没人再点"生成二维码"
        # （唯一会触发 ensure_client 的入口），snapshot 就一直报未登录、拉不了流。
        self._load_token()
        if self.token:
            self.state = "authorized"

    def _load_token(self):
        try:
            p = os.path.join(AUTH_DIR, "auth.json")
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if d.get("access_token"):
                    self.token = d["access_token"]
                    return True
        except Exception:
            pass
        return False

    def _save_token(self, auth_info):
        try:
            os.makedirs(AUTH_DIR, exist_ok=True)
            with open(os.path.join(AUTH_DIR, "auth.json"), "w", encoding="utf-8") as f:
                json.dump(auth_info, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def ensure_client(self):
        """确保 XiaomiClient 已创建（带已保存 token）。

        关键：client 必须在单例 loop 的上下文里创建，否则 SDK 内部的
        _main_loop 会指向一个随后被关闭的临时 loop。
        """
        if self._client is not None:
            return self._client
        XiaomiClient, _ = _sdk()
        self._load_token()

        # 死锁防线：如果当前正处于单例 loop 的线程里，再调 run_coroutine_threadsafe()
        # 向同一个 loop 提交任务并 .result() 等待，就会「loop 等自己」永远卡死
        # （表现为 start() 成功、alive=true，但 frames 永远 0 且无任何 SDK 日志）。
        # 此时改为就地同步构造。
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is not None and current is _LOOP.get():
            self._client = XiaomiClient(access_token=self.token)
            return self._client

        async def _mk():
            return XiaomiClient(access_token=self.token)

        self._client = _LOOP.run(_mk(), timeout=90)
        return self._client

    def snapshot(self):
        with self._lock:
            return {"state": self.state, "qr_url": self.qr_url,
                    "error": self.error, "has_token": bool(self.token)}

    # ---- 第一步：拿二维码 ----
    def get_qr(self):
        """调小米接口拿二维码 URL 和长轮询地址。返回 (ok, qr_url, err)。"""
        client = self.ensure_client()
        auth_url = "https://account.xiaomi.com/oauth2/authorize"
        params = {
            "pt": "0", "skip_confirm": False, "response_type": "code",
            "redirect_uri": MICO_REDIRECT_URI, "client_id": OAUTH2_CLIENT_ID,
            "device_id": client._device_id, "state": client._state,
            "scope": "1 3 6000", "scope_provider": "",
            "_locale": "zh_CN", "_json": "true",
        }
        try:
            auth_res = client._http.get(auth_url, params=params, allow_redirects=False)
            auth_json = json.loads(auth_res.text.split("&&&START&&&")[1])
        except Exception as e:
            with self._lock:
                self.state, self.error = "error", "获取授权失败：%s" % e
            return False, "", self.error

        # 兼容两种响应形态：正常会带 callback/sid/lsrp_appName/scope；
        # 已登录态或接口变体可能缺字段，一律用 .get 兜底，避免 KeyError。
        data0 = auth_json.get("data") or {}
        scopes = list((data0.get("scope") or {}).values())
        params2 = {
            "sid": data0.get("sid", ""),
            "lsrp_appName": data0.get("lsrp_appName", ""),
            "_customDisplay": "20", "scope": "1 3 6000",
            "client_id": OAUTH2_CLIENT_ID, "_locale": "zh_CN",
            "callback": data0.get("callback", ""),
            "serviceParam": '{"checkSafePhone":false,"checkSafeAddress":false,"lsrp_score":0.0}',
            "showActiveX": "false", "theme": "", "needTheme": "false",
            "bizDeviceType": "", "scopes": json.dumps(scopes),
            "_hasLogo": "false", "_qrsize": "240",
            "_dc": str(int(time.time() * 1000)),
        }
        try:
            login_res = client._http.get(
                "https://account.xiaomi.com/longPolling/loginUrl",
                params=params2, timeout=15)
            login_json = json.loads(login_res.text.split("&&&START&&&")[1])
        except Exception as e:
            with self._lock:
                self.state, self.error = "error", "获取二维码失败：%s" % e
            return False, "", self.error

        with self._lock:
            self._login_json = login_json
            self.qr_url = login_json.get("loginUrl", "")
            self.state = "pending"
        # 起后台线程长轮询等扫码
        self._waiter = threading.Thread(target=self._wait_loop, daemon=True)
        self._waiter.start()
        return True, self.qr_url, ""

    # ---- 第二步：长轮询等扫码 ----
    def _wait_loop(self):
        try:
            client = self.ensure_client()
            login_json = self._login_json
            lp_res = client._http.get(login_json["lp"], timeout=125)
            lp_json = json.loads(lp_res.text.split("&&&START&&&")[1])
            # 请求 /sts/oauth 拿 cookies
            client._http.get(lp_json["location"], allow_redirects=False)
            # 授权给米家 app
            self._user_authorization(client)
            # 换 code -> token
            code_url = self._get_code_url_after_auth(client, lp_json)
            code = urllib.parse.parse_qs(urllib.parse.urlparse(code_url).query)["code"][0]
            auth_info = client.authorize.get_access_token_from_mico(code)["result"]
            auth_info["created_at"] = int(time.time())
            self._save_token(auth_info)
            with self._lock:
                self.token = auth_info["access_token"]
                self.state = "authorized"
                self.error = ""
        except Exception as e:
            with self._lock:
                self.state = "error"
                self.error = "扫码登录失败：%s" % e

    def _user_authorization(self, client):
        """复刻 SDK authorize.user_authorization（把 code 授权给米家）。"""
        auth_url = "https://account.xiaomi.com/oauth2/authorize"
        params = {
            "skip_confirm": False, "response_type": "code",
            "redirect_uri": MICO_REDIRECT_URI, "state": client._state,
            "client_id": int(OAUTH2_CLIENT_ID), "device_id": client._device_id,
            "_locale": "zh_CN", "_json": "true",
        }
        auth_res = client._http.get(auth_url, params=params, allow_redirects=False)
        auth_json = json.loads(auth_res.text.split("&&&START&&&")[1])
        url = "https://account.xiaomi.com/oauth2/userAuthorization"
        data = {
            "pt": auth_json["data"]["pt"],
            "device_id": hashlib.md5(client._device_id.encode("utf-8")).hexdigest(),
            "followup": auth_json["data"]["followup"],
            "scope_id": auth_json["data"]["scope_id"],
            "redirect_uri": MICO_REDIRECT_URI,
            "client_id": OAUTH2_CLIENT_ID, "_json": "true",
            "_ssign": auth_json["data"]["_ssign"],
        }
        client._http.post(url, data=urllib.parse.urlencode(data),
                          allow_redirects=True,
                          headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})

    def _get_code_url_after_auth(self, client, lp_json):
        auth_url = "https://account.xiaomi.com/oauth2/authorize"
        params = {
            "redirect_uri": "https://mico.api.mijia.tech/login_redirect",
            "client_id": OAUTH2_CLIENT_ID, "response_type": "code",
            "device_id": client._device_id, "state": client._state,
            "skip_confirm": "False", "_locale": "zh_CN",
            "scope": "1 3 6000", "userId": str(lp_json["userId"]),
            "_from_user_authorize": "true", "confirmed": "true",
        }
        response = client._http.get(auth_url, params=params, allow_redirects=False)
        return response.headers["Location"]

    def reset(self):
        with self._lock:
            self.state = "idle"
            self.qr_url = ""
            self.error = ""


_auth = XiaomiAuth()


def get_auth():
    return _auth


# ---------------------------------------------------------------- 拉流桥接
# 小米 SDK 是 asyncio 回调模型。这里起独立线程 + 专属 event loop，把 on_raw_video
# 回调吐出的 Annex-B NAL 通过 ffmpeg stdin 转封装成 MPEG-TS，再复用 nw_live 的
# Subscriber 扇出机制广播给前端。前端零改动（还是 /live/<cam>.ts）。

def _ffmpeg_bin():
    return os.environ.get("HE_FFMPEG") or "ffmpeg"


def _to_int(v, d=0):
    try:
        return int(float(v))
    except Exception:
        return d


def _live_opts():
    """读取实时画面设置（转码宽度/帧率/CRF/是否转码）。

    直接读 nw_config，避免给 XiaomiStreamer 增加构造参数、也避免与 nw_api
    的设置热更新逻辑耦合（改完设置 drop_all() 重建 streamer 就会重新读一次）。
    """
    try:
        import nw_config as _C
        return (_C.load() or {}).get("live") or {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 全局"只允许一路会话在跑"的闸门
#
# SDK 的 miot_camera_stream 是 XiaomiClient 的**类属性**，多路并发 run_stream 会
# 互相覆盖内部状态。更麻烦的是：一台离线/连不上的摄像头会不停重试，每次重试都会
# 把**正在正常出流**的另一台踢掉，现象就是"客厅能看、一打开主卧全黑"或反过来。
#
# 这里用一个很轻的全局标记：谁最近真出过数据，谁就占着会话；别人想启动得等它
# 空出来（或等这个时间窗过期）。配合失败退避，离线摄像头就不会再反复踩别人。
# ---------------------------------------------------------------------------
_ACTIVE = {"cid": "", "t": 0.0}
_ACTIVE_LOCK = threading.Lock()
ACTIVE_WINDOW = 20.0        # 出过数据后，认为"还占着会话"的时间窗（秒）


def _touch_active(cid):
    with _ACTIVE_LOCK:
        _ACTIVE["cid"] = cid
        _ACTIVE["t"] = time.time()


def _active_other(cid):
    with _ACTIVE_LOCK:
        if (_ACTIVE["cid"] and _ACTIVE["cid"] != cid
                and (time.time() - _ACTIVE["t"]) < ACTIVE_WINDOW):
            return _ACTIVE["cid"]
    return ""


_ONLINE = {"t": 0.0, "map": {}}


def _online_map(max_age=60.0):
    """小米云侧的设备在线表（did -> isOnline），缓存 60 秒。

    离线设备直接跳过取流：既不白白占 P2P 会话，也不会因为反复重试把别的摄像头
    踢下线。取不到就沿用上一次结果，绝不因此阻断启动。
    """
    if _ONLINE["map"] and (time.time() - _ONLINE["t"]) < max_age:
        return _ONLINE["map"]
    try:
        client = _auth.ensure_client()
        client._access_token = _auth.token
        m = {}
        for d in (client.home.get_device_list() or []):
            did = str(d.get("did") or "")
            if did:
                m[did] = bool(d.get("isOnline"))
        if m:
            _ONLINE.update({"t": time.time(), "map": m})
    except Exception:
        pass
    return _ONLINE["map"]


def cloud_online_map():
    """对外的云在线表 {did: isOnline}；没登录/取不到时返回空表。

    健康检查用它区分「设备离线」和「链路故障」，所以取不到也不能抛异常 ——
    调用方按"未知"处理即可。
    """
    try:
        return dict(_online_map() or {})
    except Exception:
        return {}


class XiaomiLoop(object):
    """全局单例 asyncio 事件循环。

    小米 SDK 内部会把 client 与它创建时所在的 loop 强绑定（plugin/miot/lan.py 里
    用 self._main_loop.call_soon_threadsafe(...) 回调）。如果 client 在主线程创建、
    而每路流又各起一个临时 loop，就会出现：
        RuntimeError: Event loop is closed
        CS: PPCS_Connect errorcode: -3   （P2P 连不上，永远 0 字节）
    所以 client 的创建和 run_stream/wait_for_data 必须活在**同一个长期存活的 loop**。
    """

    def __init__(self):
        self._loop = None
        self._thread = None
        self._lock = threading.Lock()

    def get(self):
        with self._lock:
            if self._loop is not None:
                return self._loop
            loop = asyncio.new_event_loop()
            self._loop = loop
            self._thread = threading.Thread(
                target=self._serve, args=(loop,), daemon=True,
                name="xiaomi-loop")
            self._thread.start()
            return loop

    @staticmethod
    def _serve(loop):
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def run(self, coro, timeout=180):
        """提交协程到单例 loop 并阻塞等待结果。"""
        return asyncio.run_coroutine_threadsafe(coro, self.get()).result(timeout)

    def submit(self, coro):
        """提交协程不等待，返回 Future。"""
        return asyncio.run_coroutine_threadsafe(coro, self.get())

    async def call(self, func, *args):
        """在 loop 上下文里执行同步函数（用于创建 client 使其绑定本 loop）。"""
        return func(*args)


_LOOP = XiaomiLoop()

# 小米 SDK 的 camera stream 是类级共享实例，多路并发 start 会踩踏，
# 用一个 asyncio 锁把 run_stream 串行化（在 loop 线程里延迟创建）。
_MI_LOCK = None


def _mi_lock_sync():
    global _MI_LOCK
    if _MI_LOCK is None:
        _MI_LOCK = asyncio.Lock()
    return _MI_LOCK


def _detect_keyframe_and_codec(data):
    """判断是否为关键帧 + 编码类型（复刻 SDK examples/rtsp/rtsp.py）。"""
    i = 0
    while i < len(data) - 5:
        if data[i:i + 3] == b"\x00\x00\x01":
            header = data[i + 3]
            i += 3
        elif data[i:i + 4] == b"\x00\x00\x00\x01":
            header = data[i + 4]
            i += 4
        else:
            i += 1
            continue
        h264_type = header & 0x1F
        h265_type = (header >> 1) & 0x3F
        if h265_type in (19, 20, 32, 33, 34):
            return True, "hevc"
        if h264_type in (5, 7, 8):
            return True, "h264"
    return False, "unknown"


class XiaomiStreamer(object):
    """一路小米摄像头的取流：NAL 回调 -> ffmpeg -> MPEG-TS -> 扇出。

    对外接口与 nw_live.LiveCam 对齐：add_sub()/del_sub()/alive()/status()，
    这样 nw_api 的 /live/<cam>.ts 端点无需区分类型。
    """

    def __init__(self, cam, token, record_dir="", segment_minutes=5):
        self.cam_id = str(cam.get("id"))
        self.did = cam.get("did") or cam.get("url") or ""
        self.channel = int(cam.get("channel") or 0)
        self.token = token
        self.quality = (cam.get("quality") or "HIGH")
        # 录像：cam.record 为真时，同一个 ffmpeg 额外输出一路 -f segment 落盘
        self.record = bool(cam.get("record", True))
        self.record_dir = record_dir
        self.segment_minutes = int(segment_minutes or 5)

        self._lock = threading.RLock()
        self._subs = []
        # 秒开缓冲：和 nw_live.LiveCam 同一套语义（按 IDR 对齐，见那里的注释）。
        # 小米这一路以前完全没有缓冲，新观看端必然从 GOP 中间接入 —— 就是那条绿带。
        self._ring = collections.deque()
        self._ring_len = 0
        self._ffmpeg = None
        self._loop = None
        self._loop_thread = None
        self._stop_flag = threading.Event()
        self._started = False
        self.error = ""
        self.started_at = 0.0
        self.frames = 0
        self.codec = ""
        # 直播这一路实际是直拷还是转码 + 为什么（页面要能当场查出双转码没消掉）
        self.effective = ""
        self.mode_note = ""
        # 录像那一路怎么处理（直拷 / 因遮蔽重编码），给状态页照实说
        self.rec_note = ""
        self._last_active = time.time()

        # ---- 音频通道 ----
        # SDK 回调在事件循环线程里跑，不能直接做阻塞写；用一个独立线程 + 队列
        # 把 PCM 灌进 ffmpeg 的匿名管道。队列满了就丢帧（宁可丢几百毫秒声音，
        # 也不能把画面/tile 拖死）。
        self._afd = None                     # 当前 ffmpeg 的音频写端 fd
        self._audio_lock = threading.Lock()
        self._aq = queue.Queue(maxsize=120)
        self._audio_thread = None
        self.audio_frames = 0
        self.audio_bytes = 0

    # ---------- 订阅（与 LiveCam 同款） ----------
    def _mk_sub(self, needs_key=False):
        # 延迟导入，避免 Windows 无 nw_live 时也能 import nw_xiaomi
        import nw_live
        return nw_live.Subscriber(needs_key=needs_key)

    def _recent(self):
        """环形缓冲里最后一个 IDR 之后的字节，返回 (字节, 是否已对齐)。"""
        import nw_live
        with self._lock:
            ring = list(self._ring)
        for idx in range(len(ring) - 1, -1, -1):
            batch, koff = ring[idx]
            if koff >= 0:
                parts = [batch[koff:]] + [b for b, _k in ring[idx + 1:]]
                return b"".join(parts), True
        return b"", False

    def add_sub(self):
        with self._lock:
            recent, aligned = self._recent()
            s = self._mk_sub(needs_key=not aligned)
            self._subs.append(s)
            self._last_active = time.time()
            need = not self._started
        if recent:
            s.push(recent, 0)
        if need:
            self.start()
        return s

    def del_sub(self, s):
        s.close()
        with self._lock:
            if s in self._subs:
                self._subs.remove(s)
            self._last_active = time.time()

    def alive(self):
        return self._started and not self._stop_flag.is_set()

    def subscribers(self):
        with self._lock:
            return len(self._subs)

    def status(self):
        import nw_config as _C
        import nw_privacy as PV
        return {"alive": self.alive(), "subs": self.subscribers(),
                "frames": self.frames, "error": self.error,
                "audio_frames": self.audio_frames, "audio_bytes": self.audio_bytes,
                "video": self.effective, "video_note": self.mode_note,
                "rec_note": self.rec_note,
                "privacy": PV.summary(_C.get_camera(self.cam_id)),
                "codec": self.codec,
                "uptime": int(time.time() - self.started_at) if self.started_at else 0}

    # ---------- 启动 ----------
    def start(self):
        if self._started:
            return
        # 失败退避：连不上/离线时不要每来一个请求就重开一次 P2P 会话，
        # 否则会把正在正常出流的另一台摄像头踩掉。
        now = time.time()
        if now < getattr(self, "_next_try", 0):
            return
        # 已知离线：直接给明确原因，不再尝试连接（省得刷 P2P 失败日志）
        try:
            om = _online_map()
            did = str(self.did or "")
            if om and did in om and not om[did]:
                self.error = "设备离线（小米云显示离线）"
                self._next_try = now + 120
                return
        except Exception:
            pass
        self._started = True
        self._stop_flag.clear()
        self.started_at = time.time()
        self._frames_at_start = self.frames
        self._start_audio_writer()
        self._loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._loop_thread.start()

    # ---------- 音频写入 ----------
    def _start_audio_writer(self):
        if self._audio_thread and self._audio_thread.is_alive():
            return
        self._audio_thread = threading.Thread(target=self._audio_loop, daemon=True)
        self._audio_thread.start()

    def _audio_loop(self):
        while not self._stop_flag.is_set():
            try:
                data = self._aq.get(timeout=0.5)
            except queue.Empty:
                continue
            if data is None:
                break
            with self._audio_lock:
                fd = self._afd
            if fd is None:
                continue
            try:
                os.write(fd, data)
            except OSError:
                # 管道还没建好/已关闭（切编码瞬间）——丢掉这一小段即可
                time.sleep(0.02)

    def push_audio(self, data):
        """SDK 音频回调入口（事件循环线程）：只入队，不做任何阻塞 IO。"""
        if not data:
            return
        self.audio_frames += 1
        self.audio_bytes += len(data)
        try:
            self._aq.put_nowait(data)
        except queue.Full:
            # 写不过来说明 ffmpeg 卡住或订阅方太慢，丢掉最旧的一段
            try:
                self._aq.get_nowait()
                self._aq.put_nowait(data)
            except Exception:
                pass

    def stop(self):
        self._stop_flag.set()
        # 取消仍在单例 loop 里跑着的取流协程
        fut = getattr(self, "_future", None)
        if fut is not None:
            try:
                fut.cancel()
            except Exception:
                pass
        # 关 ffmpeg stdin 让子进程退出
        with self._lock:
            p = self._ffmpeg
        if p:
            try:
                p.stdin.close()
            except Exception:
                pass
            try:
                p.terminate()
            except Exception:
                pass
        # 关音频管道并叫停写线程
        with self._audio_lock:
            fd = self._afd
            self._afd = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            self._aq.put_nowait(None)
        except Exception:
            pass
        self._started = False

    # ---------- 事件循环：复用全局单例，不再每路新建 ----------
    def _run_loop(self):
        try:
            self._future = _LOOP.submit(self._stream_async())
            self._future.result()          # 一直阻塞到流结束或被 cancel
        except Exception as e:
            msg = str(e).lower()
            if "cancel" not in msg:
                self.error = "stream: %s" % e
        finally:
            self._started = False
            # 没出过数据就算失败：指数退避 10s→20s→…→300s，避免刷会话
            if not self._stop_flag.is_set() and self.frames <= getattr(self, "_frames_at_start", 0):
                self._fail_n = getattr(self, "_fail_n", 0) + 1
                delay = min(300, 10 * (2 ** min(self._fail_n - 1, 5)))
                self._next_try = time.time() + delay
            else:
                self._fail_n = 0

    async def _start_ffmpeg(self, codec):
        """按探测到的真实编码启动 ffmpeg。

        裸 NAL 流（来自 SDK 回调）没有 PTS/DTS，必须显式要求 ffmpeg 用墙上时钟
        补时间戳，否则会报 "Non-monotonic DTS" 并吐不出任何 TS 包（表现为有数据
        流、前端却黑屏）。codec 由首帧 NAL 头探测决定：小米既有 H.264 也有 H.265。

        ★ 两路输出，编码策略不同（这是能不能"看到画面"的关键）：
          * 直播 → 浏览器：H.265 必须转 H.264（浏览器 MSE / mpegts.js 解不了 HEVC，
            表现就是接口 HTTP 200、字节也在流、前端却一直"拉流失败/无画面"）；
            源本来就是 H.264 时不用转，直拷即可 —— 这就是 live.transcode=auto。
            但配了隐私遮蔽时**必须转**：遮蔽是滤镜，只有重编码才烧得进像素。
          * 录像 → 磁盘：默认 -c:v copy 原码流直拷，不重编码 —— 画质无损、
            不额外吃 CPU，这也是录像体积与摄像机原生一致的原因。配了遮蔽时改
            为重编码：录进去的就必须是打过码的画面。
        """
        live = _live_opts()
        # 这一路不需要探测：codec 是首帧 NAL 头判出来的真实编码。
        import nw_live as LV
        import nw_config as _C
        import nw_privacy as PV
        # 隐私遮蔽按 cam_id 现取：这一路重建 ffmpeg 时读的就是当时配置里的遮罩，
        # 改了遮蔽由 reconcile 重起这条流后生效。
        mask_vf = PV.filters(_C.get_camera(self.cam_id))
        need, _note = LV.transcode_needed(LV._tmode(live.get("transcode")),
                                          codec, bool(mask_vf))
        transcode = need
        self.effective = "transcode" if transcode else "copy"
        self.mode_note = _note
        tw = _to_int(live.get("width"), 0)
        tfps = _to_int(live.get("fps"), 0)
        crf = min(31, max(1, _to_int(live.get("quality"), 23)))
        audio_on = bool(live.get("audio", True))

        head = [
            _ffmpeg_bin(), "-hide_banner", "-loglevel", "error",
            "-use_wallclock_as_timestamps", "1",   # 裸流无时间戳，必须
            "-fflags", "+genpts+discardcorrupt",
            "-f", codec, "-i", "pipe:0",
        ]

        # 音频走第二个输入：SDK 解出来的 PCM（s16le / 16000Hz / 单声道）通过
        # 一根匿名管道（pipe fd）喂给 ffmpeg。用 pipe 而不是临时 FIFO 文件：
        # FIFO 的 open 会阻塞到对端就绪，重建 ffmpeg（切编码）时容易卡住整条流。
        afd_r = afd_w = None
        if audio_on:
            try:
                afd_r, afd_w = os.pipe()
                head += ["-use_wallclock_as_timestamps", "1",
                         "-thread_queue_size", "512",
                         "-f", "s16le", "-ar", "16000", "-ac", "1",
                         "-i", "pipe:%d" % afd_r]
            except OSError:
                afd_r = afd_w = None
        a_map = ["-map", "1:a:0"] if afd_w is not None else []

        # ---- 输出 1：直播 MPEG-TS（stdout）----
        cmd = list(head) + ["-map", "0:v:0"] + a_map
        # MSE/mpegts.js 只认 AAC；PCM 不能直接塞进 TS
        if afd_w is not None:
            cmd += ["-c:a", "aac", "-b:a", "64k", "-ar", "44100", "-ac", "1"]
        if transcode:
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                    "-profile:v", "main", "-pix_fmt", "yuv420p", "-crf", str(crf),
                    # 每个关键帧都重复写 SPS/PPS（配合下面 dump_extra）。
                    # 少了它，TS 里参数集只在流开头出现一次，后加入的观看端
                    # 会报 "non-existing PPS 0 referenced" → 永远黑屏。
                    "-x264-params", "repeat-headers=1",
                    "-bsf:v", "dump_extra"]
            vf = []
            if tw > 0:
                # 只缩不放：源比目标小时保持原尺寸
                vf.append("scale='min(iw,%d)':-2" % tw)
            vf += mask_vf          # 先缩后涂：drawbox 的 iw/ih 是它自己输入的尺寸
            if vf:
                cmd += ["-vf", ",".join(vf)]
            if tfps > 0:
                cmd += ["-r", str(tfps)]
            # 固定 GOP，保证新订阅者能较快拿到 I 帧（秒开）
            cmd += ["-g", str(max(tfps or 10, 1) * 2)]
        else:
            # 兜底：不转码时保持原码流（HEVC 源在浏览器上大概率播不出来）
            cmd += ["-c:v", "copy", "-bsf:v", "extract_extradata"]
        cmd += ["-flush_packets", "1",
                # 兜底：万一这台机器没有麦克风 / 音频解码失败，音轨一帧都没有时
                # 不许让 muxer 无限等音轨 —— 那会把画面也一起卡成 0 字节。
                "-max_interleave_delta", "1000000",
                "-f", "mpegts", "-mpegts_flags", "+resend_headers", "pipe:1"]

        # ---- 输出 2：录像分段 MP4（默认原码流直拷；配了遮蔽则重编码）----
        if self.record and self.record_dir:
            try:
                # segment muxer 不会自动创建 %Y/%m/%d 多级目录，直接平铺到
                # 摄像头目录：recordings/<cam_id>/YYYYMMDD_HHMMSS.mp4。
                # 单层目录、跨天/跨月都不会失效。
                os.makedirs(self.record_dir, exist_ok=True)
                pattern = os.path.join(self.record_dir, "%Y%m%d-%H%M%S.mp4")
                # extract_extradata 必须加：裸 NAL 流没有带外 extradata，mp4 muxer
                # 拿不到 VPS/SPS/PPS 就会报
                #   "Could not write header (incorrect codec parameters ?)"
                # 结果是录像一个文件都写不出来（直播那一路不受影响，很容易漏看）。
                cmd += [
                    "-map", "0:v:0"] + a_map
                if mask_vf:
                    # 录像也要带遮蔽 —— 否则"实时看着是黑的、拖回放一看原形毕露"，
                    # 那正是区域遮蔽要避免的事。代价：这一路的录像从直拷变成重编码。
                    cmd += [
                        "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", str(crf), "-pix_fmt", "yuv420p",
                        "-vf", ",".join(mask_vf),
                        "-force_key_frames", "expr:gte(t,n_forced*%d)" % (
                            max(1, self.segment_minutes) * 60),
                    ]
                    self.rec_note = "录像带遮蔽，已重编码（不直拷）"
                else:
                    cmd += [
                        "-c:v", "copy", "-bsf:v", "extract_extradata",
                    ]
                    self.rec_note = "录像原码流直拷（不重编码）"
                if afd_w is not None:
                    cmd += ["-c:a", "aac", "-b:a", "64k", "-ar", "44100", "-ac", "1"]
                cmd += [
                    "-f", "segment",
                    "-segment_time", str(max(1, self.segment_minutes) * 60),
                    "-segment_format_options", "movflags=+frag_keyframe+empty_moov",
                    "-max_interleave_delta", "10000",
                    "-reset_timestamps", "1",
                    "-strftime", "1",
                    pattern,
                ]
            except Exception as e:
                self.error = "录像目录不可用：%s" % e
        kw = {}
        if afd_r is not None:
            kw["pass_fds"] = (afd_r,)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kw
            )
        finally:
            # 读端交给子进程后，父进程必须立刻关掉，否则子进程退出时管道
            # 不会真正断开（EOF 永远不来），ffmpeg 会挂在那里不退。
            if afd_r is not None:
                try:
                    os.close(afd_r)
                except OSError:
                    pass
        # 音频写端换成新管道的（切编码会重建 ffmpeg，管道也跟着换）
        with self._audio_lock:
            if afd_w is not None:
                old = self._afd
                self._afd = afd_w
                if old is not None:
                    try:
                        os.close(old)
                    except OSError:
                        pass
            else:
                self._afd = None
        with self._lock:
            replaced = self._ffmpeg is not None
            self._ffmpeg = proc
            # 新进程的 TS 流（PAT/PMT、编码参数）和旧的不连续，旧缓冲里的字节
            # 喂给新订阅者只会解出错帧 —— 清掉，等新流自己攒出第一个 IDR。
            self._ring.clear()
            self._ring_len = 0
            old_subs = list(self._subs) if replaced else []
        # 编码切换会重建 ffmpeg，新时间线和老观看端接不上（PTS 断裂）。
        # 主动断开，让浏览器立刻重连到新流，而不是原地卡住等看门狗。
        for s in old_subs:
            s.close()
        asyncio.ensure_future(self._pump_ts(proc))
        asyncio.ensure_future(self._pump_err(proc))
        return proc

    async def _stream_async(self):
        sys.path.insert(0, SDK_PATH)
        from miloco_sdk.utils.types import MIoTCameraVideoQuality
        client = _auth.ensure_client()
        quality_map = {"LOW": MIoTCameraVideoQuality.LOW,
                       "HIGH": MIoTCameraVideoQuality.HIGH}
        quality = quality_map.get(self.quality.upper(), MIoTCameraVideoQuality.HIGH)

        # 参数集缓存：确保每个 IDR 前带 SPS/PPS，中途接入可解码
        cached_ps = {}
        state = {"proc": None, "codec": None}
        # 小米摄像头几乎全是 HEVC(H.265)，但个别老机型是 H.264。
        # 单一探测并不可靠：首帧可能只有参数集NAL，容易误判成 h264，
        # 于是 ffmpeg 报 "[h264 @ ...] no frame!" 却始终吐不出 TS。
        # 这里改成"先按首选起，出错自动切另一种"的自适应策略。
        prefer = (self.codec or "").lower()
        if prefer not in ("hevc", "h264"):
            prefer = "hevc"
        state["want"] = prefer
        self.retried = False

        async def ensure_proc(new_codec):
            """关掉旧 ffmpeg，用指定 codec 重新起一个。"""
            old = state["proc"]
            if old is not None:
                try:
                    if old.stdin:
                        old.stdin.close()
                except Exception:
                    pass
                try:
                    old.terminate()
                except Exception:
                    pass
            state["proc"] = await self._start_ffmpeg(new_codec)
            state["codec"] = new_codec

        def has_enough_ps(codec):
            """ffmpeg 要能解码/转封装，必须先拿到完整的参数集。"""
            if codec == "hevc":
                return all(("hevc_%d" % t) in cached_ps for t in (32, 33, 34))
            return all(("h264_%d" % t) in cached_ps for t in (7, 8))

        async def on_raw_video(did, data, ts, seq, channel):
            self.frames += 1
            proc = state["proc"]
            # 参数集注入要用当前实际在跑的 codec 语义（hevc/h264 的 NAL 头不同）
            data = _inject_parameter_sets(data, cached_ps, state.get("codec") or prefer)
            # 尚未起来：先攒齐 VPS/SPS/PPS 再启动 ffmpeg。
            # 中途接入时首帧往往就是 IDR 而没带参数集，直接喂会给 ffmpeg 造成
            # "PPS id out of range"，所以这里等攒够再起。
            if proc is None:
                # 官方示例的必要条件：ffmpeg 必须从 IDR 关键帧开始喂，
                # 且此时参数集已攒齐，否则解不出画面。
                if not has_enough_ps(state["want"]) and self.frames < 600:
                    return
                key, detected = _detect_keyframe_and_codec(data)
                if not key and self.frames < 600:
                    return
                if detected != "unknown":
                    state["want"] = detected
                self.codec = state["want"]
                await ensure_proc(state["want"])
                proc = state["proc"]
            # 已经被标记要换 codec：切换后重建
            elif state["want"] != state["codec"]:
                await ensure_proc(state["want"])
                proc = state["proc"]
            if proc and proc.stdin and not proc.stdin.is_closing():
                try:
                    proc.stdin.write(data)
                    await proc.stdin.drain()
                except Exception:
                    pass

        # 让 _pump_err 能触发 codec 切换
        self._switch_codec = state

        async def on_decode_pcm(did, data, ts, channel):
            """SDK 解码后的 PCM：s16le / 16000Hz / 单声道。"""
            self.push_audio(data)

        # SDK 里 miot_camera_stream 是 XiaomiClient 的**类属性**（所有实例共享），
        # 多路摄像头并发调 run_stream 会互相覆盖内部状态，表现成
        # "'MIoTClient' object has no attribute '_camera_client'"。
        # 所以启动阶段必须串行化；启动完成后 wait_for_data 在锁外长阻塞。
        # 另外：如果另一台摄像头正在正常出流，这里先让行 —— 硬上会把它踢掉。
        for _ in range(20):
            if self._stop_flag.is_set():
                break
            if not _active_other(self.cam_id):
                break
            await asyncio.sleep(2.0)

        lock = _mi_lock_sync()
        try:
            async with lock:
                # SDK 官方示例的两段式：run_stream 只是把推流任务挂上去，
                # 必须再 await wait_for_data() 才会真正驱动数据回调。漏掉这一步会
                # 表现成 "Task was destroyed but it is pending" + 永远 0 字节。
                await client.miot_camera_stream.run_stream(
                    did=self.did, channel=self.channel,
                    on_raw_video_callback=on_raw_video,
                    # 音频：run_stream 内部 start_async(enable_audio=True) 已经打开了
                    # 音频拉取，这里只要注册回调就能拿到解码后的 PCM。
                    on_decode_pcm_callback=on_decode_pcm,
                    video_quality=quality,
                )
            await client.miot_camera_stream.wait_for_data()
        except Exception as e:
            self.error = "run_stream: %s" % e
        finally:
            proc = state["proc"]
            try:
                if proc and proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass

    async def _pump_ts(self, proc):
        """读 ffmpeg stdout 的 MPEG-TS，按 188 字节切包广播。"""
        buf = b""
        try:
            while True:
                chunk = await proc.stdout.read(32768)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= 188:
                    if buf[0] != 0x47:
                        i = buf.find(b"\x47")
                        if i < 0:
                            buf = b""
                            break
                        if i > 0:
                            buf = buf[i:]
                            continue
                    n = (len(buf) // 188) * 188
                    batch = buf[:n]
                    buf = buf[n:]
                    # 登记"本路正在出数据"，让别的摄像头先让行，别把这条踩掉
                    _touch_active(self.cam_id)
                    import nw_live
                    koff = nw_live._keyframe_offset(batch, self.codec)
                    with self._lock:
                        self._ring.append((batch, koff))
                        self._ring_len += len(batch)
                        while self._ring_len > nw_live._RING:
                            self._ring_len -= len(self._ring.popleft()[0])
                        subs = list(self._subs)
                    for s in subs:
                        s.push(batch, koff)
        except Exception as e:
            self.error = "pump: %s" % e

    async def _pump_err(self, proc):
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                s = line.decode("utf-8", "replace").strip()
                if not s:
                    continue
                # 这些是"能从流断续里自愈"的噪声，不该占着 error 字段：
                # error 会被前端当成"无画面原因"显示出来，塞满 ffmpeg 内部告警
                # 反而让人以为摄像头坏了。
                low0 = s.lower()
                noisy = ("could not find ref", "pps id out of range",
                         "non monotonically increasing", "last message repeated",
                         "missing picture in access unit", "concealing")
                if any(k in low0 for k in noisy):
                    self._last_warn = s[-300:]
                    continue
                self.error = s[-300:]
                # ffmpeg 用错解析格式时典型报错：
                #   [h264 @ ...] no frame!
                #   Invalid data found when processing input
                # 此时把期望 codec 切成另一种，下一次视频回调会自动重建 ffmpeg。
                #
                # ★ 注意不要把"解码抖动"当成"猜错编码"：
                #   "Could not find ref with POC 12" / "PPS id out of range" 这类是
                #   HEVC 从 GOP 中间接入时的正常现象，ffmpeg 会自己恢复。以前把它们
                #   也算作 bad，结果一抖就把 hevc 切成 h264 → 流彻底死掉，
                #   现象是"能出几秒画面然后永久黑屏"。
                st = getattr(self, "_switch_codec", None)
                if st and not getattr(self, "retried", True):
                    low = s.lower()
                    bad = ("no frame" in low or "invalid data" in low
                           or "error while decoding" in low
                           or "non-existing pps" in low
                           or "invalid nal unit size" in low)
                    if bad:
                        cur = (self.codec or st.get("codec") or "").lower()
                        if cur not in ("hevc", "h264"):
                            cur = st.get("want") or "hevc"
                        nxt = "h264" if cur == "hevc" else "hevc"
                        self.retried = True
                        self.codec = nxt
                        st["want"] = nxt
                        sys.stderr.write("[homeeye] codec retry: switch to %s (%s)\n"
                                         % (nxt, s[:80]))
                        sys.stderr.flush()
        except Exception:
            pass


def parse_nals(data):
    """解析 Annex-B 数据中的所有 NAL 单元，返回 [(offset, nal_bytes), ...]"""
    nals = []
    offsets = []
    i = 0
    n = len(data)
    while i < n - 3:
        if data[i:i + 4] == b"\x00\x00\x00\x01":
            offsets.append(i)
            i += 4
        elif data[i:i + 3] == b"\x00\x00\x01":
            offsets.append(i)
            i += 3
        else:
            i += 1
    for idx, off in enumerate(offsets):
        end = offsets[idx + 1] if idx + 1 < len(offsets) else n
        nals.append((off, data[off:end]))
    return nals


def _nal_type_and_body(nal):
    """取 NAL 的头字节并返回 (是否hevc, nal_type)。"""
    if nal[:4] == b"\x00\x00\x00\x01":
        header = nal[4]
    elif nal[:3] == b"\x00\x00\x01":
        header = nal[3]
    else:
        return False, -1
    return True, header


def _inject_parameter_sets(data, cached_ps, codec="hevc"):
    """IDR 帧前补 VPS/SPS/PPS。

    小米 SDK 吐出的裸 NAL 里，参数集只在关键位置出现一次。ffmpeg 用 -c copy
    转封装时若首帧就是 IDR 而没有参数集，会报
        [hevc @ ...] PPS id out of range: 0
    并且吐不出任何 TS 包。这里缓存出现过的参数集，凡是遇到 IDR 但未自带参数集
    的包，就把缓存的 VPS->SPS->PPS 补在最前面。
    """
    nals = parse_nals(data)
    if not nals:
        return data

    has_idr = False
    has_params = False
    if codec == "hevc":
        param_types, idr_types = (32, 33, 34), (19, 20)
    else:
        param_types, idr_types = (7, 8), (5,)

    prefix_key = "hevc" if codec == "hevc" else "h264"
    for _, nal in nals:
        ok, header = _nal_type_and_body(nal)
        if not ok:
            continue
        nal_type = ((header >> 1) & 0x3F) if codec == "hevc" else (header & 0x1F)
        if nal_type in idr_types:
            has_idr = True
        elif nal_type in param_types:
            has_params = True
            key = "%s_%d" % (prefix_key, nal_type)
            if cached_ps.get(key) != nal:
                cached_ps[key] = nal

    if has_idr and not has_params:
        prefix = b""
        for t in param_types:
            nal = cached_ps.get("%s_%d" % (prefix_key, t))
            if nal:
                prefix += nal
        if prefix:
            return prefix + data
    return data


class XiaomiHub(object):
    """管理所有小米摄像头的取流。"""

    def __init__(self):
        self._streamers = {}
        self._lock = threading.RLock()

    def reconcile(self, cams, root="", seg_min=5):
        """按配置对齐小米取流：该跑的拉起（含录像输出），多余的停掉。"""
        cams = list(cams or [])
        for cam in cams:
            # 只处理小米类型
            if (cam.get("type") or "rtsp").lower() != "xiaomi":
                continue
            cid = str(cam.get("id"))
            if not (cam.get("enabled", True) and (cam.get("did") or cam.get("url"))):
                self.drop(cid)
                continue
            cid = str(cam.get("id"))
            camdir = os.path.join(root, cid) if root else ""
            self.get(cam, camdir, seg_min)   # 幂等：已在跑的不会重启
        # 清掉已删除的摄像头
        keep = set(str(c.get("id")) for c in cams
                   if (c.get("type") or "rtsp").lower() == "xiaomi")
        with self._lock:
            for cid in list(self._streamers):
                if cid not in keep:
                    self._streamers.pop(cid, None).stop()

    def get(self, cam, record_dir="", segment_minutes=5):
        import nw_privacy as PV
        cid = str(cam.get("id"))
        # 遮蔽区域进签名：改了罩子必须重建这条流（滤镜是启动时定死的，运行中
        # 加不上）。没改的人不受影响 —— 签名相同就还是原来那一台 ffmpeg。
        sig = (cam.get("did"), cam.get("channel"), cam.get("quality"),
               tuple((m["x"], m["y"], m["w"], m["h"])
                     for m in PV.masks_of(cam)))
        with self._lock:
            st = self._streamers.get(cid)
            if st is None or getattr(st, "_sig", None) != sig:
                if st is not None:
                    st.stop()
                # 关键：一定要在「非 loop 线程」里先把 client 准备好。
                # 若留到 _stream_async() 内部再创建，协程已在 loop 线程中运行，
                # 再向同一 loop 提交并等待会立即死锁（frames 永远 0、无任何日志）。
                _auth.ensure_client()
                st = XiaomiStreamer(cam, _auth.token, record_dir, segment_minutes)
                st._sig = sig
                self._streamers[cid] = st
            # 必须显式拉起：以前这里只创建不启动，结果请求 /live/<cam>.ts 时
            # 日志里连一次 P2P 连接尝试都没有，前端永远黑屏 0 字节。
            # start() 自带幂等（_started 标志），反复调用安全。
            st.start()
            return st

    def drop(self, cam_id):
        with self._lock:
            st = self._streamers.pop(str(cam_id), None)
        if st:
            st.stop()

    def drop_all(self):
        with self._lock:
            streamers = list(self._streamers.values())
            self._streamers.clear()
        for st in streamers:
            st.stop()

    def status(self):
        with self._lock:
            return {cid: st.status() for cid, st in self._streamers.items()}


_xiaomi_hub = XiaomiHub()


def get_hub():
    return _xiaomi_hub


if __name__ == "__main__":
    ok, msg = xiaomi_available()
    print("xiaomi_available:", ok, msg)
