# -*- coding: utf-8 -*-
"""HomeEye · HTTP 服务（标准库 http.server，无第三方依赖）

路由约定：
  /                     前端页面
  /api/*                JSON 接口
  /live/<cam>.mjpg      MJPEG 实时流（multipart/x-mixed-replace）
  /rec/<cam>/..mp4      录像文件（支持 Range，供 <video> 拖动定位）

前缀兼容：飞牛网关可能带 /app/<appname> 前缀转发，也可能剥掉。
这里统一在入口把已知前缀抹平，所以两种转发方式都能跑。
"""
import json
import mimetypes
import os
import posixpath
import re
import shlex
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import nw_ai as AI
import nw_archive as AR
import nw_audit as AU
import nw_auth as A
import nw_config as C
import nw_env as E
import nw_events as EV
import nw_export as EX
import nw_health as H
import nw_integrity as IG
import nw_live as L
import nw_privacy as PV
import nw_record as R
import nw_watchdog as W

VERSION = "1.3.0"
BOUNDARY = "nwframe"

_live = None
_rec = None
_httpd = None
_viewed = {}
_viewed_lock = threading.Lock()
VIEW_AUDIT_TTL = 300.0


# ---------------------------------------------------------------- 工具
def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


_BOOT = time.time()
_metrics_cache = {"at": 0.0, "text": ""}
METRICS_TTL = 5.0
_CAM_STATES = ("ok", "idle", "offline", "broken", "unknown")


def _proc_rss_bytes():
    """进程常驻内存（Linux 读 /proc；其它平台返回 0，指标线就是平的）。"""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _lbl(**kv):
    parts = []
    for k in sorted(kv):
        v = str(kv[k]).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
        parts.append('%s="%s"' % (k, v))
    return "{" + ",".join(parts) + "}" if parts else ""


def _num(v):
    if v is None:
        return "0"
    if isinstance(v, float):
        s = ("%.3f" % v).rstrip("0").rstrip(".")
        return s or "0"
    return str(int(v))


def _metrics_text():
    """Prometheus 文本格式 0.0.4。序列名一律 homeeye_ 前缀。

    只读缓存快照（H.statuses / _live.status / _rec.status），不会因为抓取
    而触发新一轮探测；整段结果缓存 5 秒，避免多抓取器把 NAS 上的目录遍历
    放大成负载。
    """
    now = time.time()
    if _metrics_cache["text"] and now - _metrics_cache["at"] < METRICS_TTL:
        return _metrics_cache["text"]
    out = []

    def metric(name, typ, help_, rows):
        out.append("# HELP %s %s" % (name, help_))
        out.append("# TYPE %s %s" % (name, typ))
        for labels, value in rows:
            out.append("%s%s %s" % (name, labels, _num(value)))

    cfg = C.load()
    cams = [c for c in (cfg.get("cameras") or []) if c.get("enabled", True)]
    hs = H.statuses()
    ls = _live.status() if _live is not None else {}
    rs = _rec.status() if _rec is not None else {}
    evs = EV.status()
    ws = W.state()
    st = H.storage_state()
    usage = _rec.usage() if _rec is not None else {}

    metric("homeeye_up", "gauge", "HomeEye HTTP 服务在跑", [("", 1)])
    metric("homeeye_info", "gauge", "版本信息",
           [(_lbl(version=VERSION), 1)])
    metric("homeeye_uptime_seconds", "gauge", "进程已运行秒数", [("", now - _BOOT)])
    metric("homeeye_process_rss_bytes", "gauge", "进程常驻内存字节",
           [("", _proc_rss_bytes())])

    root = C.recordings_root(cfg)
    today = time.strftime("%Y%m%d", time.localtime(now))
    up_rows, state_rows, rec_rows, seg_rows, ev_rows = [], [], [], [], []
    for cam in cams:
        cid = str(cam.get("id"))
        name = cam.get("name") or cid
        h = hs.get(cid) or {}
        state = str(h.get("state") or "unknown")
        lbl = _lbl(camera=cid, name=name)
        up_rows.append((lbl, 1 if state == "ok" else 0))
        for s in _CAM_STATES:
            state_rows.append((_lbl(camera=cid, name=name, state=s),
                               1 if state == s else 0))
        rec_rows.append((lbl, 1 if (rs.get(cid) or {}).get("running") else 0))
        if h.get("segment_age") is not None:
            seg_rows.append((lbl, h["segment_age"]))
        ev_rows.append((lbl, len(EV.read_day(root, cid, today))))
    metric("homeeye_camera_up", "gauge", "摄像头链路正常（state=ok）", up_rows)
    metric("homeeye_camera_state", "gauge", "摄像头上报状态（0/1 展开）", state_rows)
    metric("homeeye_recording_up", "gauge", "该路录像进程在跑", rec_rows)
    metric("homeeye_camera_segment_age_seconds", "gauge",
           "最新一段录像距今秒数（不录像的路不产出）", seg_rows)
    metric("homeeye_events_today", "gauge", "今天本地侦测到的事件数", ev_rows)

    metric("homeeye_live_frames_total", "counter", "直播出帧累计",
           [(_lbl(camera=cid), (v or {}).get("frames") or 0) for cid, v in sorted(ls.items())])
    metric("homeeye_live_subscribers", "gauge", "直播在线观看端数",
           [(_lbl(camera=cid), (v or {}).get("subs") or 0) for cid, v in sorted(ls.items())])

    metric("homeeye_storage_ok", "gauge", "录像目录可写且空间足够",
           [("", 1 if st.get("ok") else 0)])
    metric("homeeye_storage_free_bytes", "gauge", "录像盘剩余空间",
           [("", float(st.get("free_gb") or 0) * 1024 ** 3)])
    metric("homeeye_storage_disk_total_bytes", "gauge", "录像盘总容量",
           [("", float(st.get("total_gb") or 0) * 1024 ** 3)])
    metric("homeeye_storage_budget_bytes", "gauge", "配置的录像预算（0 = 不限）",
           [("", float(st.get("budget_gb") or 0) * 1024 ** 3)])
    metric("homeeye_recordings_bytes", "gauge", "录像目录当前占用",
           [("", usage.get("total_bytes") or 0)])

    beat = evs.get("beat") or 0
    metric("homeeye_scanner_beat_age_seconds", "gauge",
           "事件扫描最近一次转圈的间隔（-1 = 还没跑过）",
           [("", (now - beat) if beat else -1)])
    metric("homeeye_scanner_pending", "gauge", "有待处理的手动扫描请求",
           [("", 1 if evs.get("pending") else 0)])

    metric("homeeye_watchdog_beat_age_seconds", "gauge", "看门狗最近一次转圈距今秒数",
           [("", ws.get("beat_age") if ws.get("beat_age") is not None else -1)])
    metric("homeeye_watchdog_loops_total", "counter", "看门狗循环次数",
           [("", ws.get("loops") or 0)])
    metric("homeeye_watchdog_actions_total", "counter", "自愈动作累计次数",
           [(_lbl(action=k), v) for k, v in sorted((ws.get("stats") or {}).items())])

    text = "\n".join(out) + "\n"
    _metrics_cache["text"] = text
    _metrics_cache["at"] = now
    return text


# 飞牛统一网关模式下，前端 <base> 指向 /app/<appname>/；TCP 直连时指向 /
def _base_href():
    if os.environ.get("GATEWAY_SOCKET"):
        return "/app/" + C.APPNAME + "/"
    return "/"


def _ffprobe(url, transport="tcp", timeout=12):
    cmd = ["ffprobe", "-hide_banner", "-loglevel", "error"]
    if (url or "").lower().startswith("rtsp"):
        cmd += ["-rtsp_transport", transport]
    cmd += ["-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height,avg_frame_rate",
            "-of", "json", "-rw_timeout", "8000000", url]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if p.returncode != 0:
            msg = (p.stderr or b"").decode("utf-8", "replace").strip()
            return False, msg[-400:] or "拉流失败"
        info = json.loads((p.stdout or b"{}").decode("utf-8", "replace"))
        st = (info.get("streams") or [{}])[0]
        return True, {"codec": st.get("codec_name"), "width": st.get("width"),
                      "height": st.get("height"), "fps": st.get("avg_frame_rate")}
    except subprocess.TimeoutExpired:
        return False, "连接超时（检查 IP / 端口 / 账号密码 / 网络是否可达）"
    except FileNotFoundError:
        return False, "系统里找不到 ffprobe"
    except Exception as e:
        return False, str(e)[-300:]


def _view_already_logged(key):
    """同一个人反复拖同一段，5 分钟内只记一条"看过"。

    为什么去重：一次回放会打十几个分段请求，全记下来会把有用的审计埋掉 ——
    审计日志要回答的是"谁在什么时候看过客厅那段"，不是"他拖了几次进度条"。
    """
    now = time.time()
    with _viewed_lock:
        if len(_viewed) > 4000:
            for k in [k for k, t in _viewed.items() if now - t > VIEW_AUDIT_TTL]:
                _viewed.pop(k, None)
        if now - _viewed.get(key, 0) < VIEW_AUDIT_TTL:
            return True
        _viewed[key] = now
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "HomeEye/" + VERSION
    protocol_version = "HTTP/1.1"

    # ---------- 基础 ----------
    def log_message(self, fmt, *args):
        # 只在出错时留痕，正常请求不刷日志（MJPEG 一秒十几条）
        pass

    def _strip_prefix(self, path):
        p = path
        for pre in ("/app/" + C.APPNAME, "/" + C.APPNAME):
            if p == pre:
                return "/"
            if p.startswith(pre + "/"):
                return p[len(pre):]
        return p

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8", extra=None, head=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        if body and not head:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _j(self, obj, code=200):
        self._send(code, _json_bytes(obj))

    def _err(self, msg, code=400, **extra):
        obj = {"ok": False, "error": msg}
        obj.update(extra)
        self._j(obj, code)

    def _body(self):
        """解析请求 JSON（带缓存：闸门和接口本身都要看同一个 body）。

        rfile 只能读一次，不缓存的话第二个调用方永远拿到空 dict —— 表现是
        "鉴权通过后接口说缺少参数"，很难猜。
        """
        cached = getattr(self, "_body_cache", None)
        if cached is not None:
            return cached
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            self._body_cache = {}
            return self._body_cache
        raw = self.rfile.read(min(n, 4 * 1024 * 1024))
        try:
            out = json.loads(raw.decode("utf-8"))
        except Exception:
            out = {}
        self._body_cache = out if isinstance(out, dict) else {}
        return self._body_cache

    def _raw_body(self, limit):
        """原样读 body（对讲音频是裸 PCM，不能按 JSON 解析）。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return b""
        return self.rfile.read(min(n, limit))

    # ---------- 分发 ----------
    def _ip(self):
        try:
            return str((self.client_address or ("",))[0])
        except Exception:
            return ""

    def _user(self):
        """当前请求的会话（角色、授权摄像头都在里面）。

        网关那一路没有 Cookie（飞牛已经鉴过权），当管理员看待；闸门关掉时
        也没有 Cookie，但必须给一个等同管理员的匿名会话 —— 否则「关掉登录」
        反而会让按摄像头授权把每一路都判成拒绝，页面一台摄像头都看不见。
        其余一律用 token 换会话，拿不到 = 未登录。
        """
        if A.is_gateway(self):
            return A.gateway_session()
        sess = A.current(A.token_from_headers(self.headers))
        if sess is None and not A.enabled():
            return A.open_session()
        return sess

    def _logged_in(self):
        """当前请求带着有效会话吗（只回答"登没登录"，权限要看 `_user`）。"""
        return self._user() is not None

    def _actor(self):
        sess = self._user() or {}
        return str(sess.get("user") or "anonymous")

    def _audit(self, kind, **fields):
        """审计绝不能把业务请求带崩，所以整体吞异常。"""
        try:
            AU.log(kind, self._actor(), ip=self._ip(), **fields)
        except Exception:
            pass

    def _require_admin(self):
        """管理类接口（设置、摄像头增删、用户、导出分享）的统一入口闸门。"""
        sess = self._user()
        if sess is None:
            self._err("需要登录", 401, auth=A.public_state(None))
            return None
        if not A.is_admin(sess):
            self._err("需要管理员权限", 403, auth=A.public_state(sess))
            return None
        return sess

    def _cam_ok_or_deny(self, cam_id, why="view"):
        """按摄像头授权判定 —— 判在数据出口，不是把入口藏起来。

        未授权的摄像头返回 404 而不是 403：403 会告诉只读用户"这路存在"，
        而家庭账号本来就不该知道家里装了几台、装在哪儿。
        """
        sess = self._user()
        if sess is None:
            self._err("需要登录", 401, auth=A.public_state(None))
            return False
        if A.cam_ok(sess, cam_id):
            return True
        self._audit("cam_denied", cam=str(cam_id), action=why)
        self._send(404, b"not found", "text/plain")
        return False

    def do_GET(self):
        self._route("GET")

    def do_HEAD(self):
        self._route("HEAD")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method):
        # keep-alive 会复用同一个 handler 实例：body 缓存必须每条请求清一次，
        # 否则第二个请求会读到上一个请求的 JSON
        self._body_cache = None
        u = urllib.parse.urlparse(self.path)
        path = self._strip_prefix(urllib.parse.unquote(u.path))
        if ".." in path.split("/"):
            # 解码以后还要再查一次：单页应用会把没认出来的路径回退成 index.html，
            # %2e%2e/config.json 这种写法就是这样绕过守卫、还回个 200 的
            return self._send(400, b"bad path", "text/plain")
        q = urllib.parse.parse_qs(u.query)
        try:
            # 鉴权闸门：/api、/live、/rec 全部要会话（网关模式与"未设密码"时放行）
            if not A.allows(self, path, method, q):
                # 请求体没读完就回响应会把这条 keep-alive 连接的字节流弄脏，
                # 所以直接关掉连接（对讲的 PCM 包体几 KB 到几 MB，不值得吸收）
                self.close_connection = True
                return self._err("需要登录", 401, auth=A.public_state())
            if path.startswith("/api/"):
                return self._api(method, path, q)
            if path.startswith("/share/"):
                return self._share_route(path, method == "HEAD")
            if path.startswith("/export/"):
                return self._export_route(path, method == "HEAD")
            if path.startswith("/live/"):
                return self._live_stream(path, q)
            if path.startswith("/rec/"):
                return self._file(path, q, head=(method == "HEAD"))
            if path.startswith("/replay/"):
                return self._rec_replay(path, q)
            if path.startswith("/rech264/"):
                return self._rec_transcode(path, q)
            if method in ("GET", "HEAD"):
                return self._static(path, head=(method == "HEAD"))
            return self._err("不支持的请求", 405)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:
            try:
                self._err("服务端异常：%s" % e, 500)
            except Exception:
                pass

    # ---------- 静态文件 ----------
    def _static(self, path, head=False):
        if path in ("/", ""):
            path = "/index.html"
        rel = posixpath.normpath(path).lstrip("/")
        if ".." in rel.split("/"):
            return self._send(404, b"not found", "text/plain")
        full = os.path.join(C.WWW, *rel.split("/"))
        if not os.path.isfile(full):
            # service worker 不参与单页回退：把 index.html 当 sw.js 发出去，
            # 注册会"成功"但脚本内容是 HTML，结果是缓存整个坏掉且很难排查。
            if rel in ("sw.js", "manifest.webmanifest"):
                return self._send(404, b"not found", "text/plain")
            # 单页应用回退
            full = os.path.join(C.WWW, "index.html")
            if not os.path.isfile(full):
                return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if rel.endswith(".webmanifest"):
            # 不给对 mime Chrome 直接拒绝解析清单（PWA 安装按钮不会出现）
            ctype = "application/manifest+json"
        elif rel.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        elif rel.endswith(".json"):
            ctype = "application/json; charset=utf-8"
        if ctype.startswith("text/") and "charset" not in ctype:
            ctype += "; charset=utf-8"
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            return self._send(404, b"not found", "text/plain")
        # 网关模式下给 HTML 注入 <base>，保证相对路径静态资源正确解析到 /app/<appname>/
        if ctype.startswith("text/html") and b"<!--BASE-->" in data:
            data = data.replace(b"<!--BASE-->", ('<base href="%s">' % _base_href()).encode("utf-8"), 1)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.end_headers()
        if not head:
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    # ---------- 录像文件（Range） ----------
    def _file(self, path, q, head=False):
        rel = posixpath.normpath(path[len("/rec/"):]).lstrip("/")
        cid = (rel.split("/", 1) or [""])[0]
        if not self._cam_ok_or_deny(cid, "rec_read"):
            return
        root = os.path.realpath(C.recordings_root())
        full = os.path.realpath(os.path.join(root, *rel.split("/")))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            return self._send(404, b"not found", "text/plain")
        if not _view_already_logged(("rec", self._actor(), rel)):
            self._audit("rec_view", cam=cid, file=rel)
        size = os.path.getsize(full)
        rng = self.headers.get("Range") or ""
        start, end = 0, size - 1
        code = 200
        m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
            else:
                start = max(0, size - int(m.group(2)))
                end = size - 1
            end = min(end, size - 1)
            if start > end:
                return self._send(416, b"", "text/plain",
                                  extra={"Content-Range": "bytes */%d" % size})
            code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        # 已写完的分段内容不会再变，放行私有缓存一天：回放页反复进出不再
        # 每次从零下载 —— 一刀切 no-store 是"点回放转圈圈"的另一半原因。
        # 10 分钟内动过的（正在写的最后一段）仍禁缓存，防止播到半截的文件。
        try:
            fresh = time.time() - os.path.getmtime(full) < 600
        except OSError:
            fresh = True
        self.send_header("Cache-Control", "no-store" if fresh else "private, max-age=86400")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if head:
            return
        try:
            with open(full, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---------- 回放 remux（起播优先走这条路） ----------
    def _rec_replay(self, path, q):
        """把录像按起点 remux 成 MPEG-TS 边拷边发（-c copy，零转码）。

        为什么需要：fMP4 录像没有 sidx，浏览器拖到中段要先把前面的字节全下完；
        而整文件渐进下载正是"点回放转圈圈"的主力。这里让服务端用 `-ss`（放在
        `-i` 前，秒级定位）直接从指定起点吐 TS —— 客户端首字节就是关键帧，
        无论第几秒起播都是同一个速度。解不了 H.265 的浏览器仍走 /rech264 兜底。
        """
        rel = posixpath.normpath(path[len("/replay/"):]).lstrip("/")
        if rel.endswith(".ts"):
            rel = rel[:-3]
        cid = (rel.split("/", 1) or [""])[0]
        if not self._cam_ok_or_deny(cid, "replay"):
            return
        root = os.path.realpath(C.recordings_root())
        full = os.path.realpath(os.path.join(root, *rel.split("/")))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            return self._send(404, b"not found", "text/plain")
        try:
            start = max(0.0, float((q.get("start") or ["0"])[0]))
        except ValueError:
            start = 0.0
        try:
            dur = max(0.0, float((q.get("dur") or ["0"])[0]))
        except ValueError:
            dur = 0.0
        if not _view_already_logged(("replay", self._actor(), rel)):
            self._audit("rec_view", cam=cid, file=rel, start=round(start, 1))
        cmd = [os.environ.get("HE_FFMPEG") or "ffmpeg",
               "-hide_banner", "-nostdin", "-loglevel", "error"]
        if start > 0:
            # -ss 必须在 -i 前：放在后面 ffmpeg 会先解码整个前缀才能吐帧
            cmd += ["-ss", "%.3f" % start]
        cmd += ["-i", full]
        if dur > 0:
            cmd += ["-t", "%.3f" % dur]
        cmd += ["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                # fMP4 的帧序重排已经定型，进 TS 前把负 PTS 归零，
                # 否则 mpegts.js 对 DTS 跳变会直接抛 MediaError
                "-avoid_negative_ts", "make_zero",
                "-f", "mpegts", "-mpegts_flags", "+resend_headers", "pipe:1"]
        proc = None
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, bufsize=0)
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass

    # ---------- 回放转码（浏览器不支持 H.265 时用） ----------
    def _rec_transcode(self, path, q):
        """把一段录像实时转成 H.264 的 fMP4 推给浏览器。

        为什么需要：小米摄像头（以及多数新 IPC）输出的是 H.265/HEVC，Chrome /
        Edge 在不支持硬解 HEVC 的机器上根本放不出画面（<video> 只报一个没有
        细节的 error），用户看到的就是"回放打不开"。直播那一路已经转码，回放
        这一路同样要兜底，否则索引修好了、画面还是黑的。

        Range/seek 走不了（边转边发），但回放页的跳转本来就是"换一段重新
        请求"，所以只要支持 start/dur 两个参数即可，起播点用 -ss 精确切。
        """
        rel = posixpath.normpath(path[len("/rech264/"):]).lstrip("/")
        cid = (rel.split("/", 1) or [""])[0]
        if not self._cam_ok_or_deny(cid, "replay_h264"):
            return
        root = os.path.realpath(C.recordings_root())
        full = os.path.realpath(os.path.join(root, *rel.split("/")))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            return self._send(404, b"not found", "text/plain")
        try:
            start = max(0.0, float((q.get("start") or ["0"])[0]))
        except ValueError:
            start = 0.0
        try:
            dur = max(0.5, float((q.get("dur") or ["300"])[0]))
        except ValueError:
            dur = 300.0
        if not _view_already_logged(("h264", self._actor(), rel)):
            self._audit("rec_view", cam=cid, file=rel, start=round(start, 1))
        try:
            cfg = C.load() or {}
            crf = int((cfg.get("live") or {}).get("quality") or 23)
            max_w = int((cfg.get("live") or {}).get("width") or 1280)
        except Exception:
            crf, max_w = 23, 1280
        cmd = [os.environ.get("HE_FFMPEG") or "ffmpeg",
               "-hide_banner", "-nostdin", "-loglevel", "error"]
        if start > 0:
            cmd += ["-ss", "%.3f" % start]
        cmd += ["-i", full, "-t", "%.3f" % dur,
                "-map", "0:v:0", "-map", "0:a:0?",
                "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                "-profile:v", "main", "-pix_fmt", "yuv420p", "-crf", str(crf),
                # 回放也要有声音：原文件里有没有音轨不确定，用可选映射 ?，
                # 有就转 AAC 一起发，没有也不至于让 ffmpeg 直接报错退出。
                "-c:a", "aac", "-b:a", "64k", "-ar", "44100", "-ac", "1"]
        if max_w > 0:
            # 只缩不放；-2 保证高度是偶数（h264 要求）
            cmd += ["-vf", "scale='min(%d,iw)':-2" % max_w]
        cmd += ["-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                "-f", "mp4", "pipe:1"]
        proc = None
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, bufsize=0)
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass

    # ---------- MPEG-TS 直播 ----------
    def _write_chunk(self, data):
        """按 HTTP/1.1 chunked 分帧写出一段数据。        为什么必须 chunked：直播是**无限长**响应，既没有 Content-Length，也不能
        靠 Connection: close 来定界 —— 浏览器 fetch/XHR 对「既没长度也没 chunked」
        的响应当成传输异常处理，mpegts.js 直接抛 NetworkError（表现就是前端一直
        "拉流失败"，但同一地址用 curl 却能正常拿到字节）。
        """
        self.wfile.write(b"%x\r\n" % len(data))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _live_stream(self, path, q):
        cam_id = path[len("/live/"):].split(".")[0]
        cam = C.get_camera(cam_id)
        if not cam:
            return self._send(404, b"no such camera", "text/plain")
        if not self._cam_ok_or_deny(cam_id, "live"):
            return
        # ?q=low 走子码流（没配 url_low 时 LiveHub 会自动回落主码流）
        tier = "low" if (q.get("q") or [""])[0] == "low" else "high"
        # 按类型分流：小米走 XiaomiHub，其余走 LiveHub（ffmpeg 拉流）
        if (cam.get("type") or "rtsp").lower() == "xiaomi":
            import nw_xiaomi as X
            lc = X.get_hub().get(cam)      # 小米 P2P 不开第二路子码流，一律主码流
            sub = lc.add_sub()
            del_fn = lc.del_sub
        else:
            lc = _live.get(cam, tier)
            sub = lc.add_sub()
            del_fn = lc.del_sub
        if not _view_already_logged(("live", self._actor(), str(cam_id), tier)):
            self._audit("live_view", cam=str(cam_id), name=cam.get("name") or "",
                        tier=tier)
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            # 首段要等够：小米摄像头走 P2P，从 run_stream 到第一帧通常要 6~12 秒，
            # 再叠加参数集等待，实测首帧可能到 12.4s 才到。给 40 秒，避免差一点点超时。
            # 只有"根本连不上"（离线/P2P 失败）才提前放弃；录像 muxer 之类的
            # 输出级报错不影响直播，不能据此提前返回，否则会把好的流也掐了。
            first = b""
            t0 = time.time()
            fatal_keys = ("设备离线", "run_stream", "PPCS", "offline", "connect")
            while time.time() - t0 < 40.0:
                first = sub.take(2.0)
                if first:
                    break
                if sub.closed:
                    break             # 发布进程重启，这一条已经作废，别空等 40 秒
                err = (getattr(lc, "error", "") or "")
                no_frame = int(getattr(lc, "frames", 0) or 0) == 0
                if err and no_frame and any(k in err for k in fatal_keys) \
                        and (time.time() - t0) > 8:
                    break
            if not first:
                return
            deadline = time.time() + 3600 * 12
            data = first
            misses = 0
            while time.time() < deadline:
                if data:
                    misses = 0
                    try:
                        self._write_chunk(data)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
                data = sub.take(8.0)
                if data:
                    continue
                if sub.closed:
                    break
                # 一次空转不代表这路流死了：低帧率、等下一个 IDR、看门狗刚重启
                # ffmpeg，都会短暂没数据。以前这里立刻收尾，浏览器就显示
                # NetworkError —— 用户看到的是"画面闪一下又没了"。
                misses += 1
                alive = getattr(lc, "alive", None)
                running = bool(alive()) if callable(alive) else True
                if (running and misses >= 6) or (not running and misses >= 3):
                    break
            # 正常收尾：写终止块，避免浏览器把"连接直接断"当异常
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except Exception:
                pass
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            del_fn(sub)

    # ---------- API ----------
    def _api(self, method, path, q):
        parts = [p for p in path.strip("/").split("/") if p]
        # /api/metrics —— Prometheus 抓取。鉴权在 nw_auth.allows：
        # 带对 HE_METRICS_TOKEN，或用登录会话都放行。
        if parts[:2] == ["api", "metrics"] and method in ("GET", "HEAD"):
            return self._send(200, _metrics_text().encode("utf-8"),
                              "text/plain; version=0.0.4; charset=utf-8",
                              head=(method == "HEAD"))
        # /api/state
        if parts[:2] == ["api", "state"] and method == "GET":
            cfg = C.load()
            sess = self._user()
            av = _live.status()
            rv = _rec.status()
            import nw_xiaomi as X
            xv = {}
            try:
                xv = X.get_hub().status()
            except Exception:
                pass
            cams = []
            # 只读账号只看自己被授权的那几路 —— 在这里过滤，页面才不会先闪出
            # 全部摄像头再逐个报 404（真正的拦截仍然在流/文件出口，见 _cam_ok_or_deny）
            for c in A.visible_cams(sess, cfg.get("cameras") or []):
                cid = str(c.get("id"))
                d = dict(c)
                d.pop("url_password", None)
                # 小米走 XiaomiHub，其余走 LiveHub：两边的取流状态合并给前端，
                # 这样前端能直接显示"设备离线/连不上"的真实原因，而不是干等。
                live_st = _redact_secrets(dict(xv.get(cid) or {}))
                live_st.update(_redact_secrets(dict(av.get(cid) or {})))
                d["live"] = live_st
                d["rec"] = _redact_secrets(dict(rv.get(cid) or {}))
                # 「录哪一路」是全局设置，但每路的后果不一样（有的地址本来就不是
                # *_play），所以按路给出实际会录到什么，让人选之前看得懂。
                d["rec"]["source_note"] = _mask_url(R.source_note(
                    c, (cfg.get("storage") or {}).get("source") or "PLAYED"))
                d["url_masked"] = _mask_url(c.get("url"))
                # 列表里绝不带明文口令：直播页每次刷新都过这里，带上口令等于把
                # 摄像头账号挂在浏览器历史与代理日志里。要看原文走
                # GET /api/cameras/<id>/secret（仅管理员，进审计）。
                d["url"] = d["url_masked"]
                if _has_secret(c.get("url")):
                    d["url_secret"] = True
                if c.get("url_low"):
                    d["url_low_masked"] = _mask_url(c.get("url_low"))
                    d["url_low"] = d["url_low_masked"]
                    d["live_low"] = _redact_secrets(dict(av.get(cid + "#low") or {}))
                # 隐私遮蔽的状态说人话：配了几块、这一路是不是因此必须重编码
                d["privacy"] = PV.summary(c)
                d["masked"] = PV.has(c)
                # 编辑器要拿到具体区域才能画回去（privacy 那两条是给人看的文字）
                d["masks"] = PV.masks_of(c)
                cams.append(d)
            # 云台可用性：前端据此决定是否显示方向盘
            ptz_ok, ptz_why = False, "云台模块未加载"
            try:
                import nw_ptz as _P
                ptz_ok, ptz_why = _P.available()
            except Exception as e:
                ptz_why = str(e)
            return self._j({"ok": True, "version": VERSION, "cameras": cams,
                            "auth": A.public_state(sess),
                            "storage": _rec.usage(),
                            "settings": _public_settings(cfg, admin=A.is_admin(sess)),
                            "xiaomi": X.get_auth().snapshot(),
                            "ptz": {"available": ptz_ok, "reason": ptz_why},
                            "storage_error": (rv.get("_storage") or {}).get("error", "")})

        # ---- 登录闸门 / 会话 ----
        if parts[:2] == ["api", "auth"] and method == "GET":
            return self._j({"ok": True,
                            "auth": A.public_state(self._user())})

        if parts[:2] == ["api", "login"] and method == "POST":
            body = self._body()
            token = A.login(body.get("username"), body.get("password"),
                            peer=self._ip())
            if not token:
                return self._err("用户名或密码不正确（连续失败会临时锁定 15 分钟）", 401)
            return self._send(200, _json_bytes(
                {"ok": True, "auth": A.public_state(A.current(token))}),
                extra={"Set-Cookie": A.cookie(token)})

        if parts[:2] == ["api", "logout"] and method == "POST":
            tok = A.token_from_headers(self.headers)
            A.logout(tok, peer=self._ip())
            return self._send(200, _json_bytes(
                {"ok": True, "auth": A.public_state(None)}),
                extra={"Set-Cookie": A.cookie("", logout=True)})

        if parts[:2] == ["api", "password"] and method == "POST":
            # 还没设过密码 = 首次认领，免登录；已设过则必须带原密码
            if A.required() and not self._logged_in():
                return self._err("需要登录", 401)
            # 这一条改的始终是**内置管理员**的口令（口令存在 cfg["auth"] 里），
            # 所以只读账号不该碰它：他的密码由管理员在「用户与授权」里维护。
            # 不加这道判断的话，任何登录态都能拿"猜中的旧密码"把管理员换掉。
            if A.configured() and not A.is_admin(self._user()):
                return self._err("只有管理员能改内置登录密码；"
                                 "只读账号的密码请让管理员在「用户与授权」里改", 403)
            body = self._body()
            ok, msg = A.set_password(body.get("password"), body.get("old_password"))
            if not ok:
                return self._err(msg, 400)
            token = A.login(A.username(), body.get("password"), peer=self._ip())
            self._audit("password_change")
            return self._send(200, _json_bytes(
                {"ok": True, "msg": msg,
                 "auth": A.public_state(A.current(token) if token else None)}),
                extra={"Set-Cookie": A.cookie(token)} if token else None)

        # ---- 业务链路健康 ----
        if parts[:2] == ["api", "health"] and method == "GET":
            fresh = (q.get("fresh") or ["0"])[0] in ("1", "true", "yes")
            s = H.summary(force=fresh)
            if not self._logged_in():
                # 容器 healthcheck 里的 curl 不带 Cookie，只给它聚合结论，
                # 不外泄摄像头名字与状态流水
                s = {k: s.get(k) for k in
                     ("healthy", "status", "reason", "counts", "monitored", "at")}
            else:
                # 登录态才带看门狗：自愈流水里有摄像头名与设备地址
                s = dict(s)
                s["watchdog"] = W.brief()
                # 运行环境自检（ffmpeg / Python / 小米 SDK 依赖）也只在登录态给：
                # 里面有宿主机路径，不该出现在无凭据的 healthcheck 响应里
                try:
                    s["env"] = E.report()
                except Exception as e:
                    s["env"] = {"warnings": ["自检失败：%s" % e]}
            # 只有 broken 才让状态码变红：主卧每晚断电不该把容器拖着重启
            return self._send(200 if s.get("healthy") else 503,
                              _json_bytes({"ok": True, "version": VERSION,
                                           "health": s}))

        # ---- 看门狗：心跳/自愈流水，以及"现在就修一次" ----
        # 登录与否由 `nw_auth.allows` 决定（网关模式由飞牛鉴权），这里不再重复判
        if parts[:2] == ["api", "watchdog"]:
            if method == "POST":
                cid = (self._body().get("cam") or "").strip()
                cam = C.get_camera(cid) if cid else None
                if cid and not cam:
                    return self._err("没有这路摄像头：%s" % cid, 404)
                cams = [cam] if cam else [
                    c for c in (C.load().get("cameras") or [])
                    if c.get("enabled", True)]
                w = W._wd()
                out = []
                for c in cams:
                    # 手动修不受冷却限制：用户点了就是明确要求
                    with W._lock:
                        W._state["next"].pop(str(c.get("id")), None)
                    what, detail = W.repair(c, "手动触发", w)
                    out.append({"cam": c.get("id"), "action": what,
                                "detail": detail})
                return self._j({"ok": True, "results": out, "watchdog": W.state()})
            s = W.state()
            if not self._logged_in():
                # 未登录（网关模式）只给"看门狗还活着"，不漏自愈流水
                s = {"enabled": s.get("enabled"), "beat_age": s.get("beat_age"),
                     "hearts": s.get("hearts")}
            return self._j({"ok": True, "watchdog": s})

        # ---- 小米 OAuth2 扫码 ----
        if parts[:2] == ["api", "xiaomi"]:
            return self._xiaomi_api(method, parts, q)

        # ---- 设备侧侦测 / 摄像头自身录像 / SD 卡 ----
        if parts[:2] == ["api", "motion"]:
            return self._motion_api(method, parts, q)

        # ---- 本地事件侦测（时间轴事件块）----
        if parts[:2] == ["api", "events"]:
            return self._events_api(method, parts, q)

        # ---- 本地 AI：状态与自然语言检索 ----
        if parts[:2] == ["api", "ai"]:
            return self._ai_api(method, parts, q)

        # ---- 审计日志："谁看过哪段" ----
        if parts[:2] == ["api", "audit"]:
            return self._audit_api(method, parts, q)

        # ---- 录像完整性自检（丢帧/空洞清单）----
        if parts[:2] == ["api", "integrity"]:
            return self._integrity_api(method, parts, q)

        # ---- 冷层归档（rclone 上云）----
        if parts[:2] == ["api", "archive"]:
            return self._archive_api(method, parts, q)

        # ---- 片段导出 mp4 ----
        if parts[:2] == ["api", "export"]:
            return self._export_api(method, parts, q)

        # ---- 带有效期的分享链接 ----
        if parts[:2] == ["api", "shares"]:
            return self._shares_api(method, parts, q)

        # ---- 多用户与按摄像头授权 ----
        if parts[:2] == ["api", "users"]:
            return self._users_api(method, parts, q)

        # ---- 云台 PTZ ----
        if parts[:2] == ["api", "ptz"]:
            return self._ptz_api(method, parts, q)

        # ---- 对讲（上行语音）----
        if parts[:2] == ["api", "talk"]:
            return self._talk_api(method, parts, q)

        if parts[:2] == ["api", "storage"] and method == "GET":
            if len(parts) == 3 and parts[2] == "roots":
                # 挂载点清单只给登录态看（暴露NAS磁盘结构）：nw_auth.allows 已经把
                # /api 全拦在门外，这里不需要重复判
                return self._j({"ok": True, "roots": C.disk_roots(),
                                "current": C.recordings_root()})
            return self._j({"ok": True, "usage": _rec.usage()})

        if parts[:2] == ["api", "notify"]:
            # POST /api/notify/test -> 向所有已配置渠道各发一条测试通知
            if len(parts) == 3 and parts[2] == "test" and method == "POST":
                try:
                    import nw_notify as N
                except Exception as e:
                    return self._err("通知模块不可用：%s" % e, 500)
                ok, detail = N.send(
                    "【HomeEye】测试通知",
                    "如果你收到这条消息，说明 HomeEye 的通知渠道配置正确。\n\n"
                    "时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"))
                return self._j({"ok": ok, "error": detail,
                                "channels": N.channels_status()})
            return self._err("没有这个通知接口：%s" % "/".join(parts), 404)

        if parts[:2] == ["api", "settings"]:
            if method == "GET":
                # 只读账号不需要看 SMTP 地址、归档远端这些配置项
                if self._require_admin() is None:
                    return
                return self._j({"ok": True, "settings": _public_settings(C.load())})
            if method in ("PUT", "POST"):
                sess = self._require_admin()
                if sess is None:
                    return
                body = self._body()
                cfg = C.load()
                notes = []
                # 落盘前先记住原目录：新目录写不进去时要退回它（C.load 拿的是缓存
                # 里同一个对象，改完再读就没得读了）
                old_dir = (cfg.get("storage") or {}).get("dir") or ""
                old_live = dict(cfg.get("live") or {})
                # 每一组只接受白名单字段，避免前端误传把配置写脏；
                # 收进来以后一律过 C.normalize 钳一遍再落盘 —— 存的就是跑的。
                for group, allowed in (("storage", ("dir", "retention_days",
                                                    "segment_minutes", "budget_gb",
                                                    "record_mode", "record_audio",
                                                    "source", "keep_events_days")),
                                       ("live", ("width", "fps", "quality", "transcode",
                                                 "idle_stop", "default_quality",
                                                 "autoplay", "audio")),
                                       ("notify", ("enabled", "smtp_host", "smtp_port",
                                                   "smtp_ssl", "smtp_user", "smtp_pass",
                                                   "from_addr", "to_addrs", "on_motion",
                                                   "on_fault", "fault_cooldown_s",
                                                   "dnd_enabled", "dnd_start", "dnd_end",
                                                   "wecom_webhook", "ding_webhook",
                                                   "ding_secret", "bark_url",
                                                   "webhook_url", "arming_mode",
                                                   "arming_ranges", "digest_enabled",
                                                   "digest_hour", "link_base")),
                                       ("auth", ("enabled", "username", "session_hours")),
                                       ("ai", ("enabled", "audio_events",
                                               "audio_threshold", "objects",
                                               "model_path", "labels", "input_size",
                                               "obj_confidence", "vlm_enabled",
                                               "vlm_url", "vlm_key", "vlm_model")),
                                       ("archive", ("enabled", "remote", "older_days",
                                                    "delete_local", "auto",
                                                    "auto_hour")),
                                       ("integrity", ("enabled", "days",
                                                      "gap_tolerance"))):
                    sec = body.get(group)
                    if not isinstance(sec, dict):
                        continue
                    picked = {k: v for k, v in sec.items() if k in allowed}
                    # 密码不回显给前端，所以前端提交空串时表示「不修改」
                    if group == "notify" and not picked.get("smtp_pass"):
                        picked.pop("smtp_pass", None)
                    if group == "notify" and not picked.get("ding_secret"):
                        picked.pop("ding_secret", None)
                    if group == "ai" and not picked.get("vlm_key"):
                        picked.pop("vlm_key", None)
                    base = dict(cfg.get(group) or {})
                    cfg[group] = C.normalize(group, picked, base)
                    notes += C.diff_notes(group, picked, cfg[group])
                if isinstance(body.get("motion"), dict):
                    # enabled/sensitivity 是本地事件侦测的真实参数（nw_events），
                    # 过 C.normalize 钳一遍 —— 存的就是跑的；其余占位键
                    # （预录/补录等，见 C.UNHONORED）照旧原样存，不假装生效。
                    picked = {k: v for k, v in body["motion"].items()
                              if k in ("enabled", "sensitivity", "detect_persons",
                                       "pre_seconds", "post_seconds", "min_frames",
                                       "max_event_seconds")}
                    honored = {k: picked[k] for k in ("enabled", "sensitivity")
                               if k in picked}
                    cfg.setdefault("motion", {}).update(picked)
                    if honored:
                        cfg["motion"] = C.normalize("motion", honored,
                                                    cfg.get("motion"))
                    notes += C.diff_notes("motion", picked, cfg["motion"])
                ptz_restart = False
                if isinstance(body.get("ptz"), dict):
                    pz = dict(body["ptz"])
                    # token 不回显，前端提交空串表示「不修改」
                    if not pz.get("token"):
                        pz.pop("token", None)
                    if not pz.get("g2r_pass"):
                        pz.pop("g2r_pass", None)
                    old = dict(cfg.get("ptz") or {})
                    picked = {k: v for k, v in pz.items()
                              if k in ("enabled", "addr", "user", "token", "region",
                                       "step_seconds", "speed", "media_audio",
                                       "idle_seconds", "mode", "g2r_addr",
                                       "g2r_user", "g2r_pass")}
                    cfg["ptz"] = C.normalize("ptz", picked, cfg.get("ptz"))
                    notes += C.diff_notes("ptz", picked, cfg["ptz"])
                    # 只有影响侧车进程的字段变了才重启：单纯改「点一下转多久」
                    # 居然把会话打断，是不可接受的（断会话=摄像头那边要重连几秒）。
                    for k in ("enabled", "addr", "user", "token", "region",
                              "media_audio", "idle_seconds", "mode",
                              "g2r_addr"):
                        if old.get(k) != cfg["ptz"].get(k):
                            ptz_restart = True
                            break
                    if old.get("g2r_addr") != cfg["ptz"].get("g2r_addr") or \
                            old.get("g2r_pass") != cfg["ptz"].get("g2r_pass"):
                        import nw_ptz as _P
                        _P.forget_endpoints()
                if isinstance(body.get("watchdog"), dict):
                    # 钳制在落盘前做：设置页回显的就是真正跑起来的值。
                    # 范围只定义在 nw_watchdog 一处（读的时候也用它），两边不会漂。
                    cfg["watchdog"] = W.normalize(body["watchdog"], cfg.get("watchdog"))
                    notes += C.diff_notes("watchdog", body["watchdog"], cfg["watchdog"])
                # 录像目录：写不进去就不落盘（宁可当场报错，也不留一个"设置成功、
                # 从此不再长录像"的现场）
                if isinstance(body.get("storage"), dict) and "dir" in body["storage"]:
                    want_dir = cfg["storage"].get("dir") or C.recordings_root(cfg)
                    ok, err = C.ensure_writable(want_dir)
                    if not ok:
                        cfg["storage"]["dir"] = old_dir
                        notes.append("录像目录用不了：%s（保持原目录 %s 不变）" % (
                            err, old_dir or C.recordings_root(cfg)))
                if isinstance(body.get("auth"), dict) and "username" in body["auth"]:
                    # 用户名改动不该被钳成空串把人锁在门外
                    if not cfg["auth"].get("username"):
                        cfg["auth"]["username"] = "admin"
                C.save(cfg)
                # 审计记"改了哪几组 + 钳制说明"，不记具体值：设置里有 SMTP 口令、
                # passToken、VLM key，写进日志等于把秘密抄到另一个文件里
                self._audit("settings_change",
                            groups=",".join(sorted(k for k in body
                                                   if isinstance(body.get(k), dict))),
                            notes=" | ".join(notes)[:400])
                if ptz_restart:
                    try:
                        import nw_ptz as _P
                        _P.restart()
                    except Exception:
                        pass
                if isinstance(body.get("storage"), dict) and "dir" in body["storage"]:
                    try:
                        H.invalidate_storage()
                    except Exception:
                        pass
                _live.drop_all()
                if isinstance(body.get("live"), dict) and \
                        (cfg.get("live") or {}) != old_live:
                    # 小米那一路的 ffmpeg 是 XiaomiHub 自己起的，drop_all() 管不到它。
                    # live 参数一改就要把这路也重建，否则页面显示新参数、实际还在按
                    # 旧参数转码 —— 这种"改了看不出来"正是 §4.1 要避免的东西。
                    try:
                        import nw_xiaomi as X
                        X.get_hub().drop_all()
                    except Exception:
                        pass
                try:
                    _rec.reconcile()
                except Exception:
                    pass
                return self._j({"ok": True, "notes": notes,
                                "settings": _public_settings(C.load())})

        if parts[:2] == ["api", "cameras"]:
            if len(parts) == 2 and method == "POST":
                if self._require_admin() is None:
                    return
                return self._camera_create()
            if len(parts) >= 3:
                cid = parts[2]
                cam = C.get_camera(cid)
                if not cam:
                    return self._err("摄像头不存在", 404)
                if len(parts) == 3 and method == "PUT":
                    if self._require_admin() is None:
                        return
                    return self._camera_update(cam)
                if len(parts) == 3 and method == "DELETE":
                    if self._require_admin() is None:
                        return
                    cfg = C.load()
                    cfg["cameras"] = [c for c in cfg["cameras"] if str(c.get("id")) != str(cid)]
                    C.save(cfg)
                    _live.drop(cid)
                    self._audit("cam_delete", cam=str(cid),
                               name=cam.get("name") or "")
                    try:
                        _rec.reconcile()
                    except Exception:
                        pass
                    return self._j({"ok": True})
                if len(parts) == 4 and parts[3] == "test":
                    if self._require_admin() is None:
                        return
                    ok, info = _ffprobe(cam.get("url") or "", cam.get("transport") or "tcp")
                    # ffprobe 的报错原话里带着完整地址（含口令），不能直接回给页面
                    return self._j({"ok": ok,
                                    "info": _redact_secrets(info) if ok else None,
                                    "error": "" if ok else _mask_text(info)})
                if len(parts) == 4 and parts[3] == "secret":
                    # 列表里只给掩码，但设置页要能核对完整地址（填错一个字符
                    # 就连不上），所以单独开一条管理员接口，并且记审计。
                    if self._require_admin() is None:
                        return
                    self._audit("cam_secret_reveal", cam=str(cid))
                    return self._j({"ok": True, "url": cam.get("url") or "",
                                    "url_low": cam.get("url_low") or ""})
                if len(parts) == 4 and parts[3] == "snapshot":
                    if not self._cam_ok_or_deny(cid, "snapshot"):
                        return
                    return self._snapshot(cam)
                if len(parts) == 4 and parts[3] == "relive":
                    if self._require_admin() is None:
                        return
                    _live.drop(cid)
                    return self._j({"ok": True})

        if parts[:2] == ["api", "onvif"]:
            return self._onvif_api(method, parts)

        if parts[:2] == ["api", "recordings"] and method == "GET":
            cam = (q.get("cam") or [""])[0]
            if not cam or not C.get_camera(cam):
                return self._err("缺少摄像头", 400)
            if not self._cam_ok_or_deny(cam, "recordings"):
                return
            return self._j({"ok": True, "days": _rec.days_with_data(cam),
                            "segments": self._segments_for(cam, q)})

        if parts[:2] == ["api", "playlist"] and method == "GET":
            cam = (q.get("cam") or [""])[0]
            if not cam or not C.get_camera(cam):
                return self._err("缺少摄像头", 400)
            if not self._cam_ok_or_deny(cam, "playlist"):
                return
            try:
                t0 = int(float((q.get("from") or ["0"])[0]))
                t1 = int(float((q.get("to") or ["0"])[0]))
            except ValueError:
                return self._err("时间参数不合法", 400)
            if t1 <= t0:
                t1 = t0 + 86400
            return self._j({"ok": True, "segments": _rec.playlist(cam, t0, t1)})

        return self._err("没有这个接口：%s" % path, 404)

    def _segments_for(self, cam, q):
        day = (q.get("date") or [""])[0]
        if not day:
            day = time.strftime("%Y-%m-%d")
        return _rec.segments(cam, day)

    def _events_api(self, method, parts, q):
        """本地事件侦测：时间轴事件块 + 缩略图墙。

        GET  /api/events?cam=&date=YYYY-MM-DD   某路某天的事件列表
        GET  /api/events/latest?since=ts        指定时刻之后的新事件（通知轮询）
        GET  /api/events/status                 扫描器状态（设置页显示）
        POST /api/events/scan                   立即扫一轮（调完不等结果）
        GET  /api/events/thumb/<cam>/th/x.jpg   事件缩略图（文件名不可枚举出内容）

        事件里带缩略图，等于带画面 —— 每一路都要单独过按摄像头授权。
        """
        if method == "GET" and len(parts) == 3 and parts[2] == "status":
            mo = (C.load().get("motion") or {})
            return self._j({"ok": True,
                            "enabled": bool(mo.get("enabled")),
                            "sensitivity": mo.get("sensitivity", 50),
                            "scanner": EV.status()})
        if method == "POST" and len(parts) == 3 and parts[2] == "scan":
            if self._require_admin() is None:
                return
            EV.request_scan()
            return self._j({"ok": True})
        if method == "GET" and len(parts) >= 3 and parts[2] == "thumb":
            return self._event_thumb(parts)
        if method == "GET" and len(parts) == 3 and parts[2] == "latest":
            sess = self._user()
            if sess is None:
                return self._err("需要登录", 401)
            try:
                since = int(float((q.get("since") or ["0"])[0]))
            except ValueError:
                since = int(time.time()) - 3600
            items = AI.latest(C.recordings_root(), since,
                              cam_ids=[str(x) for x in (sess.get("cams") or [])])
            return self._j({"ok": True, "events": items,
                            "now": int(time.time())})
        if method == "GET" and len(parts) == 2:
            cam = (q.get("cam") or [""])[0]
            if not cam or not C.get_camera(cam):
                return self._err("缺少摄像头", 400)
            if not self._cam_ok_or_deny(cam, "events"):
                return
            compact = re.sub(r"[^0-9]", "", (q.get("date") or [""])[0])[:8]
            if len(compact) != 8:
                return self._err("日期不合法", 400)
            return self._j({"ok": True,
                            "events": EV.read_day(C.recordings_root(), cam, compact)})
        return self._err("没有这个事件接口：%s" % "/".join(parts), 404)

    def _event_thumb(self, parts):
        rel = "/".join(parts[3:])
        if len(parts) < 4 or not self._cam_ok_or_deny(parts[3], "thumb"):
            return
        if not rel.endswith(".jpg") or ".." in rel.split("/"):
            return self._send(404, b"not found", "text/plain")
        root = os.path.realpath(os.path.join(C.recordings_root(), "events"))
        full = os.path.realpath(os.path.join(root, *rel.split("/")))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            return self._send(404, b"not found", "text/plain")
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            return self._send(404, b"not found", "text/plain")
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---------- 本地 AI：状态与自然语言检索 ----------
    def _ai_api(self, method, parts, q):
        """/api/ai/status 只看得到"能不能跑、为什么没跑"；检索按授权过滤。

        诚实化：模型没装、依赖缺失、VLM 没配，都由 `AI.status()` 给出原因，
        页面直接显示 —— 不假装"已开启"。
        """
        if len(parts) == 3 and parts[2] == "status" and method == "GET":
            try:
                return self._j({"ok": True, "ai": AI.status()})
            except Exception as e:
                return self._err("AI 状态读取出错：%s" % e, 500)
        if len(parts) == 3 and parts[2] == "search" and method in ("GET", "POST"):
            body = self._body() if method == "POST" else {}
            text = (body.get("q") or (q.get("q") or [""])[0] or "").strip()
            if not text:
                return self._err("请输入检索内容，例如：昨天下午客厅有人", 400)
            sess = self._user() or {}
            cam_ids = [str(x) for x in (sess.get("cams") or [])]
            try:
                res = AI.search(C.recordings_root(), text, cam_ids=cam_ids)
            except Exception as e:
                return self._err("检索失败：%s" % e, 500)
            self._audit("ai_search", hits=res.get("total", 0), text=text[:60])
            return self._j(res)
        return self._err("没有这个 AI 接口：%s" % "/".join(parts), 404)

    # ---------- 审计日志 ----------
    def _audit_api(self, method, parts, q):
        """/api/audit —— 只有管理员能看（里面全是"谁在什么时候看了哪儿"）。"""
        if self._require_admin() is None:
            return
        if len(parts) == 3 and parts[2] == "kinds" and method == "GET":
            return self._j({"ok": True, "kinds": AU.kinds()})
        if method == "GET":
            try:
                days = max(0, min(int((q.get("days") or ["7"])[0]), 90))
            except ValueError:
                days = 7
            try:
                limit = max(1, min(int((q.get("limit") or ["500"])[0]), 5000))
            except ValueError:
                limit = 500
            rows = AU.read(days=days,
                           kind=(q.get("kind") or [None])[0],
                           actor=(q.get("actor") or [None])[0],
                           limit=limit)
            return self._j({"ok": True, "rows": rows, "status": AU.status()})
        return self._err("没有这个审计接口：%s" % "/".join(parts), 404)

    # ---------- 录像完整性自检 ----------
    def _integrity_api(self, method, parts, q):
        if len(parts) == 3 and parts[2] == "scan" and method == "POST":
            if self._require_admin() is None:
                return
            body = self._body()
            cam = str(body.get("cam") or "").strip() or None
            res = IG.take_snapshot(cams=[cam] if cam else None,
                                   deep=bool(body.get("deep")))
            self._audit("integrity_scan", cam=cam or "all", deep=bool(body.get("deep")))
            return self._j({"ok": True, **res})
        sess = self._user()
        if sess is None:
            return self._err("需要登录", 401)
        st = IG.status()
        # 只读账号只看自己那几路的空洞清单
        if not A.is_admin(sess):
            allow = {str(x) for x in (sess.get("cams") or [])}
            res = st.get("result") or {}
            res = dict(res)
            res["cams"] = [c for c in (res.get("cams") or [])
                           if str(c.get("cam")) in allow]
            res["gaps_total"] = sum(len(c.get("gaps") or []) for c in res["cams"])
            res["bad_total"] = sum(len(c.get("bad") or []) for c in res["cams"])
            st = dict(st)
            st["result"] = res
        return self._j({"ok": True, "integrity": st})

    # ---------- 冷层归档 ----------
    def _archive_api(self, method, parts, q):
        if self._require_admin() is None:
            return
        if len(parts) == 3 and parts[2] == "run" and method == "POST":
            body = self._body()
            res = AR.run(cam=str(body.get("cam") or "").strip() or None,
                         older_days=body.get("older_days"))
            self._audit("archive_run", started=res.get("started"),
                        error=res.get("error") or "")
            return self._j({"ok": True, **res})
        if len(parts) == 3 and parts[2] == "cancel" and method == "POST":
            self._audit("archive_cancel")
            return self._j(AR.cancel())
        return self._j({"ok": True, "archive": AR.status()})

    # ---------- 片段导出 ----------
    def _export_api(self, method, parts, q):
        """导出是"把原片拿走"的动作，只给管理员；每次导出与分享都进审计。"""
        if self._require_admin() is None:
            return
        if method == "POST" and len(parts) == 2:
            body = self._body()
            job, err = EX.create(body.get("cam"), body.get("from"), body.get("to"),
                                 mask=bool(body.get("mask", True)),
                                 note=body.get("note") or "",
                                 actor=self._actor())
            if err:
                return self._err(err, 400)
            self._audit("export_create", cam=job["cam"], frm=job["from"],
                        to=job["to"], mask=bool(job["mask"]), job=job["id"])
            return self._j({"ok": True, "job": EX.public_job(job)})
        if method == "GET" and len(parts) == 2:
            return self._j({"ok": True, "jobs": EX.list_jobs()})
        if len(parts) >= 3:
            jid = parts[2]
            if len(parts) == 3 and method == "GET":
                job = EX.get(jid)
                if not job:
                    return self._err("没有这个导出任务（服务重启会丢掉进度）", 404)
                return self._j({"ok": True, "job": EX.public_job(job)})
            if len(parts) == 4 and parts[3] == "share" and method == "POST":
                body = self._body()
                rec, err = EX.share(jid, hours=body.get("hours", 24),
                                    actor=self._actor(), ip=self._ip())
                if err:
                    return self._err(err, 400)
                return self._j({"ok": True, "share": _share_row(rec, self)})
        return self._err("没有这个导出接口：%s" % "/".join(parts), 404)

    # ---------- 分享链接管理 ----------
    def _shares_api(self, method, parts, q):
        if self._require_admin() is None:
            return
        if method == "DELETE" and len(parts) == 3:
            tok = urllib.parse.unquote(parts[2])
            ok, msg = EX.revoke(tok)
            self._audit("share_revoke", token=tok[:6] + "…", ok=ok)
            return self._j({"ok": ok, "msg": msg})
        if method == "GET":
            return self._j({"ok": True,
                            "shares": [_share_row(s, self) for s in EX.shares()]})
        return self._err("没有这个分享接口：%s" % "/".join(parts), 404)

    # ---------- 多用户与按摄像头授权 ----------
    def _users_api(self, method, parts, q):
        if self._require_admin() is None:
            return
        if method == "GET" and len(parts) == 2:
            return self._j({"ok": True, "users": A.users()})
        if method == "POST" and len(parts) == 2:
            body = self._body()
            user, err = A.create_user(body.get("name"), body.get("password"),
                                      role=body.get("role") or "read",
                                      cams=body.get("cams"))
            if err:
                return self._err(err, 400)
            self._audit("user_create", target=user.get("name"),
                        role=user.get("role"))
            return self._j({"ok": True, "user": user})
        if len(parts) == 3:
            name = urllib.parse.unquote(parts[2])
            if method in ("PUT", "POST"):
                body = self._body()
                user, err = A.update_user(
                    name, password=body.get("password") or None,
                    role=body.get("role"), cams=body.get("cams"),
                    enabled=body.get("enabled"))
                if err:
                    return self._err(err, 400)
                self._audit("user_update", target=name)
                return self._j({"ok": True, "user": user})
            if method == "DELETE":
                ok, msg = A.delete_user(name)
                self._audit("user_delete", target=name, ok=ok)
                return self._j({"ok": ok, "msg": msg})
        return self._err("没有这个用户接口：%s" % "/".join(parts), 404)

    # ---------- 免登录的分享页 / 导出原片 ----------
    def _share_route(self, path, head=False):
        tok = urllib.parse.unquote(path[len("/share/"):]).strip("/")
        if "/" in tok:
            # /share/<token>/video.mp4 —— 只发这一路导出文件，不带其它能力
            tok, _sub = tok.split("/", 1)
            return self._share_video(tok)
        rec, why = EX.share_of(tok or "")
        if why != "ok":
            msg = {"notfound": "链接不存在或已被撤销",
                   "expired": "链接已过期",
                   "gone": "视频文件已被清理"}[why]
            return self._send(404, _share_error_page(msg).encode("utf-8"),
                              "text/html; charset=utf-8", head=head)
        base = _share_base(self, tok)
        return self._send(200, EX._share_page(rec, base).encode("utf-8"),
                          "text/html; charset=utf-8", head=head)

    def _share_video(self, tok):
        rec, why = EX.share_of(tok or "")
        if why != "ok":
            return self._send(404, b"not found", "text/plain")
        self._audit("share_hit", cam=rec.get("cam"), share=tok[:6] + "…")
        return self._send_video_file(str(rec.get("file") or ""))

    def _export_route(self, path, head=False):
        """/export/<id>/file.mp4 —— 登录 + 授权以后取导出原片。"""
        sess = self._user()
        if sess is None:
            return self._err("需要登录", 401)
        parts = [p for p in path.strip("/").split("/") if p]
        if len(parts) < 3 or parts[2] != "file.mp4":
            return self._send(404, b"not found", "text/plain")
        job = EX.get(parts[1])
        if not job:
            return self._send(404, b"not found", "text/plain")
        if not A.cam_ok(sess, job.get("cam")):
            return self._send(404, b"not found", "text/plain")
        if job.get("state") != "done":
            return self._err("导出还没完成（当前 %s%%）" % job.get("pct"), 409)
        self._audit("export_download", cam=job.get("cam"), job=job.get("id"))
        # 下载名用中性的任务号，不带摄像头真名（导出件经常要转给别人）
        return self._send_video_file(str(job.get("file") or ""),
                                     attach_name="homeeye-%s.mp4" % job.get("id"))

    def _send_video_file(self, full, attach_name=""):
        """带 Range 的视频文件下发（导出与分享共用），路径必须是绝对且存在的。"""
        full = os.path.realpath(full or "")
        allowed = os.path.realpath(EX.exports_dir())
        if not full.startswith(allowed + os.sep) or not os.path.isfile(full):
            return self._send(404, b"not found", "text/plain")
        size = os.path.getsize(full)
        rng = self.headers.get("Range") or ""
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)", rng.strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                return self._send(416, b"", "text/plain",
                                  extra={"Content-Range": "bytes */%d" % size})
            code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        # 导出走下载（attachment），分享页要内嵌播放（inline）
        cd = ('attachment; filename="%s"' % attach_name) if attach_name else "inline"
        self.send_header("Content-Disposition", cd)
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        try:
            with open(full, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _onvif_api(self, method, parts):
        """ONVIF 设备发现。

        POST /api/onvif/scan                      扫描局域网
        POST /api/onvif/probe  {"host","port","user","pass"}
                                                  取指定设备的 RTSP 地址
        """
        try:
            import nw_onvif as O
        except Exception as e:
            return self._err("ONVIF 模块不可用：%s" % e, 500)
        # 扫描与探测都要带设备账号，且是在"添加摄像头"这条路上 —— 只给管理员
        if self._require_admin() is None:
            return

        if len(parts) == 3 and parts[2] == "scan" and method == "POST":
            try:
                devs = O.discover(timeout=3.0)
            except Exception as e:
                return self._err("扫描失败：%s" % e, 500)
            out = []
            for d in devs:
                out.append({
                    "host": d.get("host", ""),
                    "port": d.get("port", 80),
                    "name": d.get("name") or d.get("host", ""),
                    # 先给一个按常见品牌猜出来的地址，用户还能改
                    "url": "rtsp://%s:554/Streaming/Channels/101" % (d.get("host", ""),),
                    "xaddrs": d.get("xaddrs", ""),
                "candidates": O.guess_rtsp(d.get("host", ""), d.get("port", 80)),
                "type": "onvif",
                "detected": True,
                "manual": False,
                "enabled": True,
                "record": True,
                "width": 0,
                "height": 0,
                "fps": 0,
                "transport": "tcp",
            })
            return self._j({"ok": True, "devices": out})

        if len(parts) == 3 and parts[2] == "probe" and method == "POST":
            body = self._body()
            host = (body.get("host") or "").strip()
            if not host:
                return self._err("缺少 host", 400)
            try:
                uris = O.get_stream_uris(host, body.get("port") or 80,
                                         body.get("user") or "",
                                         body.get("pass") or "")
            except Exception as e:
                return self._err("探测失败：%s" % e, 500)
            return self._j({"ok": True, "streams": uris})

        return self._err("没有这个 ONVIF 接口：%s" % "/".join(parts), 404)

    def _ptz_api(self, method, parts, q):
        """云台（PTZ）。真正干活的常驻侧车在 nw_ptz，这里只做参数校验和转发。"""
        import nw_ptz as P

        # GET /api/ptz/status -> 各摄像头的云台状态与当前角度
        if len(parts) == 3 and parts[2] == "status" and method == "GET":
            return self._j(P.status())

        # POST /api/ptz/move  {"cam": "c2", "op": 1, "hold": 0.5}
        if len(parts) == 3 and parts[2] == "move" and method == "POST":
            body = self._body()
            cam = str(body.get("cam") or "")
            if not cam:
                return self._err("缺少 cam", 400)
            # 转动摄像头是"改变物理世界"的动作：只读账号既不能转别人的头，
            # 也不能转没授权给它的那一路
            if self._require_admin() is None:
                return
            if not self._cam_ok_or_deny(cam, "ptz_move"):
                return
            try:
                op = int(body.get("op"))
            except (TypeError, ValueError):
                return self._err("缺少 op", 400)
            if op < 0 or op > 15:
                return self._err("op 超出范围（0 停 / 1 右 / 2 左 / 3 上 / 4 下）", 400)
            hold = body.get("hold")
            try:
                hold = float(hold) if hold is not None else None
            except (TypeError, ValueError):
                hold = None
            if hold is not None:
                hold = max(0.05, min(hold, 5.0))
            try:
                res = P.move(cam, op, hold)
            except P.PtzError as e:
                return self._err("云台指令失败：%s" % e, 502, need_session=e.need_session)
            except Exception as e:
                return self._err("云台指令失败：%s" % e, 502)
            return self._j({"ok": True, "result": res})

        # POST /api/ptz/import -> 从本机 go2rtc 配置导入米家凭据
        if len(parts) == 3 and parts[2] == "import" and method == "POST":
            ok, msg = P.import_from_go2rtc()
            return self._j({"ok": ok, "message": msg})

        # POST /api/ptz/migrate -> 把小米摄像头改成走 go2rtc 取流（不占会话名额）
        if len(parts) == 3 and parts[2] == "migrate" and method == "POST":
            changed, skipped = P.migrate_to_go2rtc()
            # 这里 ok 恒为 True：迁移这个**动作**本身是成功的，
            # 「没有可迁移的」只是结果说明，不能当成错误抛给前端。
            return self._j({
                "ok": True,
                "changed": [{"id": c[0], "name": c[1], "url": c[2]} for c in changed],
                "skipped": [{"id": s[0], "reason": s[1]} for s in skipped],
                "message": ("已迁移 %d 路摄像头到 go2rtc 取流" % len(changed))
                           if changed else "没有可迁移的摄像头（go2rtc 配置里找不到对应流）",
            })

        # POST /api/ptz/restart -> 重启侧车（改完凭据后）
        if len(parts) == 3 and parts[2] == "restart" and method == "POST":
            P.restart()
            return self._j({"ok": True, "available": P.available()})

        return self._err("没有这个云台接口：%s" % "/".join(parts), 404)

    def _talk_api(self, method, parts, q):
        """对讲（上行语音）。音频是裸 PCM，body 直接当二进制读，不走 JSON。"""
        import nw_ptz as P

        # 对着摄像头喇叭喊话会真的在家里出声，只给管理员，而且要看它有没有
        # 这一路的授权。push 的 body 是裸 PCM，绝不能按 JSON 读（会把 rfile
        # 吃掉，后面 _raw_body 就什么都读不到了），所以它的 cam 只从 query 取。
        key = parts[2] if len(parts) == 3 else ""
        cam = ""
        if key == "push":
            cam = (q.get("cam") or [""])[0]
        elif key in ("start", "stop", "beep"):
            cam = str(self._body().get("cam") or "")
        if self._require_admin() is None:
            return
        if key and cam and not self._cam_ok_or_deny(cam, "talk"):
            return

        # POST /api/talk/start  {"cam": "c2"} -> 打开摄像头喇叭通道
        if len(parts) == 3 and parts[2] == "start" and method == "POST":
            body = self._body()
            cam = str(body.get("cam") or "")
            if not cam:
                return self._err("缺少 cam", 400)
            try:
                res = P.talk_start(cam)
            except P.PtzError as e:
                return self._err("打开对讲失败：%s" % e, 502, need_session=e.need_session)
            except Exception as e:
                return self._err("打开对讲失败：%s" % e, 502)
            return self._j({"ok": True, "result": res})

        # POST /api/talk/push?cam=c2   body = 16bit/8kHz 单声道 PCM
        if len(parts) == 3 and parts[2] == "push" and method == "POST":
            cam = (q.get("cam") or [""])[0]
            if not cam:
                return self._err("缺少 cam", 400)
            pcm = self._raw_body(512 * 1024)
            if not pcm:
                return self._j({"ok": True, "frames": 0})
            try:
                res = P.talk_push(cam, pcm)
            except P.PtzError as e:
                return self._err("推音失败：%s" % e, 502, need_session=e.need_session)
            except Exception as e:
                return self._err("推音失败：%s" % e, 502)
            return self._j({"ok": True, "result": res})

        # POST /api/talk/stop  {"cam": "c2"}
        if len(parts) == 3 and parts[2] == "stop" and method == "POST":
            body = self._body()
            cam = str(body.get("cam") or "")
            if not cam:
                return self._err("缺少 cam", 400)
            try:
                res = P.talk_stop(cam)
            except Exception as e:
                return self._err("关闭对讲失败：%s" % e, 502)
            return self._j({"ok": True, "result": res})

        # POST /api/talk/beep  {"cam": "c2"} -> 播一段提示音，用来确认喇叭会响
        if len(parts) == 3 and parts[2] == "beep" and method == "POST":
            body = self._body()
            cam = str(body.get("cam") or "")
            if not cam:
                return self._err("缺少 cam", 400)
            try:
                res = P.beep(cam)
            except Exception as e:
                return self._err("播放提示音失败：%s" % e, 502)
            return self._j({"ok": True, "result": res})

        return self._err("没有这个对讲接口：%s" % "/".join(parts), 404)

    def _xiaomi_api(self, method, parts, q):
        import nw_xiaomi as X
        auth = X.get_auth()
        # 扫码授权、重置授权、列设备都在动米家账号，只给管理员；
        # status/available 只是"这台机器认没授权"，只读账号的状态页要看
        if method == "POST" or (len(parts) == 3 and parts[2] == "devices"):
            if self._require_admin() is None:
                return
        # /api/xiaomi/status -> 授权状态
        if len(parts) == 3 and parts[2] == "status":
            return self._j({"ok": True, **auth.snapshot()})
        # /api/xiaomi/available -> SDK 是否可用
        if len(parts) == 3 and parts[2] == "available":
            ok, msg = X.xiaomi_available()
            return self._j({"ok": True, "available": ok, "error": msg})
        # /api/xiaomi/qr (POST) -> 拿二维码 URL
        if len(parts) == 3 and parts[2] == "qr" and method == "POST":
            ok, qr, err = auth.get_qr()
            return self._j({"ok": ok, "qr_url": qr, "error": err})
        # /api/xiaomi/qr_image -> 把 qr_url 转成 PNG 图片（前端 <img> 直接显示）
        if len(parts) == 3 and parts[2] == "qr_image" and method == "GET":
            qr_url = auth.qr_url
            if not qr_url:
                return self._err("还没有二维码，先调用 /api/xiaomi/qr", 400)
            try:
                import qrcode
                import io
                img = qrcode.make(qr_url, box_size=8, border=2)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                return self._send(200, buf.getvalue(), "image/png")
            except Exception as e:
                return self._err("生成二维码失败：%s" % e, 500)
        # /api/xiaomi/reset (POST) -> 重置授权
        if len(parts) == 3 and parts[2] == "reset" and method == "POST":
            auth.reset()
            return self._j({"ok": True})
        # /api/xiaomi/devices -> 设备列表
        if len(parts) == 3 and parts[2] == "devices" and method == "GET":
            if auth.state != "authorized":
                return self._err("尚未授权，请先扫码登录", 401)
            try:
                client = auth.ensure_client()
                client._access_token = auth.token
                devs = client.home.get_device_list()
                cams = []
                for d in devs or []:
                    if "camera" in (d.get("model") or "").lower():
                        cams.append({
                            "did": d.get("did"),
                            "name": d.get("name"),
                            "model": d.get("model"),
                            "online": d.get("isOnline", False),
                        })
                return self._j({"ok": True, "devices": cams})
            except Exception as e:
                return self._err("获取设备列表失败：%s" % e, 500)
        return self._err("没有这个小米接口：%s" % "/".join(parts), 404)

    def _motion_api(self, method, parts, q):
        """设备侧配置面：读的是摄像头自己生效的配置，写也是写进摄像头。

        只走米家云（同步短请求），不碰取流那条单例 loop —— 见 nw_motion 的说明。
        """
        import nw_motion as M
        fresh = (q.get("fresh") or ["0"])[0] in ("1", "true", "yes")
        if method == "GET":
            want = (q.get("cam") or [""])[0]
            sess = self._user() or {}
            cams = A.visible_cams(sess, C.load().get("cameras") or [])
            cams = [c for c in cams if c.get("enabled", True)]
            if want:
                cams = [c for c in cams if str(c.get("id")) == want]
                if not cams:
                    return self._err("没有这路摄像头：%s" % want, 404)
            try:
                return self._j({"ok": True, "cams": M.list_state(cams, fresh=fresh)})
            except Exception as e:
                return self._err("读取设备配置失败：%s" % e, 502)
        if method in ("PUT", "POST"):
            body = self._body()
            cid = str(body.get("cam") or "")
            cam = C.get_camera(cid)
            if not cam:
                return self._err("没有这路摄像头：%s" % cid, 404)
            changes = body.get("changes")
            if not isinstance(changes, dict):
                return self._err("changes 要是一个对象", 400)
            try:
                res = M.apply_changes(cam, changes)
            except Exception as e:
                return self._err("下发失败：%s" % e, 502)
            # 部分格子改成果、部分被拒是常事（离线/只读/越界）。
            # 这里不返回 ok=false：那样会把"已经生效的那几条 + 回读结果"一起丢掉，
            # 页面只能显示一句错误。失败明细在 result.errors 里，由页面逐条说清。
            res["applied_all"] = bool(res.get("applied")) and not res.get("errors")
            return self._j({"ok": True, "result": res})
        return self._err("没有这个侦测配置接口：%s" % "/".join(parts), 404)

    def _snapshot(self, cam):
        """独立抽一帧 JPEG（不依赖直播广播，避免和 MPEG-TS 订阅纠缠）。"""
        url = cam.get("url") or ""
        transport = (cam.get("transport") or "tcp").lower()
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        if url.lower().startswith("rtsp"):
            cmd += ["-rtsp_transport", transport,
                    "-fflags", "nobuffer", "-flags", "low_delay"]
        cmd += ["-i", url, "-frames:v", "1", "-q:v", "4", "-f", "image2", "pipe:1"]
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        except subprocess.TimeoutExpired:
            return self._err("截图超时（摄像头无响应）", 503)
        except FileNotFoundError:
            return self._err("系统里找不到 ffmpeg", 500)
        except Exception as e:
            return self._err("截图失败：%s" % e, 500)
        if p.returncode != 0 or not p.stdout:
            msg = (p.stderr or b"").decode("utf-8", "replace").strip()
            return self._err("暂时没有画面（" + (msg[-200:] or "未出流") + "）", 503)
        return self._send(200, p.stdout, "image/jpeg")

    def _camera_create(self):
        body = self._body()
        ctype = (body.get("type") or "rtsp").lower()
        if ctype == "xiaomi":
            did = (body.get("did") or "").strip()
            if not did:
                return self._err("请填写小米摄像头的 DID")
            cfg = C.load()
            # 🔴 去重：同一台小米摄像头（按 DID 判定，与 type 无关——迁移到
            # go2rtc 后旧摄像头 type 会变成 rtsp，但 did 还在）只允许添加一次。
            for c in cfg.get("cameras") or []:
                if str(c.get("did") or "").strip() == did:
                    return self._err("该摄像头（DID: %s）已添加，请勿重复添加" % did)

            cam = {
                "id": C.new_camera_id(cfg),
                "type": "xiaomi",
                "name": (body.get("name") or "未命名").strip()[:40],
                "url": did,              # 小米摄像头存 did 到 url（兼容）
                "did": did,
                "channel": int(body.get("channel") or 0),
                "quality": (body.get("quality") or "HIGH").upper(),
                "transport": "tcp",
                "enabled": bool(body.get("enabled", True)),
                "record": bool(body.get("record", True)),
                "note": (body.get("note") or "")[:200],
            }
            # 小米这一路也支持遮蔽：遮蔽烧进 P2P 解码后重编码的画面，
            # 与 RTSP 走的是同一套 ffmpeg drawbox 滤镜
            xm_masks = PV.sanitize((body.get("privacy") or {}).get("masks"))
            if xm_masks:
                cam["privacy"] = {"masks": xm_masks}
            cfg["cameras"].append(cam)
            C.save(cfg)
            _rec.reconcile()
            self._audit("cam_create", cam=cam["id"], name=cam["name"], type="xiaomi")
            return self._j({"ok": True, "camera": _pub_cam(cam)})

        url = (body.get("url") or "").strip()
        if not url:
            return self._err("请填写流地址（rtsp:// 或 file:// 或 http://）")
        cfg = C.load()
        # 🔴 去重：同一路流地址（忽略大小写、尾部斜杠、空白）只允许添加一次。
        def _norm(u):
            u = (u or "").strip().lower()
            u = u.rstrip("/")
            return u
        nu = _norm(url)
        for c in cfg.get("cameras") or []:
            if _norm(c.get("url")) == nu:
                return self._err("该流地址已添加（%s），请勿重复添加" % (c.get("name") or c.get("id")))

        cam = {
            "id": C.new_camera_id(cfg),
            # onvif 扫描出来的最终也是一条 RTSP 地址，这里保留原始类型
            # 只是为了设置页能显示「这台是 ONVIF 接入的」
            "type": ctype if ctype in ("rtsp", "onvif", "url") else "rtsp",
            "name": (body.get("name") or "未命名").strip()[:40],
            "url": url,
            "transport": (body.get("transport") or "tcp").lower(),
            "enabled": bool(body.get("enabled", True)),
            "record": bool(body.get("record", True)),
            "note": (body.get("note") or "")[:200],
        }
        low = (body.get("url_low") or "").strip()
        if low:
            cam["url_low"] = low[:400]
        masks = PV.sanitize((body.get("privacy") or {}).get("masks"))
        if masks:
            cam["privacy"] = {"masks": masks}
        if ctype == "onvif":
            cam["onvif_host"] = (body.get("onvif_host") or "").strip()
            cam["onvif_port"] = int(body.get("onvif_port") or 80)
        cfg["cameras"].append(cam)
        C.save(cfg)
        _rec.reconcile()
        self._audit("cam_create", cam=cam["id"], name=cam["name"], type=cam["type"])
        return self._j({"ok": True, "camera": _pub_cam(cam)})

    def _camera_update(self, cam):
        body = self._body()
        cfg = C.load()
        masks = None
        if "privacy" in body:
            masks = PV.sanitize((body.get("privacy") or {}).get("masks"))
        old = C.get_camera(cam.get("id")) or {}
        for k in ("url", "url_low"):
            # 表单回显的是掩码地址，用户没重填口令时把原口令贴回去：
            # 不然"改个名字"就会把 rtsp://user:***@host 真写进配置。
            if k in body and isinstance(body[k], str) and body[k].strip():
                body[k] = _merge_url_secret(body[k].strip(), old.get(k) or "")
        for c in cfg["cameras"]:
            if str(c.get("id")) != str(cam.get("id")):
                continue
            for k in ("name", "url", "url_low", "transport", "note"):
                if k in body and body[k] is not None:
                    c[k] = str(body[k]).strip()[:400]
            for k in ("enabled", "record"):
                if k in body:
                    c[k] = bool(body[k])
            # 隐私遮蔽：sanitize 过的归一化区域才落盘。改了就重建这一路的直播与
            # 录像 —— 遮蔽是烧进像素的，不重起 ffmpeg 就还是旧画面。
            if masks is not None:
                c["privacy"] = {"masks": masks}
            if "quality" in body and c.get("type") == "xiaomi":
                c["quality"] = str(body.get("quality") or "HIGH").upper()
        C.save(cfg)
        _live.drop(cam.get("id"))
        try:
            import nw_xiaomi as X
            X.get_hub().drop(str(cam.get("id")))
        except Exception:
            pass
        try:
            _rec.reconcile()
        except Exception:
            pass
        if masks is not None:
            self._audit("privacy_update", cam=str(cam.get("id")), masks=len(masks))
        return self._j({"ok": True, "camera": _pub_cam(C.get_camera(cam.get("id")))})


def _share_base(handler, token):
    """分享页里 <video> 的绝对路径前缀（网关模式要带 /app/homeeye）。"""
    pre = ""
    raw = str(getattr(handler, "path", "") or "")
    for cand in ("/app/" + C.APPNAME, "/" + C.APPNAME):
        if raw.startswith(cand + "/share/"):
            pre = cand
            break
    return "%s/share/%s" % (pre, urllib.parse.quote(str(token), safe=""))


def _share_row(rec, handler):
    """分享记录 + 一条能直接点开的完整 URL。

    主机名从 Host 头取：容器里监听 0.0.0.0，本机根本不知道别人是从哪个
    地址访问进来的，写死 127.0.0.1 发出去的链接在别人手里全是打不开的。
    """
    row = dict(rec or {})
    host = str(handler.headers.get("Host") or "").strip()
    if not host:
        try:
            cfg = C.load()
            host = (cfg.get("notify") or {}).get("link_base") or ""
            host = re.sub(r"^https?://", "", str(host)).rstrip("/")
        except Exception:
            host = ""
    scheme = _scheme(handler)
    token = str(row.get("token") or "")
    row["url"] = ("%s://%s/share/%s" % (scheme, host, token)) if host else ""
    return row


def _scheme(handler):
    """当前请求走的是 http 还是 https（分享链接必须是绝对地址）。"""
    fwd = str(handler.headers.get("X-Forwarded-Proto") or "").strip().lower()
    if fwd in ("http", "https"):
        return fwd
    try:
        import ssl as _ssl
        if isinstance(getattr(handler, "connection", None), _ssl.SSLSocket):
            return "https"
    except Exception:
        pass
    return "http"


def _share_error_page(msg):
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>HomeEye · 链接不可用</title>'
            '<body style="margin:0;background:#0b0f14;color:#dfe7ef;'
            'font:15px/1.7 system-ui;display:flex;align-items:center;'
            'justify-content:center;min-height:100vh">'
            '<div style="text-align:center"><b>这个链接已经不能用了</b><br>'
            '<span style="color:#93a6bd">%s</span></div></body></html>' % msg)


def _mask_url(url):
    """日志/接口里不回显密码，只留形态，方便用户确认填对了。"""
    if not url:
        return ""
    u = re.sub(r"(//[^:/@]+:)[^@]+@", r"\1***@", url)
    return u


def _has_secret(url):
    """地址里是否带口令段（scheme://user:pass@host）。"""
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/?#@]*:[^/?#@]*@", url or ""))


def _pub_cam(cam):
    """给前端的摄像头：地址一律掩码，口令只在 reveal 接口里按需取。"""
    d = _redact_secrets(dict(cam or {}))
    if d.get("url"):
        d["url"] = _mask_url(d["url"])
        d["url_masked"] = d["url"]
    if d.get("url_low"):
        d["url_low"] = _mask_url(d["url_low"])
        d["url_low_masked"] = d["url_low"]
    d.pop("url_password", None)
    return d


def _mask_text(text):
    """自由文本（ffmpeg/ffprobe 的报错原话）里的口令也一并抹掉。"""
    s = str(text or "")
    if "://" not in s:
        return s
    return re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^:/@\s]+:)[^@\s]+@", r"\1***@",
                  _mask_url(s))


def _redact_secrets(obj, depth=0):
    """把状态字典里的流地址口令抹掉（就地返回新结构）。

    取流/录像的 error 常常是 ffmpeg 原话，里面就带着 rtsp://user:pass@host ——
    前端一显示，口令就进了浏览器。地址类字段与自由文本都过一遍。
    """
    if depth > 4:
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str):
                lk = str(k).lower()
                if ("url" in lk or "addr" in lk or "source" in lk or lk in
                        ("error", "reason", "detail", "note", "message", "last")):
                    v = _mask_text(v)

            elif isinstance(v, (dict, list)):
                v = _redact_secrets(v, depth + 1)
            out[k] = v
        return out
    if isinstance(obj, list):
        return [_redact_secrets(x, depth + 1) for x in obj]
    return obj


def _merge_url_secret(new, old):
    """表单里回显的是掩码地址，用户没重填口令时要把原口令贴回去。

    否则"改个名字"都会把 rtsp://viewer:***@192.168.1.9 真写进配置 —— 摄像头
    当场取不到流，而用户根本没动过地址。只有新值仍是掩码形态才合并。
    """
    if not new or not old or "***@" not in new:
        return new
    if not _has_secret(old):
        return new
    mn = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)[^/?#@]*@", new)
    mo = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*://)[^/?#@]*@", old)
    if not mn or not mo:
        return new
    return mo.group(0) + new[len(mn.group(0)):]


def _public_settings(cfg, admin=True):
    """配置回显。`admin=False` 时只给播放必需的那几项。

    设置里全是地址、账号与远端路径（SMTP 用户名、rclone remote、VLM 网关），
    只读的家庭账号不需要这些才能看画面 —— 少给就是少泄。
    """
    st = dict(cfg.get("storage") or {})
    st["effective_dir"] = C.recordings_root(cfg)
    live = dict(cfg.get("live") or {})
    if not admin:
        return {
            "storage": {},
            "storage_status": {},
            "meta": {},
            "live": {k: live.get(k) for k in ("autoplay", "default_quality", "audio")},
            "motion": {},
            "notify": {},
            "ptz": {},
            "watchdog": {},
            "ai": {},
            "archive": {},
            "integrity": {},
            "users": [],
            "auth": {"enabled": bool((cfg.get("auth") or {}).get("enabled", True)),
                     "username": "", "configured": True, "session_hours": 24},
        }
    nt = dict(cfg.get("notify") or {})
    # 密码不回显：只告诉前端「已设置」，避免明文出现在 /api/state 里
    if nt.get("smtp_pass"):
        nt["smtp_pass_set"] = True
        nt["smtp_pass"] = ""
    if nt.get("ding_secret"):
        nt["ding_secret_set"] = True
        nt["ding_secret"] = ""
    pz = dict(cfg.get("ptz") or {})
    # passToken 等价于账号密码，绝不回显
    if pz.get("token"):
        pz["token_set"] = True
        pz["token"] = ""
    if pz.get("g2r_pass"):
        pz["g2r_pass_set"] = True
        pz["g2r_pass"] = ""
    au = dict(cfg.get("auth") or {})
    ai = dict(cfg.get("ai") or {})
    # VLM 的 key 等价于第三方账号的口令，只回「已设置」
    if ai.get("vlm_key"):
        ai["vlm_key_set"] = True
        ai["vlm_key"] = ""
    # 盐与派生值都是敏感物（拿到它能离线跑字典），一个都不回显
    try:
        storage_status = H.storage_state()
    except Exception as e:
        storage_status = {"dir": st.get("effective_dir", ""), "ok": True,
                          "error": "", "probe_error": str(e)[:120]}
    return {
        "storage": st,
        "storage_status": storage_status,
        "meta": C.settings_meta(),
        "live": dict(cfg.get("live") or {}),
        "motion": dict(cfg.get("motion") or {}),
        "notify": nt,
        "ptz": pz,
        "watchdog": dict(cfg.get("watchdog") or {}),
        "ai": ai,
        "archive": dict(cfg.get("archive") or {}),
        "integrity": dict(cfg.get("integrity") or {}),
        "users": A.users(),
        "auth": {"enabled": bool(au.get("enabled", True)),
                 "username": au.get("username") or "admin",
                 "configured": bool(au.get("pw_hash") and au.get("salt")),
                 "session_hours": au.get("session_hours", 24)},
    }


# ---------------------------------------------------------------- 入口
# 飞牛统一网关 Unix Socket HTTP 服务（仅 Linux 有 UnixStreamServer；Windows 上跳过定义）
if hasattr(socketserver, "UnixStreamServer"):
    class GatewayUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        """复用同一个 Handler：飞牛把 /app/homeeye/... 转发到这个 socket，
        Handler 里已有的 _strip_prefix 会把 /app/homeeye 前缀抹平后再路由。"""
        daemon_threads = True

        def get_request(self):
            request, _ = super().get_request()
            # BaseHTTPRequestHandler 需要 (client_address) 二元组
            return request, ("unix", 0)
else:
    GatewayUnixHTTPServer = None


def _start_side_loops():
    """辅助循环统一在这里拉起（都自带"只起一次"）。

    完整性自检与归档定时都是"没人点也要跑"的运维动作：空洞越早发现越有用，
    冷层搬运放在低峰自己跑，不需要人每天去点一次按钮。
    """
    try:
        IG.start()
    except Exception:
        pass
    try:
        AR.start_auto()
    except Exception:
        pass


def build_server(port, host="127.0.0.1"):
    global _live, _rec
    C.ensure_dirs()
    _live = L.LiveHub(C.load)
    _rec = R.Recorder(C.load, C.recordings_root)
    _rec.start()
    EV.start()
    try:
        import nw_notify as N
        N.start_digest()
    except Exception:
        pass
    H.start()
    W.start()
    _start_side_loops()
    httpd = ThreadingHTTPServer((host, int(port)), Handler)
    httpd.daemon_threads = True
    return httpd


def _ensure_core():
    """直播/录像核心只初始化一次；HTTPS 监听是额外挂上去的，不能重复初始化。"""
    global _live, _rec
    C.ensure_dirs()
    if _live is None:
        _live = L.LiveHub(C.load)
    if _rec is None:
        _rec = R.Recorder(C.load, C.recordings_root)
        _rec.start()
    EV.start()
    try:
        import nw_notify as N
        N.start_digest()
    except Exception:
        pass
    H.start()
    W.start()
    _start_side_loops()


def ensure_selfsigned(certdir):
    """生成/复用一个自签证书，返回 (cert, key)；失败返回 (None, None)。

    对讲必须调浏览器麦克风，而浏览器只在**安全上下文**（HTTPS / localhost）下
    才允许 getUserMedia。局域网 http://ip:port 是不安全上下文，麦克风会被直接
    拒绝，所以额外开一个 HTTPS 端口是最省事的解法：
    浏览器首次会提示证书不受信任，点「继续访问」之后就是安全上下文了。
    """
    import socket

    os.makedirs(certdir, exist_ok=True)
    cert = os.path.join(certdir, "cert.pem")
    key = os.path.join(certdir, "key.pem")
    if os.path.exists(cert) and os.path.exists(key):
        return cert, key

    ips = ["127.0.0.1"]
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("223.5.5.5", 53))
            ip = s.getsockname()[0]
            if ip and ip not in ips:
                ips.append(ip)
        finally:
            s.close()
    except OSError:
        pass

    san = ",".join(["DNS:homeeye", "DNS:localhost"] + ["IP:%s" % i for i in ips])
    cmd = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
           "-keyout", key, "-out", cert, "-days", "3650",
           "-subj", "/CN=homeeye", "-addext", "subjectAltName=" + san]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)
    except Exception:
        return None, None
    return cert, key


def build_tls_server(port, host, cert, key):
    """额外的 HTTPS 监听（给对讲用麦克风）。"""
    import ssl

    _ensure_core()
    httpd = ThreadingHTTPServer((host, int(port)), Handler)
    httpd.daemon_threads = True
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    return httpd


def build_gateway_server(sock_path):
    """监听 unix socket，供飞牛统一网关接入（type=iframe + gatewaySocket）。"""
    if GatewayUnixHTTPServer is None:
        raise RuntimeError("unix socket 仅在 Linux 上可用")
    global _live, _rec
    C.ensure_dirs()
    _live = L.LiveHub(C.load)
    _rec = R.Recorder(C.load, C.recordings_root)
    _rec.start()
    EV.start()
    try:
        import nw_notify as N
        N.start_digest()
    except Exception:
        pass
    H.start()
    W.start()
    parent = os.path.dirname(sock_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if os.path.exists(sock_path):
        try:
            os.remove(sock_path)
        except OSError:
            pass
    old_umask = os.umask(0o007)  # socket 权限 0770，与飞牛应用惯例一致
    try:
        httpd = GatewayUnixHTTPServer(sock_path, Handler)
    finally:
        os.umask(old_umask)
    try:
        os.chmod(sock_path, 0o770)
    except OSError:
        pass
    httpd.daemon_threads = True
    return httpd


def run(port=None, host="127.0.0.1"):
    port = int(port or os.environ.get("HE_PORT") or 17071)
    httpd = build_server(port, host)
    return httpd


def shutdown_all():
    """停止所有取流/录像子进程。退出时必须调，否则 ffmpeg 会变孤儿一直拉流。"""
    global _live, _rec
    try:
        if _rec is not None:
            _rec.stop()
    except Exception:
        pass
    try:
        if _live is not None:
            _live.drop_all()
    except Exception:
        pass
    # 云台侧车同样要收掉，否则重启后旧进程一直占着 P2P 会话
    try:
        import nw_ptz as _P
        _P.shutdown()
    except Exception:
        pass


def start_sidecars():
    """启动常驻侧车（云台）。失败不影响主服务。"""
    try:
        import nw_ptz as _P
        ok, why = _P.available()
        if ok:
            if _P.backend() == "go2rtc":
                # go2rtc 后端复用它自己的取流会话，不需要额外的常驻进程
                sys.stderr.write("[homeeye] ptz via go2rtc (%s)\n" % _P.g2r_api())
                # 按需建立的会话闲置后要还回去（摄像头 P2P 上限只有 3 条）
                _start_ptz_reaper()
            else:
                _P.ensure_running()
                sys.stderr.write("[homeeye] ptz sidecar started\n")
        else:
            sys.stderr.write("[homeeye] ptz disabled: %s\n" % why)
    except Exception as e:
        sys.stderr.write("[homeeye] ptz sidecar failed: %s\n" % e)


_reaper_started = False


def _start_ptz_reaper():
    """拉起云台会话的闲置回收线程（只起一次）。"""
    global _reaper_started
    if _reaper_started:
        return
    _reaper_started = True
    try:
        import nw_ptz as _P
        threading.Thread(target=_P._idle_reaper, daemon=True).start()
    except Exception as e:
        sys.stderr.write("[homeeye] ptz reaper failed: %s\n" % e)
