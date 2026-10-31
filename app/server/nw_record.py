# -*- coding: utf-8 -*-
"""HomeEye · 录像与回放索引

录像策略：`-c copy` 直接落盘（不解码、不转码），4 路 1080p 在 4 核 Xeon 上
CPU 占用基本可以忽略；分段用 ffmpeg 的 segment muxer，按 时钟切段，
文件名就是起始时间（HHMMSS.mp4），所以**索引不需要数据库**——
目录结构本身就是索引，重启/断电都不会出现"数据库和文件对不上"。

mp4 用 fragmented 模式（+frag_keyframe+empty_moov），这样正在写入的分段
也能被浏览器播放（否则 moov 在文件尾，播放器会判定为损坏）。
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time

import nw_privacy as PV

_lock = threading.RLock()
_procs = {}      # cam_id -> Popen
_urls = {}       # cam_id -> (在录的地址, 遮蔽滤镜)；换源或改遮蔽都靠它发现该重起
_thr = {}        # cam_id -> Thread
_errs = {}       # cam_id -> 最后一条 ffmpeg 报错
_started = {}    # cam_id -> 进程启动时刻（用来识别「起来就挂」）
_backoff = {}    # cam_id -> {"n": 连续快速退出次数, "until": 最早下次尝试时刻}

# 起来不到这么久就退出，算一次"白起"，按次数指数退避。
# 没有这段退避时：主卧摄像头断电的 3.5 小时里 reconcile 每 3 秒起一个 ffmpeg，
# 累计约 2300 个进程、1900 次超时，CPU 和日志都被白烧。
FAST_EXIT = 20.0

# 分隔符同时接受 - 和 _：RTSP 一路写 %Y%m%d-%H%M%S，小米回调流一路历史版本
# 写的是 %Y%m%d_%H%M%S。只认 "-" 会让小米的录像在回放页全部"查无录像"，
# 而磁盘上文件明明存在（1.0.3 就踩了这个坑），所以这里两种都认。
SEG_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})[-_](\d{2})(\d{2})(\d{2})\.mp4$")


def _ffmpeg_bin():
    return os.environ.get("HE_FFMPEG") or "ffmpeg"


# path -> (mtime, 时长秒)。ffprobe 一次几毫秒到几十毫秒，但 segments() 在
# playlist 的跨天循环里会被反复调，缓存省的就是这些重复探测。
_durs = {}


def _ffprobe_duration(path):
    """分段的真实时长（秒），探不出或还在写就返回 0（调用方退回估算值）。"""
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return 0
    hit = _durs.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        p = subprocess.run(
            ["ffprobe", "-hide_banner", "-loglevel", "error",
             "-show_entries", "format=duration", "-of", "csv=p=0", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8)
        dur = float((p.stdout or b"").strip() or 0)
    except Exception:
        return 0
    if dur > 0:
        _durs[path] = (mt, dur)
    return dur


def cam_dir(root, cam_id):
    return os.path.join(root, str(cam_id))


# ---------------------------------------------------------------- 录哪一路
PLAY_SUFFIX = "_play"


def is_play_url(url):
    """`rtsp://127.0.0.1:8555/ktcam_play` 这种 go2rtc 转码流。"""
    u = str(url or "")
    head = u.split("?")[0]
    return head.endswith(PLAY_SUFFIX)


def raw_url_of(cam):
    """这一路的原始流地址（不经过 go2rtc 的 H.264 转码）。

    优先用配置里显式写的 `raw_url`；没有就把 `*_play` 尾巴去掉 —— 这正是
    he_prod_migrate.sh 建立 `ktcam` / `ktcam_play` 成对命名时定下的约定。
    地址本身不是 `*_play`（直接录摄像机 RTSP 的普通摄像头）时，原始流就是它自己。
    """
    explicit = str(cam.get("raw_url") or "").strip()
    if explicit:
        return explicit
    url = str(cam.get("url") or "")
    if is_play_url(url):
        return url[:-len(PLAY_SUFFIX)]
    return url


def record_url(cam, source="PLAYED"):
    return raw_url_of(cam) if str(source or "PLAYED").upper() == "RAW" \
        else (cam.get("url") or "")


def source_note(cam, source="PLAYED"):
    """这一档实际会录到什么，给人看的一句话。选错了要说得出来错在哪。"""
    url = str(cam.get("url") or "")
    if (cam.get("type") or "rtsp").lower() == "xiaomi":
        # 小米这路根本没有 RTSP 地址：ffmpeg 拿的是 SDK 直接喂进来的原始码流，
        # go2rtc 那道转码压根不在链路上 —— 说"录的是同一路"是骗人。
        return ("这路是米家摄像头，由 SDK 直接拉原始流录制，不经过 go2rtc 的转码，"
                "这一项对它没有影响")
    if str(source or "PLAYED").upper() != "RAW":
        return "录转码后的画面（与实时看到的一致，文件是 H.264，浏览器直接能放）"
    if not is_play_url(url) and not cam.get("raw_url"):
        return ("这路的地址本身就是原始流（不是 *_play 转码流），"
                "RAW 和 PLAYED 录的是同一路")
    return ("录摄像头原始流 %s：画质与体积都是摄像机自己那份"
            "（小米一般是 H.265，省一半体积），但浏览器放不了 H.265 的分段，"
            "回放页会走实时转码那一条路" % raw_url_of(cam))


def seg_cmd(cam, root, segment_minutes, record_audio=True, source="PLAYED",
            crf=23):
    # 平铺到单层目录（cam_dir 已预先 mkdir），文件名带日期：
    #   root/c2/20261029-143000.mp4
    # 不用 %Y/%m/%d 多级目录——ffmpeg segment muxer 不会自动建目录，
    # 跨天时多级目录缺失会导致录像写不进去。
    out = os.path.join(cam_dir(root, str(cam.get("id"))), "%Y%m%d-%H%M%S.mp4")
    transport = (cam.get("transport") or "tcp").lower()
    url = record_url(cam, source)
    cmd = [
        _ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "warning",
    ]
    if url.lower().startswith("rtsp"):
        cmd += ["-rtsp_transport", transport]
        # 用摄像机时间戳当墙钟，断电重连后时间轴不会错位（仅 RTSP 适用）
        cmd += ["-use_wallclock_as_timestamps", "1"]
    if url.lower().startswith("file"):
        # 本地文件源：循环播放（测试/回放源用），否则 60 秒播完就 EOF
        cmd += ["-stream_loop", "-1"]
    cmd += ["-i", url]
    # 音轨可选映射：有些 RTSP 摄像机只有一个视频流，写死 -map 0:a 会让
    # ffmpeg 直接退出、一个 mp4 都录不出来。有音轨时统一转 AAC（mp4 里
    # G.711/PCM 兼容性差，手机和浏览器放不出来）。
    cmd += ["-map", "0:v:0"]
    if record_audio:
        cmd += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "64k",
                "-ar", "44100", "-ac", "1"]
    else:
        # 用户在设置里关了「录像带声音」。
        #
        # 🔴 不只是省空间：go2rtc 转码流（ffmpeg:ktcam#...#audio=aac#raw=-ar 44100
        # -af aresample=async=1,...）的音频时间戳并不连续，ffmpeg 用 -c:a aac
        # 重编码时会刷 "Queue input is backward in time"，极端情况下直接
        # "Error muxing a packet" 退出 —— 那样录像进程就没了。
        # 实测只映射视频时零告警、产物正常。
        cmd += ["-an"]
    mask_vf = PV.filters(cam)
    if mask_vf:
        # 隐私遮蔽要烧进像素，录像就必须重编码（copy 改不了画面）。分段边界靠
        # -force_key_frames 落在每 segment 的起点，否则切片点不在关键帧上，
        # 回放拖过去会花一段。CRF 用设置页「实时画面质量」同一个数。
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast",
            "-crf", str(min(31, max(1, int(crf)))), "-pix_fmt", "yuv420p",
            "-vf", ",".join(mask_vf),
            "-force_key_frames",
            "expr:gte(t,n_forced*%d)" % (int(segment_minutes) * 60),
        ]
    else:
        cmd += ["-c:v", "copy"]
    cmd += [
        "-f", "segment",
        "-segment_time", str(int(segment_minutes) * 60),
        "-reset_timestamps", "1",
        "-strftime", "1",
        "-segment_format", "mp4",
        "-segment_format_options", "movflags=+frag_keyframe+empty_moov+default_base_moof",
        out,
    ]
    return cmd


def _err_reader(cam_id, proc):
    """收集 ffmpeg 的 stderr；遇到「真故障」类错误时按冷却策略发一次告警邮件。

    注意：网络抖动、关键帧丢失这类瞬时错误非常常见，只有明确的
    连接失败 / 打不开输入 才值得惊动用户，否则邮箱会被刷爆。
    """
    fatal_pat = ("Connection refused", "No route to host", "Connection timed out",
                 "Could not connect", "Invalid data found", "Server returned 4",
                 "Server returned 5", "Immediate exit requested", "401 Unauthorized",
                 "403 Forbidden", "404 Not Found")
    try:
        for line in iter(proc.stderr.readline, b""):
            s = line.decode("utf-8", "replace").strip()
            if not s:
                continue
            _errs[cam_id] = s[-300:]
            if any(p in s for p in fatal_pat):
                try:
                    import nw_config as _C
                    cam = _C.get_camera(cam_id) or {}
                    import nw_notify as N
                    N.notify_fault(cam.get("name") or cam_id, s[-300:])
                except Exception:
                    pass
    except Exception:
        pass


def _note_exit(cam_id):
    """记下一个录像进程的「死法」：起来就挂 = 源不通，按次数指数退避。

    正常跑完一段再退出的（>= FAST_EXIT）不算失败，计数清零 —— 全时录像本来
    就是每段重启一次 muxer，不能把它当故障。
    """
    started = _started.get(cam_id) or 0
    life = (time.time() - started) if started else 0
    b = _backoff.get(cam_id) or {"n": 0, "until": 0.0}
    if started and life < FAST_EXIT:
        b["n"] += 1
        b["until"] = time.time() + min(300.0, 10.0 * (2 ** min(b["n"] - 1, 5)))
    else:
        b = {"n": 0, "until": 0.0}
    _backoff[cam_id] = b


def _try_now(cam_id):
    """按退避窗口决定这一轮要不要起进程。"""
    b = _backoff.get(cam_id)
    return not (b and b.get("n") and time.time() < b.get("until", 0))


def _ensure_dir_writable(path):
    """能不能在这儿落文件。判据落在 nw_config.ensure_writable（设置页选目录时
    用同一个函数，两边不会一个说能写一个说不能）。"""
    import nw_config as C
    ok, err = C.ensure_writable(path)
    if not ok:
        # 这条必须响：目录写不进去 = 静默丢录像（进程在、页面开、/api/state 200，
        # 磁盘上就是不长文件）。错误由 nw_health.storage_state() 升级成告警。
        _errs["_storage"] = "录像目录不可写：%s（%s）" % (path, err)
        return False
    _errs.pop("_storage", None)
    return True


class Recorder(object):
    def __init__(self, cfg_getter, root_getter):
        self._cfg = cfg_getter
        self._root = root_getter
        self._stop = False
        self._cleaned = 0
        # 看门狗的心跳依据：线程"活着"不等于"在转圈"，只有心跳能区分卡死
        self._beat = {"watch": 0.0, "clean": 0.0}
        # 线程出生时间：还没打过第一次心跳时用它兜底，避免开局误判卡死
        self._spawned = {}
        self._threads = {}

    # ---------- 生命周期 ----------
    def start(self):
        self._spawn("watch", self._watch_loop)
        self._spawn("clean", self._clean_loop)

    def _spawn(self, key, target):
        t = threading.Thread(target=target, daemon=True, name="he-rec-" + key)
        self._threads[key] = t
        self._spawned[key] = time.time()
        t.start()

    def ensure_loops(self, stale_seconds=180.0):
        """看门狗用：录像循环死了或卡住不转圈了，就补一个。

        这一路一旦停转就是**静默丢录像**：进程还在、页面还能开、`/api/state`
        照样 200，但磁盘上不再长文件。所以它必须有人看着。

        两个循环的节奏差三个数量级（reconcile 3 秒一圈，清理 15 分钟一圈），
        所以各自的"多久没转算卡死"要分开算，不能拿 180 秒去要求清理循环 ——
        那会把每 15 分钟一次的正常睡觉判成死亡。
        """
        fixed = []
        now = time.time()
        for key, target, stale in (("watch", self._watch_loop, stale_seconds),
                                   ("clean", self._clean_loop, 1500.0)):
            t = self._threads.get(key)
            alive = t is not None and t.is_alive()
            # 心跳在干活之后才打（这样卡死时心跳会停），所以刚起线程那一圈还没打点。
            # 没打过点不等于卡死：拿出生时间当参照，否则开站第一轮巡检就会把
            # 正常启动中的循环判成死亡并补第二个 —— 重复循环正是这个模块要防的事。
            ref = self._beat.get(key) or self._spawned.get(key) or 0.0
            if alive and ref and now - ref <= stale:
                continue
            if self._stop:
                continue
            self._spawn(key, target)
            fixed.append(key)
        return fixed


    def heartbeat(self):
        return dict(self._beat)

    def stop(self):
        self._stop = True
        with _lock:
            for cid, p in list(_procs.items()):
                try:
                    p.terminate()
                except Exception:
                    pass
            _procs.clear()
            _urls.clear()

    def restart(self, cam_id, why=""):
        """主动掐掉这路的录像进程，让下一轮 reconcile 用新源重起。

        看门狗改过 go2rtc 的源之后必须调一次：ffmpeg 手里那条 RTSP 连接属于
        被换掉的旧 stream，它自己不会重连到新 stream（实测只会继续读旧连接直到
        超时）。清掉退避计数，是为了让"修好了"立刻见效，而不是等下一轮退避。
        """
        cid = str(cam_id)
        with _lock:
            p = _procs.pop(cid, None)
            _urls.pop(cid, None)
            _started.pop(cid, None)
            _backoff.pop(cid, None)
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass
            return True
        return False


    def _watch_loop(self):
        while not self._stop:
            try:
                self.reconcile()
                self._reconcile_xiaomi()
            except Exception:
                pass
            # 心跳放在干活之后：卡死在 reconcile 里时心跳会停，看门狗才看得出
            self._beat["watch"] = time.time()
            time.sleep(3.0)

    def _reconcile_xiaomi(self):
        """小米摄像头不经过 RTSP ffmpeg，改由 XiaomiHub 拉流并附带录像输出。"""
        try:
            import nw_xiaomi as X
        except Exception:
            return
        if not X.get_auth().snapshot().get("has_token"):
            return   # 没登录就不用尝试
        cfg = self._cfg()
        root = self._root()
        seg = (cfg.get("storage") or {}).get("segment_minutes") or 5
        # record_mode=off 时连小米也不录
        mode = (cfg.get("storage") or {}).get("record_mode") or "continuous"
        cams = cfg.get("cameras") or []
        if mode == "off":
            cams = []
        else:
            cams = [c for c in cams if c.get("record", True)]
        try:
            X.get_hub().reconcile(cams, root, seg)
        except Exception:
            pass

    def reconcile(self):
        """按配置对齐：该录的在跑，不该录的停掉，挂掉的拉起来。"""
        cfg = self._cfg()
        root = self._root()
        mode = (cfg.get("storage") or {}).get("record_mode") or "continuous"
        want = {}
        if mode != "off":
            for cam in cfg.get("cameras") or []:
                # 小米摄像头走回调流，不是 RTSP，ffmpeg 拉不了 —— 由 _reconcile_xiaomi 处理
                if (cam.get("type") or "rtsp").lower() == "xiaomi":
                    continue
                if cam.get("enabled", True) and cam.get("record", True) and cam.get("url"):
                    want[str(cam.get("id"))] = cam
        if want and not _ensure_dir_writable(root):
            return
        want = self._drop_offline(want)
        with _lock:
            # 停掉不再需要的
            for cid in list(_procs.keys()):
                if cid not in want:
                    try:
                        _procs.pop(cid).terminate()
                    except Exception:
                        pass
                    _started.pop(cid, None)
                    _urls.pop(cid, None)
            # 拉起需要的
            for cid, cam in want.items():
                st = cfg.get("storage") or {}
                url_now = record_url(cam, st.get("source") or "PLAYED")
                # 遮蔽改了也要重起：ffmpeg 手里的滤镜组是启动时定死的，运行中
                # 加不上。把它并进"这一路当前在录什么"的签名里，换源那段现成的
                # 重起逻辑就顺带把遮蔽变更也管了。
                sig = (url_now, tuple(PV.filters(cam)))
                p = _procs.get(cid)
                if p is not None:
                    if p.poll() is None:
                        if _urls.get(cid) == sig:
                            continue
                        # 源换了（RAW/PLAYED 来回切，或地址改了）：ffmpeg 手里那条
                        # RTSP 连接属于被换掉的旧流，它自己不会重连到新流，只能掐掉
                        # 重起。这是"照用户要求换路"，不是起挂了，所以不动退避计数。
                        _procs.pop(cid, None)
                        _urls.pop(cid, None)
                        _started.pop(cid, None)
                        try:
                            p.terminate()
                        except Exception:
                            pass
                    else:
                        _procs.pop(cid, None)
                        _urls.pop(cid, None)
                        _note_exit(cid)
                if not _try_now(cid):
                    continue
                os.makedirs(cam_dir(root, cid), exist_ok=True)
                cmd = seg_cmd(cam, root, st.get("segment_minutes") or 5,
                              record_audio=bool(st.get("record_audio", True)),
                              source=st.get("source") or "PLAYED",
                              crf=(cfg.get("live") or {}).get("quality") or 23)
                try:
                    pr = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.PIPE, close_fds=True)
                except Exception as e:
                    _errs[cid] = "启动录像失败：%s" % e
                    continue
                _procs[cid] = pr
                _urls[cid] = sig
                _started[cid] = time.time()
                _errs.pop(cid, None)
                threading.Thread(target=_err_reader, args=(cid, pr), daemon=True).start()

    def _drop_offline(self, want):
        """把判定为「设备离线」的摄像头从待录集合里摘掉。

        断电期间起录像进程只会白起：ffmpeg 连 go2rtc 那路 RTSP 拿不到流就退出，
        下一轮 reconcile 又起一个。设备自己回来时，健康检查翻回 ok，这里不再
        拦截，3 秒内恢复录制 —— 所以拦掉的只是"注定失败的重试"，不是录像本身。
        """
        if not want:
            return want
        try:
            import nw_health as H
            offline = H.offline_ids()
        except Exception:
            return want
        if not offline:
            return want
        return {cid: cam for cid, cam in want.items() if cid not in offline}

    def status(self):
        with _lock:
            out = {}
            for cid, p in _procs.items():
                sig = _urls.get(cid) or ("", ())
                out[cid] = {"running": p.poll() is None, "pid": p.pid,
                            "url": sig[0] if isinstance(sig, tuple) else sig,
                            "masked": bool(isinstance(sig, tuple) and sig[1]),
                            "error": _errs.get(cid, "")}
            for cid, b in _backoff.items():
                if b.get("n") and b.get("until", 0) > time.time():
                    out.setdefault(cid, {"running": False, "error": ""})[
                        "retry_in"] = int(b["until"] - time.time())
            if "_storage" in _errs:
                out["_storage"] = {"running": False, "error": _errs["_storage"]}
            return out

    # ---------- 清理 ----------
    def _clean_loop(self):
        while not self._stop:
            try:
                self.cleanup()
            except Exception:
                pass
            self._beat["clean"] = time.time()
            time.sleep(900)


    def _locked_files(self):
        """被本地事件引用的录像段（相对路径集合）——清理时留到最后删。"""
        root = self._root()
        ev = os.path.join(root, "events")
        locked = set()
        if not os.path.isdir(ev):
            return locked
        for cid in os.listdir(ev):
            cdir = os.path.join(ev, cid)
            if not os.path.isdir(cdir):
                continue
            for n in os.listdir(cdir):
                if not n.endswith(".jsonl"):
                    continue
                try:
                    with open(os.path.join(cdir, n), "r", encoding="utf-8") as f:
                        for line in f:
                            try:
                                fn = json.loads(line).get("file")
                            except Exception:
                                continue
                            if fn:
                                locked.add(str(fn).replace("\\", "/"))
                except OSError:
                    continue
        return locked

    def cleanup(self):
        cfg = self._cfg()
        st = cfg.get("storage") or {}
        root = self._root()
        days = int(st.get("retention_days") or 0)
        budget_gb = float(st.get("budget_gb") or 0)
        if not os.path.isdir(root):
            return
        files = []
        total = 0
        now = time.time()
        for dirpath, _dirs, names in os.walk(root):
            for n in names:
                if not n.endswith(".mp4"):
                    continue
                p = os.path.join(dirpath, n)
                try:
                    sz = os.path.getsize(p)
                    mt = os.path.getmtime(p)
                except OSError:
                    continue
                files.append((mt, sz, p))
                total += sz
        locked = self._locked_files()

        # 1) 按天数：事件锁住的段不按天删（生命周期跟着 keep_events_days 走）
        if days > 0:
            cutoff = now - days * 86400
            cands = [(mt, sz, p) for mt, sz, p in files
                     if mt < cutoff
                     and os.path.relpath(p, root).replace("\\", "/") not in locked]
            freed = []
            for mt, sz, p in cands:
                try:
                    os.unlink(p)
                    freed.append(sz)
                except OSError:
                    pass
            total -= sum(freed)
        # 2) 按容量上限（从最旧开始删，事件锁住的段放最后）
        if budget_gb > 0:
            limit = budget_gb * (1024 ** 3)
            if total > limit:
                cands = sorted(files, key=lambda x: x[0])
                for mt, sz, p in cands:
                    if total <= limit:
                        break
                    rel = os.path.relpath(p, root).replace("\\", "/")
                    if rel in locked:
                        continue
                    try:
                        if os.path.exists(p):
                            os.unlink(p)
                            total -= sz
                    except OSError:
                        pass
        self._cleaned = int(now)

    # ---------- 索引 ----------
    def segments(self, cam_id, date_str):
        """某一天的分段列表（date_str=YYYY-MM-DD，文件名 YYYYMMDD-HHMMSS.mp4）。"""
        root = self._root()
        base = cam_dir(root, cam_id)
        out = []
        if not os.path.isdir(base):
            return out
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return out
        # date_str 的紧凑形如 20261029
        compact = date_str.replace("-", "")
        starts = []
        for n in names:
            mt = SEG_RE.match(n)
            if not mt:
                continue
            y, m, d, hh, mm, ss = (int(x) for x in mt.groups())
            if "%04d%02d%02d" % (y, m, d) != compact:
                continue
            try:
                ts = int(time.mktime(time.strptime("%04d-%02d-%02d %02d:%02d:%02d" % (y, m, d, hh, mm, ss),
                                                   "%Y-%m-%d %H:%M:%S")))
            except Exception:
                continue
            p = os.path.join(base, n)
            try:
                sz = os.path.getsize(p)
            except OSError:
                sz = 0
            starts.append([ts, p, sz])
        starts.sort(key=lambda x: x[0])
        # 最后一段没有「下一段起点」可参照：用 ffprobe 读真实时长（带缓存）。
        # 以前按设置里的单段时长估，正在写的段会把时间轴撑到 5 分钟，
        # 点上去却是"这一天没有录像"——误差全让跳转兜底去填。
        try:
            seg_sec = max(60, int((self._cfg().get("storage") or {}).get("segment_minutes") or 5) * 60)
        except Exception:
            seg_sec = 300
        for i, (ts, p, sz) in enumerate(starts):
            if i + 1 < len(starts):
                end = starts[i + 1][0]
            else:
                dur = _ffprobe_duration(p)
                end = ts + (dur if dur > 0 else seg_sec)
            out.append({
                "file": os.path.relpath(p, root).replace(os.sep, "/"),
                "start": ts,
                "end": end,
                "size": sz,
            })
        return out

    def playlist(self, cam_id, ts_from, ts_to):
        """给回放用：把区间内的分段按时间拼起来（跨天自动展开）。"""
        out = []
        day = time.strftime("%Y-%m-%d", time.localtime(ts_from))
        last = time.strftime("%Y-%m-%d", time.localtime(ts_to))
        guard = 0
        while day <= last and guard < 400:
            guard += 1
            for s in self.segments(cam_id, day):
                if s["end"] <= ts_from or s["start"] >= ts_to:
                    continue
                out.append(s)
            try:
                t = time.mktime(time.strptime(day, "%Y-%m-%d")) + 86400
            except Exception:
                break
            day = time.strftime("%Y-%m-%d", time.localtime(t))
        return out

    def days_with_data(self, cam_id, months_back=2):
        """有录像的日期列表（回放页的日历高亮），扫描平铺文件名。"""
        root = cam_dir(self._root(), cam_id)
        out = []
        if not os.path.isdir(root):
            return out
        try:
            names = os.listdir(root)
        except OSError:
            return out
        for n in names:
            mt = SEG_RE.match(n)
            if mt:
                y, m, d = (int(x) for x in mt.groups()[:3])
                out.append("%04d-%02d-%02d" % (y, m, d))
        return sorted(set(out), reverse=True)

    def usage(self):
        root = self._root()
        total = 0
        per = {}
        if os.path.isdir(root):
            for dirpath, _dirs, names in os.walk(root):
                for n in names:
                    if not n.endswith(".mp4"):
                        continue
                    try:
                        sz = os.path.getsize(os.path.join(dirpath, n))
                    except OSError:
                        continue
                    total += sz
                    rel = os.path.relpath(os.path.join(dirpath, n), root)
                    cid = rel.split(os.sep)[0]
                    per[cid] = per.get(cid, 0) + sz
        du = shutil.disk_usage(root if os.path.isdir(root) else "/")
        return {"root": root, "total_bytes": total, "per_camera": per,
                "disk_total": du.total, "disk_free": du.free, "disk_used": du.used}
