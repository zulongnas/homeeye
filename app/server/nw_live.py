# -*- coding: utf-8 -*-
"""HomeEye · 实时取流（MPEG-TS 广播，对齐 minvr 的稳定方案）

为什么是 MPEG-TS over HTTP + MSE，而不是 MJPEG / HLS / WebRTC：
  * MPEG-TS 走纯 HTTP、浏览器原生 MediaSource 播放，延迟 1~2 秒 —— 这正是
    minvr 被用户评价「很稳、低延迟」的关键选型；
  * 一台摄像机只拉**一条** RTSP，用 Python 做扇出（fan-out）把同一份 TS 字节
    广播给所有观看端 —— 避免「4 个窗口 = 4 条 RTSP」把摄像机连接数打满；
  * 相比 MJPEG：带宽更低（H.264 压缩）、画面更顺（含 B/P 帧）。

每个摄像头一个 :class:`LiveCam`：ffmpeg 把 RTSP 转封装成 MPEG-TS 写到管道，
读线程把 TS 包切片后广播给所有订阅者（只保留最近一小段环形缓冲，供新订阅者
能立刻拿到 SPS/PPS + I 帧，避免「黑屏直到下一个关键帧」）。最后一个观看者
离开后延迟一段时间自动停流。

TS 包固定 188 字节；SDT/PAT/PMT 由 ffmpeg 周期性重发，新订阅者从缓冲拿到
最近的 PAT/PMT 即可让 mpegts.js 秒开。
"""
import collections
import os
import subprocess
import threading
import time

TS_PACKET = 188
# 每个订阅者保留的最近字节（约 4 秒 @ 1Mbps），足够覆盖 SPS/PPS + I 帧
_RING = 512 * 1024


def _ffmpeg_bin():
    return os.environ.get("HE_FFMPEG") or "ffmpeg"


# ---------------------------------------------------------------- 转码档位
TMODES = ("auto", "always", "never")

# url -> (探测时刻, 编码名或 None, 说明)。只缓存"源本身是什么编码"，
# 与观看端数量无关，所以一条 URL 一份就够。
_PROBE = {}
_PROBE_OK_TTL = 600.0
_PROBE_BAD_TTL = 20.0
_probe_lock = threading.Lock()


def _tmode(raw):
    """live.transcode 读成三档。老配置里它是布尔：True=always、False=never。

    落盘的配置不一定经过 normalize（手改过、或升级前写的），所以读这一侧也要
    能认布尔 —— 两处口径必须一致，否则页面显示的和实际跑的就对不上。
    """
    if isinstance(raw, bool):
        return "always" if raw else "never"
    txt = str(raw if raw is not None else "").strip().lower()
    if txt in ("1", "on", "yes", "true"):
        return "always"
    if txt in ("0", "off", "no", "false"):
        return "never"
    return txt if txt in TMODES else "auto"


def _ffprobe_codec(url, transport="tcp", timeout=6):
    """问一次源的视频编码。返回 (codec|None, 一句说明)。

    为什么值得多花这一次探测：源已经是 H.264 时（go2rtc 的 *_play 转码流就是），
    浏览器 MSE 能直接解，HomeEye 再 libx264 软转一遍就是纯粹浪费 —— 一路 1080p
    吃掉一整个核，还把帧率砍到设置里那个 10。探测失败就照旧转码，宁可多花 CPU
    也不要黑屏。
    """
    cmd = ["ffprobe", "-hide_banner", "-loglevel", "error"]
    if (url or "").lower().startswith("rtsp"):
        cmd += ["-rtsp_transport", (transport or "tcp").lower()]
    cmd += ["-select_streams", "v:0", "-show_entries", "stream=codec_name",
            "-of", "csv=p=0", "-rw_timeout", "5000000", url]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout)
        name = (p.stdout or b"").decode("utf-8", "replace").strip().splitlines()
        if p.returncode == 0 and name and name[0]:
            return name[0].strip().lower(), ""
        return None, ((p.stderr or b"").decode("utf-8", "replace").strip()[-200:]
                      or "ffprobe 没报出编码")
    except subprocess.TimeoutExpired:
        return None, "探测超时（源没响应）"
    except FileNotFoundError:
        return None, "系统里找不到 ffprobe"
    except Exception as e:
        return None, str(e)[-200:]


def probe_codec(url, transport="tcp"):
    """带缓存的编码探测：成功记很久，失败只记 20 秒（源可能刚起来）。"""
    if not url:
        return None, "没有源地址"
    now = time.time()
    with _probe_lock:
        hit = _PROBE.get(url)
        if hit and now - hit[0] < (_PROBE_OK_TTL if hit[1] else _PROBE_BAD_TTL):
            return hit[1], hit[2]
    codec, note = _ffprobe_codec(url, transport)
    with _probe_lock:
        _PROBE[url] = (time.time(), codec, note)
    return codec, note


def forget_probe(url=None):
    with _probe_lock:
        if url:
            _PROBE.pop(url, None)
        else:
            _PROBE.clear()


def transcode_needed(mode, codec):
    """这一路到底要不要转码。返回 (要转吗, 给人看的一句原因)。"""
    if mode == "always":
        return True, "设置里选了「总是转码」"
    if mode == "never":
        return False, "设置里选了「不转码，原码流直拷」"
    if codec == "h264":
        return False, "源已是 H.264，浏览器能直接解 —— 不再二次转码"
    if codec == "hevc":
        return True, "源是 H.265（HEVC），浏览器解不了，必须转"
    if codec:
        return True, "源是 %s，浏览器解不了，转成 H.264" % codec
    return True, "没探出源的编码，先按转码处理（宁可多花 CPU，不要黑屏）"


class Subscriber(object):
    """一个观看端：自己的字节队列（有界），避免慢客户端拖垮快客户端。"""

    __slots__ = ("q", "cv", "closed", "t0")

    def __init__(self):
        self.q = collections.deque()
        self.cv = threading.Condition()
        self.closed = False
        self.t0 = time.time()

    def push(self, data):
        with self.cv:
            if self.closed:
                return
            self.q.append(data)
            # 有界：慢客户端积压超过上限就丢最旧的，保证实时性
            total = sum(len(x) for x in self.q)
            while self.q and total > _RING * 2:
                total -= len(self.q.popleft())
            self.cv.notify()

    def take(self, timeout=5.0):
        with self.cv:
            if not self.q and not self.closed:
                self.cv.wait(timeout)
            if self.q:
                return self.q.popleft()
            return b""

    def close(self):
        with self.cv:
            self.closed = True
            self.q.clear()
            self.cv.notify()


class LiveCam(object):
    def __init__(self, cam, opts):
        self.cam_id = str(cam.get("id"))
        self.name = cam.get("name") or self.cam_id
        self.url = cam.get("url") or ""
        self.transport = (cam.get("transport") or "tcp").lower()
        self.width = int(opts.get("width") or 1280)
        self.fps = int(opts.get("fps") or 10)
        self.quality = int(opts.get("quality") or 23)
        self.tmode = _tmode(opts.get("transcode"))
        # 本次实际怎么走的（copy / transcode）+ 为什么。给 /api/state 和页面看，
        # "消了双转码"这件事要能当场查出来，不能只写在提交信息里。
        self.effective = ""
        self.mode_note = ""
        # quality 在实时画面里就是 x264 CRF：1~31，越小越清晰
        self.crf = min(31, max(1, self.quality))
        self.idle_stop = int(opts.get("idle_stop") or 12)
        # 直播是否带音频（设置页 live.audio）。关掉就完全不处理音轨，省 CPU。
        self.audio = bool(opts.get("audio", True))

        self._lock = threading.RLock()
        self._subs = []
        self._proc = None
        self._reader = None
        self._latest = None
        self._ring = collections.deque()   # 最近 _RING 字节的环形缓冲（秒开用）
        self._ring_len = 0
        self._last_active = time.time()
        self._stopping = False
        self.error = ""
        self.started_at = 0.0
        self.frames = 0

    # ---------- 进程管理 ----------
    def cmdline(self):
        # 输出 MPEG-TS 给浏览器 MSE 播放。要不要转 H.264 由 live.transcode 决定：
        #   always  总是转（老行为，兼容一切源）
        #   never   永不转（原码流直拷，源不是 H.264 时浏览器会黑屏）
        #   auto    问一次源是什么编码：H.264 直拷，其它才转
        # go2rtc 的 *_play 那一路本来就是 H.264 转码流，再软转一遍就是双重转码
        # —— auto 就是为把它省掉而加的。
        is_rtsp = (self.url or "").lower().startswith("rtsp")
        is_file = (self.url or "").lower().startswith("file")
        cmd = [
            _ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error",
        ]
        if is_rtsp:
            cmd += ["-rtsp_transport", self.transport]
            cmd += ["-fflags", "nobuffer", "-flags", "low_delay"]
        if is_file:
            # 本地文件源：循环播放、按实时速率（模拟直播），避免 60 秒播完就 EOF
            cmd += ["-stream_loop", "-1", "-re"]
        # 音轨用可选映射（0:a?）：很多 RTSP 摄像机根本没有音轨，
        # 写死 -map 0:a 会让 ffmpeg 直接报 "Stream map ... matches no streams" 退出，
        # 结果连画面都没了。有音轨时统一转成 AAC —— MSE/mpegts.js 只认 AAC。
        cmd += ["-i", self.url, "-map", "0:v:0", "-map", "0:a:0?"]
        if self.audio:
            cmd += ["-c:a", "aac", "-b:a", "64k", "-ar", "44100", "-ac", "1"]
        else:
            cmd += ["-an"]
        codec = None
        if self.tmode == "auto":
            codec, _note = probe_codec(self.url, self.transport)
        need, note = transcode_needed(self.tmode, codec)
        self.mode_note = note
        self.effective = "transcode" if need else "copy"
        if need:
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                    "-profile:v", "main", "-pix_fmt", "yuv420p", "-crf", str(self.crf),
                    # 关键帧重复 SPS/PPS：TS 里参数集只在开头出现一次的话，
                    # 中途加入的观看端会因为拿不到 PPS 而永久黑屏。
                    "-x264-params", "repeat-headers=1",
                    "-bsf:v", "dump_extra"]
            if self.width > 0:
                cmd += ["-vf", "scale='min(iw,%d)':-2" % self.width]
            if self.fps > 0:
                cmd += ["-r", str(self.fps)]
            cmd += ["-g", str(max(self.fps or 10, 1) * 2)]
        else:
            cmd += ["-c:v", "copy"]
        cmd += ["-f", "mpegts", "-mpegts_flags", "+resend_headers",
                "-mpegts_copyts", "1", "pipe:1"]
        return cmd

    def _spawn(self):
        try:
            self._proc = subprocess.Popen(
                self.cmdline(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0, close_fds=True,
            )
        except Exception as e:
            self._proc = None
            self.error = "启动 ffmpeg 失败：%s" % e
            return
        self.started_at = time.time()
        self.frames = 0
        self.error = ""
        self._reader = threading.Thread(target=self._read_loop, args=(self._proc,), daemon=True)
        self._reader.start()
        threading.Thread(target=self._err_loop, args=(self._proc,), daemon=True).start()

    def _err_loop(self, proc):
        try:
            for line in iter(proc.stderr.readline, b""):
                s = line.decode("utf-8", "replace").strip()
                if s:
                    self.error = s[-300:]
        except Exception:
            pass

    def _read_loop(self, proc):
        buf = b""
        out = proc.stdout
        try:
            while True:
                chunk = out.read(32768)
                if not chunk:
                    break
                buf += chunk
                # 按 188 字节 TS 包切块，成批广播（每批约 32KB，减少锁竞争）
                while len(buf) >= TS_PACKET:
                    # 找同步字节 0x47（TS 包都以 0x47 开头）
                    if buf[0] != 0x47:
                        i = buf.find(b"\x47")
                        if i < 0:
                            buf = b""
                            break
                        if i > 0:
                            buf = buf[i:]
                            continue
                    # 取一批 TS 包（对齐到 188 的整数倍）
                    n = (len(buf) // TS_PACKET) * TS_PACKET
                    if n < 32 * 1024:
                        # 数据还不够一批，继续读；但至少送 1 个包避免停顿
                        if len(buf) < 32 * 1024:
                            break
                    batch = buf[:n]
                    buf = buf[n:]
                    if not batch:
                        break
                    self.frames += 1
                    self._latest = time.time()
                    self._feed(batch)
        except Exception as e:
            self.error = "read: %s" % e
        finally:
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
            with self._lock:
                if self._proc is proc:
                    self._proc = None

    def _feed(self, batch):
        """把一批 TS 字节写进环形缓冲并广播给所有订阅者。"""
        with self._lock:
            self._ring.append(batch)
            self._ring_len += len(batch)
            while self._ring_len > _RING:
                self._ring_len -= len(self._ring.popleft())
            subs = list(self._subs)
        for s in subs:
            s.push(batch)

    def _recent(self):
        """返回最近一段 TS 缓冲（新订阅者秒开用），并重置其读取进度。"""
        with self._lock:
            data = b"".join(self._ring)
        return data

    def _stop_proc(self):
        with self._lock:
            p, self._proc = self._proc, None
        if not p:
            return
        try:
            p.terminate()
            try:
                p.wait(timeout=3)
            except Exception:
                p.kill()
        except Exception:
            pass

    # ---------- 对外 ----------
    def alive(self):
        p = self._proc
        return bool(p and p.poll() is None)

    def latest(self):
        return self._latest

    def add_sub(self):
        with self._lock:
            s = Subscriber()
            self._subs.append(s)
            self._last_active = time.time()
            need = self._proc is None or self._proc.poll() is not None
            recent = self._recent()
        # 新订阅者先把最近一段缓冲吃进去（含 PAT/PMT/SPS/PPS/I 帧），秒开
        if recent:
            s.push(recent)
        if need:
            self._spawn()
        return s

    def del_sub(self, s):
        s.close()
        with self._lock:
            if s in self._subs:
                self._subs.remove(s)
            self._last_active = time.time()

    def subscribers(self):
        with self._lock:
            return len(self._subs)

    def tick(self):
        """守护线程调用：没人看就停流；进程意外退出则在有人看时重启。"""
        with self._lock:
            n = len(self._subs)
            idle = time.time() - self._last_active
            proc = self._proc
        if n == 0:
            if proc is not None and idle > self.idle_stop:
                self._stop_proc()
            return
        if proc is None or proc.poll() is not None:
            if idle >= 0:
                # 崩溃后 2 秒内不反复重启，避免摄像机不可达时刷进程
                if time.time() - getattr(self, "_last_spawn_try", 0) > 2.0:
                    self._last_spawn_try = time.time()
                    self._spawn()


class LiveHub(object):
    def __init__(self, cfg_getter):
        self._cfg = cfg_getter
        self._cams = {}
        self._lock = threading.RLock()
        self._janitor = None

    def _opts(self):
        cfg = self._cfg()
        return cfg.get("live") or {}

    def get(self, cam):
        cid = str(cam.get("id"))
        opts = self._opts()
        with self._lock:
            lc = self._cams.get(cid)
            # 参数或地址变了就重建（改完设置不用重启应用）
            sig = (cam.get("url"), cam.get("transport"),
                   _tmode(opts.get("transcode")), bool(opts.get("audio", True)),
                   int(opts.get("width") or 0), int(opts.get("fps") or 0),
                   int(opts.get("quality") or 23))
            if lc is None or getattr(lc, "_sig", None) != sig:
                if lc is not None:
                    lc._stop_proc()
                lc = LiveCam(cam, opts)
                lc._sig = sig
                self._cams[cid] = lc
            self._ensure_janitor()
            return lc

    def drop(self, cam_id):
        with self._lock:
            lc = self._cams.pop(str(cam_id), None)
        if lc:
            lc._stop_proc()

    def drop_all(self):
        with self._lock:
            cams = list(self._cams.values())
            self._cams.clear()
        for lc in cams:
            lc._stop_proc()

    def _ensure_janitor(self):
        if self._janitor and self._janitor.is_alive():
            return
        self._janitor = threading.Thread(target=self._loop, daemon=True)
        self._janitor.start()

    def _loop(self):
        while True:
            time.sleep(1.0)
            with self._lock:
                cams = list(self._cams.values())
            for lc in cams:
                try:
                    lc.tick()
                except Exception:
                    pass

    def status(self):
        with self._lock:
            return {
                cid: {"alive": lc.alive(), "subs": lc.subscribers(),
                      "frames": lc.frames, "error": lc.error,
                      "tmode": lc.tmode, "video": lc.effective,
                      "video_note": lc.mode_note,
                      "uptime": int(time.time() - lc.started_at) if lc.started_at else 0}
                for cid, lc in self._cams.items()
            }
