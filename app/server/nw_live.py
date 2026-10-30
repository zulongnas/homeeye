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

**为什么要按关键帧对齐**（1.2.0 修的花屏）：缓冲原本只管"最近 N 字节"，
新观看端从任意位置开吃 —— 而实测主码流的 GOP 是 3.35 秒，512KB 环形缓冲
在 8 Mbps 下只有半秒，等于**每次加入都必然落在 GOP 中间**。浏览器没有 IDR
就硬解，画面中间糊一条绿带，直到下一个关键帧才自愈。现在环形缓冲按
"能盖住一个 GOP"来定，并且只从最后一个 IDR 起播；慢客户端积压丢包后同样
标记"等下个 IDR"，宁可空半秒，也不喂半截帧。
"""
import collections
import os
import subprocess
import threading
import time

TS_PACKET = 188
# 环形缓冲：要盖得住一整个 GOP。按实测主码流 8 Mbps / GOP 3.35 s 算，
# 4 MB ≈ 4 秒，够；子码流（1~2 Mbps）下是 20 秒，更够。
_RING = 4 * 1024 * 1024
# 单个观看端允许积压的字节（8 Mbps 下约 2 秒）。超过就整条队列作废、
# 等下一个 IDR —— 半截帧喂给浏览器只会解出绿带。
_MAX_BACKLOG = 2 * 1024 * 1024

# H.264 的 NAL 头是低 5 位（5=IDR、7=SPS），H.265 是高 6 位（19/20=IDR、
# 16=BLA、32/33=VPS/SPS）。参数集开头也算关键帧 —— 解码器本来就要先拿到它们。
_H264_KEY = (5, 7)
_HEVC_KEY = (16, 19, 20, 32, 33)


def _keyframe_offset(batch, codec=None):
    """返回这一批 TS 字节里「最后一个关键帧起始包」的偏移，没有就返回 -1。

    codec 是这一路的视频编码（"h264"/"hevc"，未知传 None）。必须传：H.264
    的非 IDR 切片（0x41）按 H.265 的位布局解读恰好是 VPS（32），不锁编码的话
    每个普通帧都会被当成关键帧，对齐就完全失效了。

    只认 payload_unit_start 的视频 PES 包：TS 头 → （可选）adaptation field
    → PES 头 → Annex-B 起始码 → NAL 头。整批 32KB 约 174 个包，真正要细看的
    只有每帧开头那几个包，代价可以忽略。
    """
    h264_key = codec != "hevc"
    hevc_key = codec in (None, "hevc")
    n = len(batch)
    found = -1
    i = 0
    while i + TS_PACKET <= n:
        if batch[i] != 0x47:
            i += 1
            continue
        end = i + TS_PACKET
        if batch[i + 1] & 0x40:                      # payload_unit_start
            afc = (batch[i + 3] >> 4) & 0x3
            p = i + 4
            if afc & 0x2:                            # 有 adaptation field
                al = batch[p] & 0x1F if p < end else 0
                p += 1 + al
            if p + 9 <= end and batch[p] == 0 and batch[p + 1] == 0 \
                    and batch[p + 2] == 1 and 0xE0 <= batch[p + 3] <= 0xEF:
                # PES 头 9 字节：stream_id 在 p+3，header_data_length 在 p+8，
                # ES（Annex-B）从 p+9+hdl 开始 —— 差一个字节就永远找不到 IDR。
                q = p + 9 + batch[p + 8]
                # 一个访问单元里 IDR 前面通常还垫着 AUD/SPS/PPS，只看第一个 NAL
                # 会全部漏掉；从 ES 开头把本包内的 NAL 都扫一遍。
                r = q
                while r + 4 < end:
                    if batch[r] == 0 and batch[r + 1] == 0:
                        if batch[r + 2] == 1:
                            sc = 3
                        elif batch[r + 2] == 0 and batch[r + 3] == 1:
                            sc = 4
                        else:
                            r += 1
                            continue
                    else:
                        r += 1
                        continue
                    b0 = batch[r + sc]
                    if (h264_key and (b0 & 0x1F) in _H264_KEY) \
                            or (hevc_key and ((b0 >> 1) & 0x3F) in _HEVC_KEY):
                        found = i
                        break
                    r += sc
        i += TS_PACKET
    return found




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
    """一个观看端：自己的字节队列（有界），避免慢客户端拖垮快客户端。

    needs_key —— 这一路接下来必须先攒到一个 IDR 才能喂给浏览器。新订阅者
    进来时、以及积压被丢弃后都会置上，两种情况原本正是绿带的来源。
    """

    __slots__ = ("q", "cv", "closed", "t0", "needs_key")

    def __init__(self, needs_key=False):
        self.q = collections.deque()
        self.cv = threading.Condition()
        self.closed = False
        self.t0 = time.time()
        self.needs_key = needs_key

    def push(self, data, koff=-1):
        with self.cv:
            if self.closed:
                return
            if self.needs_key:
                if koff < 0:
                    return
                data = data[koff:]
                if not data:
                    return
                self.needs_key = False
            self.q.append(data)
            total = sum(len(x) for x in self.q)
            if total > _MAX_BACKLOG:
                # 积压到这份上说明这一路已经追不上了。继续喂只会让浏览器
                # 拿到中间断掉的一段，解出绿带；不如整条作废，等下个 IDR。
                self.q.clear()
                self.needs_key = True
                self.cv.notify()
                return
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
        self._codec = ""
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
        # 关键帧对齐要知道这条流的真实编码：转码出来必是 H.264，直拷就用探到的
        self._codec = "h264" if need else (codec or "")
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
                    self._feed(batch, _keyframe_offset(batch, self._codec))
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

    def _feed(self, batch, koff=-1):
        """把一批 TS 字节写进环形缓冲并广播给所有订阅者。

        koff 是这批里最后一个关键帧起始包的偏移（没有则 -1），环形缓冲要记住它，
        新订阅者才知道该从哪里开始喂浏览器。
        """
        with self._lock:
            self._ring.append((batch, koff))
            self._ring_len += len(batch)
            while self._ring_len > _RING:
                self._ring_len -= len(self._ring.popleft()[0])
            subs = list(self._subs)
        for s in subs:
            s.push(batch, koff)

    def _recent(self):
        """新订阅者的秒开数据：环形缓冲里「最后一个 IDR 之后」的那一段。

        返回 (字节, 是否从关键帧对齐)。缓冲里还没有关键帧（GOP 比缓冲还长）时
        返回空，让订阅者挂 needs_key 等下一个 IDR，也不要把半截 GOP 喂进去。
        """
        with self._lock:
            ring = list(self._ring)
        for idx in range(len(ring) - 1, -1, -1):
            batch, koff = ring[idx]
            if koff >= 0:
                parts = [batch[koff:]] + [b for b, _k in ring[idx + 1:]]
                return b"".join(parts), True
        return b"", False

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
            need = self._proc is None or self._proc.poll() is not None
            # 缓冲里没有 IDR 就先挂着等：宁可让浏览器晚半秒出画面，
            # 也不要从 GOP 中间开吃 —— 那正是那条绿带。
            recent, aligned = self._recent()
            s = Subscriber(needs_key=not aligned)
            self._subs.append(s)
            self._last_active = time.time()
        # 新订阅者先把「最后一个 IDR 之后」这段吃进去（含 PAT/PMT/SPS/PPS/I 帧），秒开
        if recent:
            s.push(recent, 0)
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

    def get(self, cam, tier="high"):
        """取一路直播。tier="low" 用摄像头配置里的子码流地址（url_low），
        没配就回落主码流 —— 网格里 4/9 宫格没必要每格都拉 8 Mbps 主码流。"""
        cid = str(cam.get("id"))
        low = (tier == "low")
        url = (cam.get("url_low") or "").strip() if low else (cam.get("url") or "")
        if not url:
            url = cam.get("url") or ""
        key = ("%s#low" % cid) if low and (cam.get("url_low") or "").strip() else cid
        opts = self._opts()
        with self._lock:
            lc = self._cams.get(key)
            # 参数或地址变了就重建（改完设置不用重启应用）
            sig = (url, cam.get("transport"),
                   _tmode(opts.get("transcode")), bool(opts.get("audio", True)),
                   int(opts.get("width") or 0), int(opts.get("fps") or 0),
                   int(opts.get("quality") or 23))
            if lc is None or getattr(lc, "_sig", None) != sig:
                if lc is not None:
                    lc._stop_proc()
                cam2 = dict(cam)
                cam2["url"] = url
                lc = LiveCam(cam2, opts)
                lc._sig = sig
                self._cams[key] = lc
            self._ensure_janitor()
            return lc

    def drop(self, cam_id):
        with self._lock:
            lc = self._cams.pop(str(cam_id), None)
            lc_low = self._cams.pop("%s#low" % cam_id, None)
        for x in (lc, lc_low):
            if x:
                x._stop_proc()

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
