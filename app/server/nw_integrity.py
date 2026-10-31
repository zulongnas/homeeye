# -*- coding: utf-8 -*-
"""HomeEye · 录像完整性自检（丢段/空洞/坏文件清单）

不是在"猜"录像有没有问题：分段文件名就是起始时间，相邻两段的开始时间差
如果明显超过单段时长，中间就是真的少了一段（摄像头断电、网络断了、ffmpeg
退出重连都算）。再把每段的大小/最后一段的真实时长对一遍，就能给出：

  * 空洞：哪段时间没有录像（前后两个分段的起止）
  * 坏段：0 字节，或 ffprobe 读不出时长的文件
  * 概况：每路扫描了多少段、总时长、总字节

深度模式会把**每个分段**都过一遍 ffprobe（慢，适合手动点一次）；默认只
探测每天最后一段（其余分段的结束时间用下一段的开始时间），几秒钟就能跑完。
"""
import json
import os
import threading
import time

import nw_config as C
import nw_record as R

_lock = threading.RLock()
_state = {"running": False, "at": 0.0, "result": None, "error": ""}
_cache = {"at": 0.0, "data": None}
CACHE_PATH = lambda: os.path.join(C.PKGVAR, "integrity.json")


def _load_cache():
    if _cache["data"] is not None:
        return
    try:
        with open(CACHE_PATH(), "r", encoding="utf-8") as f:
            _cache["data"] = json.load(f)
            _cache["at"] = float(_cache["data"].get("at") or 0)
    except Exception:
        _cache["data"] = None


def _save_cache(data):
    try:
        os.makedirs(C.PKGVAR, exist_ok=True)
        tmp = CACHE_PATH() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH())
    except OSError:
        pass


def _seg_start(name):
    m = R.SEG_RE.match(name or "")
    if not m:
        return 0
    try:
        return int(time.mktime(time.strptime(
            "%s-%s-%s %s:%s:%s" % m.groups(), "%Y-%m-%d %H:%M:%S")))
    except ValueError:
        return 0


def _list_day(root, cid, compact):
    base = R.cam_dir(root, cid)
    out = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for n in names:
        if not n.endswith(".mp4"):
            continue
        ts = _seg_start(n)
        if not ts or time.strftime("%Y%m%d", time.localtime(ts)) != compact:
            continue
        p = os.path.join(base, n)
        try:
            sz = os.path.getsize(p)
        except OSError:
            sz = 0
        out.append((ts, p, sz, n))
    out.sort()
    return out


def scan(cams=None, days=None, deep=False):
    """同步扫描。返回结果 dict（同时写入缓存）。days 上限 62 天。

    days / 空洞容差都读设置里的 `integrity` 一段 —— 页面上填的数就是这里用的
    数，调用方传 None 时不另外硬编码一套默认值。
    """
    cfg = C.load()
    root = C.recordings_root(cfg)
    ic = cfg.get("integrity") or {}
    if days is None:
        days = ic.get("days", 7)
    try:
        days = max(1, min(int(days or 7), 62))
    except (TypeError, ValueError):
        days = 7
    seg_min = 5
    try:
        seg_min = int((cfg.get("storage") or {}).get("segment_minutes") or 5)
    except (TypeError, ValueError):
        seg_min = 5
    try:
        tol = min(6.0, max(1.0, float(ic.get("gap_tolerance") or 1.5)))
    except (TypeError, ValueError):
        tol = 1.5
    nominal = max(60, seg_min * 60)
    gap_thr = max(120, int(nominal * tol))
    if cams:
        by_id = {str(c.get("id")): c for c in (cfg.get("cameras") or [])}
        cams = [by_id.get(str(x), {"id": str(x), "name": str(x)}) for x in cams]
    else:
        cams = [c for c in (cfg.get("cameras") or []) if c.get("enabled", True)]
    out = {"ok": True, "at": time.time(),
           "at_text": time.strftime("%Y-%m-%d %H:%M:%S"),
           "days": days, "deep": bool(deep),
           "segment_minutes": seg_min, "root": root,
           "cams": [], "gaps_total": 0, "bad_total": 0,
           "segments_total": 0, "bytes_total": 0}
    for cam in cams:
        cid = str(cam.get("id"))
        item = {"cam": cid, "name": cam.get("name") or cid,
                "segments": 0, "bytes": 0, "hours": 0.0,
                "gaps": [], "bad": [], "empty_days": 0, "days": []}
        for back in range(days):
            day_ts = time.time() - back * 86400
            compact = time.strftime("%Y%m%d", time.localtime(day_ts))
            segs = _list_day(root, cid, compact)
            if not segs:
                item["empty_days"] += 1
                continue
            item["days"].append(compact)
            for i, (ts, p, sz, n) in enumerate(segs):
                item["segments"] += 1
                item["bytes"] += sz
                out["segments_total"] += 1
                out["bytes_total"] += sz
                if sz <= 0:
                    item["bad"].append({"file": os.path.relpath(p, root),
                                        "why": "0 字节", "at": ts})
                    continue
                if i + 1 < len(segs):
                    end = segs[i + 1][0]
                else:
                    dur = R._ffprobe_duration(p)
                    end = ts + (dur if dur > 0 else nominal)
                    if deep and dur <= 0:
                        item["bad"].append({
                            "file": os.path.relpath(p, root),
                            "why": "ffprobe 读不出时长（文件可能损坏）", "at": ts})
                if deep and i + 1 < len(segs):
                    if R._ffprobe_duration(p) <= 0:
                        item["bad"].append({
                            "file": os.path.relpath(p, root),
                            "why": "ffprobe 读不出时长（文件可能损坏）", "at": ts})
                item["hours"] += max(0, end - ts) / 3600.0
                if i + 1 < len(segs):
                    nxt = segs[i + 1][0]
                    # 两段起点差明显超过单段时长 = 中间少了一段（或多段）
                    if nxt - ts > gap_thr:
                        item["gaps"].append({
                            "from": ts + nominal, "to": nxt,
                            "seconds": int(nxt - ts - nominal),
                            "after": os.path.relpath(p, root)})
            # 跨天闭合：当天最后一段的结束时间与次日第一段的开始时间
            if back + 1 < days:
                nxt_day = time.strftime("%Y%m%d", time.localtime(day_ts - 86400))
                nxt_segs = _list_day(root, cid, nxt_day)
                if nxt_segs:
                    last_ts, last_p, _sz, _n = segs[-1]
                    dur = R._ffprobe_duration(last_p) or nominal
                    gap = nxt_segs[0][0] - (last_ts + dur)
                    if gap > max(300, gap_thr):
                        item["gaps"].append({
                            "from": last_ts + dur, "to": nxt_segs[0][0],
                            "seconds": int(gap),
                            "after": os.path.relpath(last_p, root)})
        item["hours"] = round(item["hours"], 2)
        item["gaps"].sort(key=lambda g: g["from"])
        item["bad"].sort(key=lambda b: b["at"])
        out["gaps_total"] += len(item["gaps"])
        out["bad_total"] += len(item["bad"])
        out["cams"].append(item)
    with _lock:
        _cache["data"] = out
        _cache["at"] = out["at"]
    _save_cache(out)
    return out


def take_snapshot(cams=None, days=None, deep=False):
    """后台跑一次（API 用），立刻返回；结果用 status() 查。"""
    with _lock:
        if _state["running"]:
            return {"started": False, "running": True}
        _state["running"] = True
        _state["error"] = ""

    def _job():
        try:
            res = scan(cams=cams, days=days, deep=deep)
            with _lock:
                _state["result"] = res
                _state["at"] = res["at"]
        except Exception as e:
            with _lock:
                _state["error"] = str(e)[:300]
        finally:
            with _lock:
                _state["running"] = False

    threading.Thread(target=_job, daemon=True, name="he-integrity").start()
    return {"started": True, "running": True}


def status():
    _load_cache()
    with _lock:
        return {"running": _state["running"],
                "error": _state["error"],
                "at": _state["at"] or _cache["at"],
                "result": _state["result"] or _cache["data"]}


# ---------------------------------------------------------------- 定时自检
def _loop():
    """每 6 小时自查一次（integrity.enabled 关了就不动）。

    空洞这件事越早发现越有用：等用户点开回放页才发现少了 40 分钟，已经来不及
    去查是哪台设备掉线了。结果只落缓存，不发消息 —— 要不要通知由人决定。
    """
    while True:
        try:
            cfg = C.load()
            if (cfg.get("integrity") or {}).get("enabled", True):
                take_snapshot()
        except Exception:
            pass
        time.sleep(6 * 3600.0)


_started = False


def start():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="he-integrity-loop").start()


# ---------------------------------------------------------------- 自测
def _selftest():
    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond

    chk("起始时间解析", _seg_start("20261031-101500.mp4") > 0)
    chk("下划线也认", _seg_start("20261031_101500.mp4") ==
        _seg_start("20261031-101500.mp4"))
    chk("认不出的返回 0", _seg_start("camera.mp4") == 0)
    # 空洞阈值：5 分钟段 × 1.5 容差 = 750 秒。相邻两段起点差超过它就是少了东西
    nominal, tol = 300, 1.5
    gap_thr = max(120, int(nominal * tol))
    chk("正常接续不算空洞", nominal + 30 <= gap_thr)
    chk("少一段算空洞", 2 * nominal > gap_thr)
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)
