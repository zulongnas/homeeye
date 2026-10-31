# -*- coding: utf-8 -*-
"""HomeEye · 本地事件侦测与时间轴索引（1.2.0-C）

事件从哪来：录像分段**写完之后**离线分析。每段只解关键帧
（`-skip_frame nokey`，约全部帧的 1/30~1/60），缩到 160x90 灰度后
在 Python 里做像素差分 —— 4 核 Xeon 上一段 5 分钟分段只花零点几秒
CPU，不与录像、直播抢资源，也不需要额外拉一路流。

代价是事件有延迟（分段写完 + 扫描间隔），但事件时间轴本来就是
"回看"场景：晚一两分钟标出来完全够用。要秒级通知是 1.2.0-D 的事，
到时另走低码率实时抽帧，不在这条链路上加负担。

索引不需要数据库（与录像索引同一个设计决定）：
  <录像根>/events/<cam>/YYYYMMDD.jsonl   每行一个事件 {"t0","t1","file","score"}
  <录像根>/events/<cam>/th/<段名>-<t0>.jpg  事件缩略图
  <录像根>/events/.state.json            已处理分段清单（防重扫）
分段文件删掉后，读取时按"段还在不在"过滤 —— 清理循环删录像，
事件跟着失效，两边不会对不上。
"""
import json
import os
import subprocess
import threading
import time

import nw_config as C

_lock = threading.RLock()
_state = None          # .state.json 的内容（rel -> {"st": "done"|"skip", "mt": mtime}）
_beat = 0.0            # 看门狗心跳（扫描循环真正转圈才打点）
_last = ""             # 最后一条扫描日志（给人看的）
_started = False
_scan_requested = threading.Event()   # 手动触发（测试/设置页"立即扫描"）

# 首次启用只回溯这么久：更早的历史分段直接标 skip，不在升级当晚把
# CPU 烧在几个月前的旧录像上（想要旧片事件只能重录或手动清 .state.json）。
BACKFILL_HOURS = 36
# 分段 mtime 比这新就当"还在写"，不动（segment_minutes 最大 180，留余量）。
QUIET_SECONDS = 150
FRAME_W, FRAME_H = 160, 90
FRAME_BYTES = FRAME_W * FRAME_H
GOP_GAP_SAMPLES = 3       # 连续这么多帧没动才算事件结束
MIN_EVENT_SAMPLES = 2     # 单帧闪烁不算事件
LOOP_SECONDS = 20


def _ffmpeg_bin():
    return os.environ.get("HE_FFMPEG") or "ffmpeg"


def events_root(root):
    return os.path.join(root, "events")


def state_path(root):
    return os.path.join(events_root(root), ".state.json")


def seg_ts(name):
    """20261030-143000.mp4 -> 当天零点的 unix 秒。认不出返回 0。"""
    import re
    m = re.match(r"^(\d{4})(\d{2})(\d{2})[-_](\d{2})(\d{2})(\d{2})\.mp4$", name)
    if not m:
        return 0
    try:
        st = time.strptime("%s-%s-%s %s:%s:%s" % m.groups(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0
    return int(time.mktime(st))


def frac_threshold(sensitivity):
    """灵敏度 1~100 -> 变化像素占比阈值。灵敏度高 = 阈值低 = 更容易算事件。"""
    try:
        s = max(1, min(100, int(sensitivity)))
    except Exception:
        s = 50
    return 0.006 + 0.09 * (100 - s) / 100.0


# ---------------------------------------------------------------- 检测核心
def keyframe_scan(seg_path, timeout=180):
    """一次 ffmpeg 同时给出关键帧灰度图与其 pts（秒，相对段首）。

    走 `select=eq(pict_type,I)` 而不是 ffprobe 对帧：时间戳与画面出自
    **同一次解码**，两边永远对得上；ffprobe 与 ffmpeg 分开跑时数量可能
    差一两帧（实测 59 vs 61），对不上会把时间戳张冠李戴。
    `-skip_frame nokey` 让解码器跳过非关键帧的解码（真实 5 分钟 1080p
    段从 7.3s 降到 3.3s）。ffmpeg 7.1 单用它会把未解码帧照样吐出来，
    所以必须配 select 滤镜把它们筛掉。

    数量对不上或解不出返回 (None, None) —— 宁可这段不出事件，也不能
    出错位的事件。
    """
    cmd = [_ffmpeg_bin(), "-hide_banner", "-loglevel", "info",
           "-skip_frame", "nokey", "-i", seg_path, "-an",
           "-vf", "select='eq(pict_type,I)',scale=%d:%d,showinfo" % (FRAME_W, FRAME_H),
           "-vsync", "0",
           "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pts = []

    def _eat_err():
        try:
            for line in iter(p.stderr.readline, b""):
                s = line.decode("utf-8", "replace")
                if "Parsed_showinfo" in s and "pts_time:" in s:
                    try:
                        pts.append(float(s.split("pts_time:")[1].split()[0]))
                    except Exception:
                        pass
        except Exception:
            pass

    import threading as _t
    th = _t.Thread(target=_eat_err, daemon=True)
    th.start()
    frames = []
    try:
        while True:
            chunk = p.stdout.read(FRAME_BYTES)
            if not chunk or len(chunk) < FRAME_BYTES:
                break
            frames.append(chunk)
    finally:
        try:
            p.kill()
        except Exception:
            pass
        for f in (p.stdout, p.stderr):
            try:
                f.close()
            except Exception:
                pass
        try:
            p.wait(timeout=5)
        except Exception:
            pass
        th.join(timeout=2)
    if not frames or len(frames) != len(pts):
        return None, None
    return pts, frames


def frame_motion(a, b, pixel_thr=25):
    """两张灰度帧的差异：变化像素占比（按绝对差 > pixel_thr 算"变了"）。

    不用全帧平均亮度差：那种指标对"阳光挪了一下"这种整体明暗变化没有
    抵抗力，而"几个像素明显变了"才是有人走过画面的形态。
    """
    n = len(a)
    changed = 0
    for i in range(n):
        d = a[i] - b[i] if a[i] >= b[i] else b[i] - a[i]
        if d > pixel_thr:
            changed += 1
    return changed / float(n)


def detect_events(seg_path, seg_t0, sensitivity):
    """一段录像里的事件列表 [(t0, t1, score)]，墙钟时间。"""
    pts, frames = keyframe_scan(seg_path)
    if not pts or len(pts) < MIN_EVENT_SAMPLES:
        return []
    thr = frac_threshold(sensitivity)
    flags = [False]  # flags[i] = 第 i 帧相对前一帧动了
    for i in range(1, len(frames)):
        flags.append(frame_motion(frames[i - 1], frames[i]) >= thr)
    events = []
    run = []
    for i, f in enumerate(flags):
        if f:
            run.append(i)
            continue
        # 空窗超过 GOP_GAP_SAMPLES 帧才算断（关键帧间隔 2~4 秒，
        # 间隔 3 帧仍属于"同一拨人影断断续续"）
        if run and i - run[-1] > GOP_GAP_SAMPLES:
            events.append(run)
            run = []
    if run:
        events.append(run)
    out = []
    for idx in events:
        if len(idx) < MIN_EVENT_SAMPLES:
            continue
        t0 = seg_t0 + pts[idx[0]]
        t1 = seg_t0 + pts[idx[-1]]
        if t1 - t0 < 1.0:
            continue
        out.append((t0, t1, round(float(len(idx)) / max(1, len(flags)), 3)))
    return out


def make_thumb(seg_path, offset_s, out_path):
    """事件中间截一张 320 宽的 jpg。失败不致命（时间轴照样能用）。"""
    cmd = [_ffmpeg_bin(), "-hide_banner", "-loglevel", "error",
           "-ss", "%.2f" % max(0.0, offset_s), "-i", seg_path,
           "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "5", "-y",
           out_path]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)
    except Exception:
        pass
    return os.path.isfile(out_path)


# ---------------------------------------------------------------- 存储
def load_state(root):
    global _state
    if _state is not None:
        return _state
    try:
        with open(state_path(root), "r", encoding="utf-8") as f:
            _state = json.load(f)
    except Exception:
        _state = {}
    return _state


def save_state(root):
    global _state
    with _lock:
        try:
            tmp = state_path(root) + ".tmp"
            os.makedirs(events_root(root), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_state or {}, f)
            os.replace(tmp, state_path(root))
        except Exception:
            pass


def read_day(root, cam_id, date_compact):
    """某摄像头某天的事件列表（过滤掉分段已被清理的）。"""
    p = os.path.join(events_root(root), str(cam_id),
                     "%s.jsonl" % date_compact)
    rows = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                seg = os.path.join(root, str(d.get("file") or ""))
                if not os.path.isfile(seg):
                    continue
                rows.append(d)
    except OSError:
        pass
    rows.sort(key=lambda d: d.get("t0") or 0)
    return rows


def append_events(root, cam_id, date_compact, rows):
    base = os.path.join(events_root(root), str(cam_id))
    os.makedirs(base, exist_ok=True)
    p = os.path.join(base, "%s.jsonl" % date_compact)
    with open(p, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def cam_dir_safe(root, cid):
    return os.path.join(root, cid)


# ---------------------------------------------------------------- 扫描循环
def _candidates(root, cfg):
    """没处理过、且已经写完的分段。一天最多回溯 BACKFILL_HOURS。"""
    out = []
    ids = {str(c.get("id")) for c in (cfg.get("cameras") or [])}
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    now = time.time()
    st = load_state(root)
    for cid in names:
        if cid not in ids or cid == "events":
            continue
        base = os.path.join(root, cid)
        if not os.path.isdir(base):
            continue
        try:
            segs = os.listdir(base)
        except OSError:
            continue
        for n in segs:
            if not n.endswith(".mp4"):
                continue
            ts0 = seg_ts(n)
            if not ts0:
                continue
            rel = "%s/%s" % (cid, n)
            hit = st.get(rel)
            if hit and hit.get("st") in ("done", "skip"):
                continue
            p = os.path.join(base, n)
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if now - mt < QUIET_SECONDS:
                continue
            if ts0 < now - BACKFILL_HOURS * 3600:
                # 太老的历史分段：记为 skip，以后不再扫它（这一条不是失败）
                st[rel] = {"st": "skip", "mt": mt}
                continue
            out.append((ts0, rel, p))
    return sorted(out)


def _thumb_rel(root, cid, base, path, t0, t1):
    """事件缩略图的相对路径（目录不在就先建，建不成返回空 = 不带图）。"""
    rel = "th/%s-%d.jpg" % (base, int(t0))
    full = os.path.join(events_root(root), cid, "th")
    try:
        os.makedirs(full, exist_ok=True)
    except OSError:
        return ""
    make_thumb(path, max(0.0, (t1 - t0) / 2.0), os.path.join(full,
             os.path.basename(rel)))
    return "%s/%s" % (cid, rel)


def _process_one(root, rel, path, cfg, cams_by_id):
    """一段写完的分段 -> 事件行（画面动静 / 声响 / 可选的对象标签与一句话描述）。

    三条来源各自独立开关：像素差分归 `motion.enabled`，声响与识别归 `ai.enabled`
    下面的分项。开着哪条才做哪条 —— 解码一次分段是要花真 CPU 的，不能因为"AI
    打开了"就去做根本没启用的画面分析。
    """
    global _last
    cid = rel.split("/")[0]
    cam = cams_by_id.get(cid) or {}
    mo = cfg.get("motion") or {}
    ai = cfg.get("ai") or {}
    ai_on = bool(ai.get("enabled"))
    ts0 = seg_ts(os.path.basename(path))
    base = os.path.basename(path)[:-len(".mp4")]
    rows = []
    motion_rows = []
    if mo.get("enabled"):
        evs = detect_events(path, ts0, mo.get("sensitivity", 50))
        for (t0, t1, score) in evs:
            motion_rows.append({"t0": t0, "t1": t1, "score": score,
                                "file": rel,
                                "thumb": _thumb_rel(root, cid, base, path, t0, t1),
                                "kind": "motion", "labels": ["移动"]})
        if ai_on and ai.get("objects"):
            try:
                import nw_ai as AI
                AI.annotate_objects(path, ts0, evs, motion_rows, ai)
            except Exception as e:
                _note_err("objects", e)
    rows.extend(motion_rows)

    if ai_on and ai.get("audio_events"):
        try:
            import nw_ai as AI
            for (t0, t1, score) in AI.audio_events(
                    path, ts0, ai.get("audio_threshold", 2.5)):
                rows.append({"t0": t0, "t1": t1, "score": score,
                             "file": rel,
                             "thumb": _thumb_rel(root, cid, base, path, t0, t1),
                             "kind": "audio", "labels": ["声响"]})
        except Exception as e:
            _note_err("audio", e)

    # VLM 要外发一帧画面到第三方接口，所以只给最长的一两条写描述：一段 5 分钟
    # 里可能有几十个事件，全问一遍既慢又烧钱，还让"关掉就什么都不发"变得说不清。
    if ai_on and ai.get("vlm_enabled") and rows:
        try:
            import nw_ai as AI
            picks = sorted(rows, key=lambda r: -(r["t1"] - r["t0"]))[:2]
            for row in picks:
                text = AI.describe_event(path, ts0, row, ai)
                if text:
                    row["desc"] = text
        except Exception as e:
            _note_err("vlm", e)

    rows.sort(key=lambda r: r["t0"])
    day = time.strftime("%Y%m%d", time.localtime(ts0))
    first_ts = rows[0]["t0"] if rows else ts0
    if rows:
        append_events(root, cid, day, rows)
        # 1.2.0-D：扫出动静就发通知（受总开关/on_motion/布防/冷却约束，
        # 布防撤防逻辑在 nw_notify 里）
        try:
            import nw_notify as N
            tags = []
            for r in rows:
                for lab in (r.get("labels") or []):
                    if lab not in tags:
                        tags.append(lab)
            N.notify_local_event(cam.get("name") or cid, len(rows), first_ts,
                                 labels=tags, cam_id=cid, t0=first_ts)
        except Exception:
            pass
    st = load_state(root)
    st[rel] = {"st": "done", "mt": os.path.getmtime(path), "n": len(rows)}
    save_state(root)
    _last = "%s %s -> %d 事件" % (time.strftime("%H:%M:%S"), rel, len(rows))


def _note_err(which, exc):
    """AI 某一侧失败不影响画面事件照常入库，但要把原因留给设置页说清楚。"""
    try:
        import nw_ai as AI
        AI.note_error(which, str(exc)[:160])
    except Exception:
        pass


def _prune(root):
    """分段被清理循环删掉后，把指向它的事件行与缩略图一并清掉。

    keep_events_days（storage 设置，1.2.0-D 转正）：事件明细只保留这么多天，
    超龄的行直接移除（连同一个指标：即使分段还在也不留）。
    """
    try:
        base = events_root(root)
        if not os.path.isdir(base):
            return
        keep_days = 0
        try:
            keep_days = int((C.load().get("storage") or {}).get("keep_events_days") or 0)
        except Exception:
            keep_days = 0
        min_t0 = (time.time() - keep_days * 86400) if keep_days > 0 else 0
        for cid in os.listdir(base):
            cbase = os.path.join(base, cid)
            if not os.path.isdir(cbase):
                continue
            for n in os.listdir(cbase):
                if not n.endswith(".jsonl"):
                    continue
                p = os.path.join(cbase, n)
                keep = []
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        for line in f:
                            try:
                                d = json.loads(line)
                            except Exception:
                                continue
                            too_old = min_t0 and float(d.get("t1") or d.get("t0") or 0) < min_t0
                            if too_old:
                                th = d.get("thumb")
                                if th:
                                    try:
                                        os.unlink(os.path.join(base, str(th)))
                                    except OSError:
                                        pass
                                continue
                            if os.path.isfile(os.path.join(root, str(d.get("file") or ""))):
                                keep.append(line if line.endswith("\n") else line + "\n")
                            else:
                                th = d.get("thumb")
                                if th:
                                    try:
                                        os.unlink(os.path.join(base, str(th)))
                                    except OSError:
                                        pass
                except OSError:
                    continue
                if keep:
                    with open(p, "w", encoding="utf-8") as f:
                        f.writelines(keep)
                else:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
    except Exception:
        pass


def scan_once():
    """跑一轮扫描（最多 2 段，防止 CPU 长时间被占）。返回处理条数。"""
    global _last
    root = C.recordings_root()
    cfg = C.load()
    mo = cfg.get("motion") or {}
    ai = cfg.get("ai") or {}
    # 画面动静和本地 AI 是两条独立来源：只开 AI（只听声响/只识别对象）时
    # 扫描循环照样要转，否则 ai.enabled 会变成一格骗人的开关。
    if not mo.get("enabled") and not ai.get("enabled"):
        return 0
    cams_by_id = {str(c.get("id")): c for c in (cfg.get("cameras") or [])}
    cands = _candidates(root, cfg)
    save_state(root)     # skip 标记要落盘，不然每轮都重新看见它们
    done = 0
    for ts0, rel, path in cands:
        if done >= 2:
            break
        try:
            _process_one(root, rel, path, cfg, cams_by_id)
            done += 1
        except Exception as e:
            _last = "扫描失败 %s：%s" % (rel, str(e)[:120])
            st = load_state(root)
            st[rel] = {"st": "done", "mt": 0, "n": 0, "err": str(e)[:80]}
            save_state(root)
    return done


def _loop():
    global _beat
    n = 0
    while True:
        try:
            _scan_requested.wait(LOOP_SECONDS)
            _scan_requested.clear()
            scan_once()
            _beat = time.time()
            n += 1
            if n % 90 == 0:      # 约每 30 分钟清一次孤儿事件
                _prune(C.recordings_root())
        except Exception:
            _beat = time.time()


def request_scan():
    _scan_requested.set()


def start():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="he-events").start()


def status():
    return {"beat": _beat, "last": _last,
            "pending": _scan_requested.is_set()}


# ---------------------------------------------------------------- 自测
def _selftest():
    """纯逻辑部分：阈值映射与事件合并。"""
    ok = True
    def chk(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond
    chk("阈值: 默认50在 0.03~0.07", 0.03 <= frac_threshold(50) <= 0.07)
    chk("阈值: 单调递减", frac_threshold(90) < frac_threshold(50) < frac_threshold(10))
    chk("阈值: 边界钳制", frac_threshold(1000) >= 0.006 and frac_threshold(-5) <= 0.1)
    chk("seg_ts: 认得标准名",
        seg_ts("20261030-143000.mp4") > 0)
    chk("seg_ts: 认得下划线名",
        seg_ts("20261030_143000.mp4") == seg_ts("20261030-143000.mp4"))
    chk("seg_ts: 认不出返回 0", seg_ts("abc.mp4") == 0)
    a = bytes([100]) * FRAME_BYTES
    b = bytes([100]) * FRAME_BYTES
    c = bytes([100]) * (FRAME_BYTES // 2) + bytes([220]) * (FRAME_BYTES // 2)
    chk("帧差: 静止帧为 0", frame_motion(a, b) == 0.0)
    chk("帧差: 半幅变化 ~0.5", 0.45 <= frame_motion(a, c) <= 0.55)
    chk("帧差: 半幅变化过默认阈值", frame_motion(a, c) >= frac_threshold(50))
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)
