# -*- coding: utf-8 -*-
"""HomeEye · 链路健康判定与掉线退避

为什么需要这一层：
  容器的 healthcheck 只看 `/api/state` 返不返回 200，而「全时录像已经断了
  3 小时」时它照样返回 200、照样 healthy。主卧摄像头（c1）每晚被拔插头是
  **预期工况**，不是故障 —— 所以必须把状态分开：

    ok       画面与录像都正常
    offline  设备离线（断电/断网）：系统无事可做，等它自己回来，不发邮件、
             不占着录像进程反复重试
    broken   设备可达但链路坏了（源配错、go2rtc 挂了、目录不可写）—— 只有
             这个值得惊动用户，也只有它该让 healthcheck 变红
    idle     这路既不录像也没人看，无从判断（不算异常）

  offline 期间必须拦住录像：实测断流 3.5 小时里 go2rtc 每 6 秒重试一次、
  HomeEye 每 3 秒重起一个录像 ffmpeg，刷出 1900 次超时、约 2300 个进程。
  录像侧由 `recording_allowed()` 闸门拦掉。

判据从哪来（按可靠性排序）：
  1. 录像分段的新鲜度 —— 这是唯一的业务地面真相：磁盘上在长文件就说明整条
     链路（摄像头→go2rtc→转码→RTSP→ffmpeg→落盘）真的通。
  2. go2rtc `/api/streams?json` 里 producer 的 `bytes_recv` 增量 —— 能区分
     「会话在但不出数据」（僵死）和「根本没有会话」。
  3. `/proc/net/arp` —— 容器是 host 网络，能直接看到宿主机 ARP 表。小米摄像机
     本地不开任何端口、容器里也没有 ping、ICMP socket 无权限，ARP 是唯一
     零依赖的"设备上没上电"证据（实测断电后条目变成 00:00:00:00:00:00）。
  4. 小米云的在线表（登录后才有）—— 有就用它加强判断，没有也不阻断。
"""
import collections
import os
import re
import shutil
import threading
import time

import nw_config as C

_lock = threading.RLock()
_state = {
    "cams": {},                              # cam_id -> 最近一次判定
    "events": collections.deque(maxlen=200),  # 状态翻转流水
    "prev": {},                               # cam_id -> 上一次状态
    "at": 0.0,
    "probing": False,                         # 是否已有线程在探链路
    "beat": 0.0,                              # 巡检线程最近一次转圈的时刻
    "loops": 0,                               # 起过几个巡检线程（看门狗重启的证据）
}
_thread = None

# go2rtc 码率采样：{流名: (采样时刻, bytes_recv)}，用来算增量
_rx = {}
# 摄像头 IP 缓存（配置里改了地址最迟 5 分钟生效）
_ip_cache = {"at": 0.0, "map": {}}
# 录像目录判定缓存（写探针 + statvfs 都很便宜，但 healthcheck 每 30 秒问一次，
# 没必要每次都真去碰磁盘）
_STORAGE_TTL = 30.0
_storage = {"at": 0.0, "data": None}


# ---------------------------------------------------------------- 探测原语
def _g2r_streams():
    """go2rtc 的流表：{流名: {"producers": [...], "consumers": [...]}}。

    注意返回结构是**平铺的流名 -> {producers, consumers}**（1.9.x 的
    /api/streams?json），不是老版本的 {in:.., out:..}。
    """
    try:
        import nw_ptz as P
        raw = P._g2r_call("/api/streams?json", timeout=5)
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _stream_of_cam(cam):
    """这路摄像头在 go2rtc 里的流名。

    优先用配置里的 `ptz_stream`（建摄像头时就写死了），其次从 rtsp 地址的
    路径段里取 —— `rtsp://127.0.0.1:8555/zwcam_play` -> `zwcam_play`。
    """
    name = (cam.get("ptz_stream") or "").strip()
    if name:
        return name
    path = (cam.get("url") or "").split("//", 1)[-1]
    path = path.split("?", 1)[-1]
    if "/" in path:
        return path.rsplit("/", 1)[-1].strip()
    return ""


def _producer_bytes(streams, name):
    """这路流"活 producer"的 bytes_recv 之和；没有活 producer 返回 None。

    必须区分占位记录：配置里写过的流，即使设备断了电，go2rtc 的流表里仍会留
    一条只有 url 的记录（实测：
    `{"producers":[{"url":"xiaomi://***@192.168.1.23?did=…"}]}`）。
    它不代表有会话，只说明"这条流被注册过"。运行期字段（bytes_recv /
    format_name）一个都没有，就算没会话 —— 否则断电的摄像头会被误判成
    "会话在但不出数据"，看门狗就会白忙一场去修一条根本不需要修的流。
    """
    entry = streams.get(name)
    if not isinstance(entry, dict):
        return None
    total = None
    for p in entry.get("producers") or []:
        if not isinstance(p, dict):
            continue
        if p.get("bytes_recv") is None and p.get("format_name") is None:
            continue        # 占位记录
        try:
            total = (total or 0) + int(p.get("bytes_recv") or 0)
        except (TypeError, ValueError):
            total = total or 0
    return total


def _rate(name, total):
    """距上次采样这个流的字节增速（B/s）。首次采样返回 None。"""
    now = time.monotonic()
    with _lock:
        prev = _rx.get(name)
        _rx[name] = (now, total)
    if prev is None or total is None:
        return None
    dt = now - prev[0]
    if dt <= 0:
        return None
    return max(0.0, (total - prev[1]) / dt)


def _lan_ip_of(cam, streams):
    """摄像头在局域网里的地址。

    主用 go2rtc 配置：源形如 `xiaomi://999999999:cn@192.168.1.23?did=988888888`，
    按 did 精确归属。设备离线时流表里什么都没有，而配置里一直有，所以流表
    （producer 的 `remote_addr`）只作为配置读不到时的兜底。
    """
    did = str(cam.get("did") or "")
    if not did:
        return ""
    ip = _ip_from_config(did)
    if _looks_like_ip(ip):
        return ip
    for _name, entry in (streams or {}).items():
        if not isinstance(entry, dict):
            continue
        for p in (entry.get("producers") or []):
            fmt = str(p.get("format_name") or "").lower()
            cand = str(p.get("remote_addr") or "").split(":")[0]
            if ("xiaomi" in fmt or "miss" in fmt) and _looks_like_ip(cand):
                return cand
    return ""


def _looks_like_ip(text):
    return bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}$", text or ""))


def _ip_from_config(did):
    """从 go2rtc 配置反查 did 对应的局域网地址（@ 后面那串就是摄像头地址）。

    结果缓存 5 分钟：这个查询每轮健康检查都要做，而容器里看不到宿主机的
    yaml，只能走 /api/config 打一次 HTTP。
    """
    now = time.time()
    with _lock:
        if now - _ip_cache["at"] < 300 and did in _ip_cache["map"]:
            return _ip_cache["map"].get(did, "")
    found = {}
    try:
        import nw_ptz as P
        for name, src in (P.g2r_stream_sources() or {}).items():
            src = str(src)
            if ("did=%s" % did) not in src:
                continue
            m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", src.split("@")[-1])
            if m and not src.startswith("ffmpeg:"):
                found[did] = m.group(1)
    except Exception:
        pass
    with _lock:
        if found:
            _ip_cache["map"].update(found)
            _ip_cache["at"] = now
        return found.get(did, _ip_cache["map"].get(did, ""))


def _arp_state(ip):
    """宿主机 ARP 表里这个地址的状态。

    返回 True（有 MAC，设备在网）/ False（条目缺失或 MAC 全零，多半断电）
    / None（读不到表，未知）。
    """
    if not ip:
        return None
    try:
        with open("/proc/net/arp", "r") as f:
            for line in f.readlines()[1:]:
                cols = line.split()
                if not cols or cols[0] != ip:
                    continue
                mac = cols[3] if len(cols) > 3 else "00:00:00:00:00:00"
                return mac != "00:00:00:00:00:00"
    except OSError:
        return None
    return False


def _cloud_online(did):
    """小米云的在线表（True/False/None=未知）。没登录或取不到就返回 None。"""
    if not did:
        return None
    try:
        import nw_xiaomi as X
        m = X.cloud_online_map() or {}
    except Exception:
        return None
    if did in m:
        return bool(m[did])
    return None


def _newest_segment_age(cam_id):
    """最新录像分段距今多少秒；目录里没有分段时返回 None（=从没录成过）。"""
    base = os.path.join(C.recordings_root(), str(cam_id))
    if not os.path.isdir(base):
        return None
    newest = 0.0
    try:
        for n in os.listdir(base):
            if not n.endswith(".mp4"):
                continue
            try:
                newest = max(newest, os.path.getmtime(os.path.join(base, n)))
            except OSError:
                continue
    except OSError:
        return None
    if not newest:
        return None
    return max(0.0, time.time() - newest)


def _grace_seconds(cfg):
    """分段之间允许的最长"没有新文件"间隔：单段时长 × 2.5，下限 3 分钟。

    ffmpeg 的 segment muxer 是写完上一段才落盘（文件名取段起始时间），
    所以"没有新文件"本身正常，只有超过 1.5 个分段时长才算断。
    """
    try:
        seg = int((cfg.get("storage") or {}).get("segment_minutes") or 5)
    except (TypeError, ValueError):
        seg = 5
    return max(180.0, seg * 60.0 * 2.5)


# ---------------------------------------------------------------- 判定
def storage_state(force=False):
    """录像目录的真实状态：在不在、能不能写、还剩多少空间。

    这一类比"某台摄像头掉线"更严重：它不针对一台，它针对全部，而且除了磁盘
    不再长文件以外没有任何表征（进程在、页面 200、直播照样能看）。掉线是运维
    问题，这个是丢数据问题，所以它单独判、单独报，并且能让 healthcheck 变红。

    快满的盘也算不健康：ffmpeg 写失败时是**丢掉那一段**继续跑，页面上一切正常。
    """
    now = time.time()
    with _lock:
        cached = _storage["data"]
        if cached is not None and not force and now - _storage["at"] < _STORAGE_TTL:
            return dict(cached)
    cfg = C.load()
    st = cfg.get("storage") or {}
    root = C.recordings_root(cfg)
    ok, err = C.probe_writable(root)
    free_gb = total_gb = None
    try:
        # shutil.disk_usage 而不是 os.statvfs：后者在 Windows 上根本没有，
        # 而 fpk/本地调试那一条路也要能报出容量
        probe_path = root if os.path.isdir(root) else (os.path.dirname(root) or ".")
        du = shutil.disk_usage(probe_path)
        total_gb = round(du.total / (1024.0 ** 3), 1)
        free_gb = round(du.free / (1024.0 ** 3), 1)
    except Exception as e:
        err = err or ("读容量失败：%s" % str(e)[:120])
    budget = 0.0
    try:
        budget = float(st.get("budget_gb") or 0)
    except (TypeError, ValueError):
        budget = 0.0
    if ok and free_gb is not None:
        # 剩余空间低于单段时长的量级就写不出下一段了；给 2GB 的地板值，
        # 再按容量比例留余量（大盘 5% 也比 2GB 大）
        floor = max(2.0, min(20.0, (total_gb or 0) * 0.05))
        if free_gb < floor:
            ok = False
            err = "录像盘只剩 %.1f GB（低于 %.1f GB 安全线），分段会写失败" % (free_gb, floor)
    out = {"dir": root, "ok": ok, "error": err if not ok else "",
           "free_gb": free_gb, "total_gb": total_gb, "budget_gb": budget}
    with _lock:
        prev = _storage["data"]
        _storage["data"] = out
        _storage["at"] = now
    # 第一次判定时按"本来是好的"算：开机就是坏的目录同样要报，
    # 否则"基线未知"会变成"永远不报"。
    _notify_storage(prev["ok"] if prev else True, ok, out)
    return dict(out)


def invalidate_storage():
    """改过 storage.dir 后调：下一轮判定立刻重新探，别拿旧目录的结论糊弄。"""
    with _lock:
        _storage["at"] = 0.0


def _notify_storage(prev_ok, now_ok, st):
    """目录从能写变不能写（或反过来）时报一次；一直坏着不刷屏。"""
    if prev_ok is None or prev_ok == now_ok:
        return
    try:
        import nw_notify as N
        if not now_ok:
            N.notify_fault("录像存储", st.get("error") or "录像目录不可写")
        else:
            N.notify("【HomeEye】录像存储已恢复",
                     "录像目录 %s 重新可写。\n时间：%s"
                     % (st.get("dir"), time.strftime("%Y-%m-%d %H:%M:%S")),
                     key="recover:storage", cooldown=0)
    except Exception:
        pass


def _probe(cam, streams, cfg):
    cid = str(cam.get("id"))
    did = str(cam.get("did") or "")
    st = cfg.get("storage") or {}
    mode = st.get("record_mode") or "continuous"
    recording = bool(cam.get("record", True)) and mode != "off" \
        and bool(cam.get("enabled", True))
    grace = _grace_seconds(cfg)
    url = cam.get("url") or ""
    is_rtsp = url.lower().startswith("rtsp")
    stream = _stream_of_cam(cam)

    base = {
        "id": cid, "name": cam.get("name") or cid, "did": did,
        "stream": stream, "recording": recording,
        "segment_age": None, "bytes_rate": None, "cam_ip": "",
        "net": None, "cloud": None, "session": None,
        "live": False,
    }

    if not is_rtsp:
        # 小米 SDK 直连那一路：按 hub 自己的状态判
        try:
            import nw_xiaomi as X
            s = (X.get_hub().status() or {}).get(cid) or {}
            frames = int(s.get("frames") or 0)
            base["bytes_rate"] = None
            base["live"] = bool(s.get("alive")) and frames > 0
            base["session"] = bool(s.get("alive"))
            if not s.get("alive"):
                base["state"] = "broken"
                base["reason"] = s.get("error") or "SDK 取流进程未运行"
            elif not frames:
                base["state"] = "broken"
                base["reason"] = "SDK 会话在但没有出帧"
            else:
                base["state"] = "ok"
                base["reason"] = ""
        except Exception as e:
            base["state"] = "unknown"
            base["reason"] = str(e)[:160]
        return base

    age = _newest_segment_age(cid) if recording else None
    total = _producer_bytes(streams, stream) if stream else None
    rate = _rate(stream, total) if stream else None
    ip = _lan_ip_of(cam, streams)
    cloud = _cloud_online(did)

    base.update({"segment_age": age, "bytes_rate": rate, "cam_ip": ip,
                 "cloud": cloud, "net": _arp_state(ip) if ip else None,
                 "session": total is not None})

    # flowing：go2rtc 里有 producer 且字节在涨（首轮没有基线时，"有会话"先当作在流）
    flowing = (total is not None) and (rate is None or rate > 0)
    fresh = age is not None and age <= grace

    if recording and fresh:
        base["state"], base["reason"] = "ok", ""
        base["live"] = True
        return base
    if not recording and flowing:
        base["state"], base["reason"], base["live"] = "ok", "", True
        return base
    if not recording and total is None:
        base["state"], base["reason"] = "idle", "不录像且无人观看，无从判断"
        return base

    # 到这里 = 数据面确实不通。用离线证据决定是 offline 还是 broken。
    offline_evidence = (base["net"] is False) or (cloud is False)
    if offline_evidence:
        base["state"] = "offline"
        base["reason"] = "设备离线（%s）" % (
            "ARP 无响应，多半断电/断网" if base["net"] is False
            else "小米云显示离线")
        return base
    if total is None:
        base["state"] = "broken"
        base["reason"] = "设备在网但 go2rtc 没有活跃的取流会话（源未启动或被挤掉）"
        return base
    if recording and age is None:
        base["state"] = "broken"
        base["reason"] = "有取流会话但一段录像都没落盘（检查录像目录权限与磁盘）"
        return base
    base["state"] = "broken"
    if flowing:
        # 画面在流、磁盘不涨 —— 问题在录像这一侧，不是摄像头
        base["reason"] = ("画面正常但 %d 分钟没有新录像（录像进程或目录权限问题）"
                          % int((age or 0) / 60))
    else:
        base["reason"] = "取流会话卡死：producer 在但长时间不出数据"
    return base


# ---------------------------------------------------------------- 对外接口
def refresh(force=False):
    """重新判定所有摄像头；60 秒内复用上次结果（除非 force）。

    探链路里最慢的是小米云（没有超时参数，网络差时能卡好几秒），所以已有线程
    在探时后来者直接复用旧结果，不叠第二趟 —— 容器 healthcheck 只有 8 秒额度，
    并发请求把探测堆起来会把它拖超时。
    """
    now = time.time()
    with _lock:
        fresh = now - _state["at"] < 60 and _state["cams"]
        if fresh and not force:
            return dict(_state["cams"])
        if fresh and _state["probing"]:
            return dict(_state["cams"])
        _state["probing"] = True
    try:
        out = _probe_all()
    finally:
        with _lock:
            _state["probing"] = False
    return out


def _probe_all():
    cfg = C.load()
    cams = [c for c in (cfg.get("cameras") or []) if c.get("enabled", True)]
    streams = _g2r_streams()
    out = {}
    for cam in cams:
        cid = str(cam.get("id"))
        try:
            out[cid] = _probe(cam, streams, cfg)
        except Exception as e:
            out[cid] = {"id": cid, "name": cam.get("name") or cid,
                        "state": "broken", "reason": str(e)[:160],
                        "recording": False, "segment_age": None,
                        "bytes_rate": None, "stream": "", "cam_ip": "",
                        "net": None, "cloud": None, "session": None,
                        "live": False}
    now = time.time()
    with _lock:
        prev = dict(_state["prev"])
        _state["cams"] = out
        _state["prev"] = {k: v["state"] for k, v in out.items()}
        _state["at"] = now
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        for cid, d in out.items():
            if prev.get(cid) and prev[cid] != d["state"]:
                _state["events"].appendleft(
                    {"at": ts, "cam": cid, "name": d.get("name") or cid,
                     "from": prev[cid], "to": d["state"],
                     "reason": d.get("reason", "")})
        for cid in list(prev.keys()):
            if cid not in out:
                _state["events"].appendleft({"at": ts, "cam": cid, "name": cid,
                                             "from": prev[cid], "to": "removed",
                                             "reason": ""})
    _notify_transitions(out, prev)
    return out


def statuses():
    with _lock:
        return dict(_state["cams"])


def events(limit=30):
    with _lock:
        return list(_state["events"])[:max(1, int(limit))]


def offline_ids():
    """当前判定为离线的摄像头 id。"""
    with _lock:
        return {cid for cid, d in _state["cams"].items()
                if d.get("state") == "offline"}


def recording_allowed(cam_id):
    """这路摄像头现在值不值得起录像进程。

    判定离线就不起 —— 设备自己回来了下一轮 reconcile（3 秒）自然恢复，
    所以宁可晚几秒，也别在断电期间每 3 秒白起一个 ffmpeg。
    """
    return str(cam_id) not in offline_ids()


def summary(force=False):
    """给 /api/health 与容器 healthcheck 用的整体结论。

    `healthy` 只被两类事影响：某路链路 `broken`，以及录像目录写不进去。
    主卧每晚断电（offline）不在其列 —— 它不该让容器变 unhealthy，否则
    healthcheck 一红，重启策略会把整个 HomeEye 拖着重启。
    但"盘满了 / 目录挂了"不一样：那是全量丢录像，必须让 healthcheck 变红。
    """
    try:
        refresh(force=force)
    except Exception:
        pass
    with _lock:
        cams = dict(_state["cams"])
        at = _state["at"]
    broken = sorted(cid for cid, d in cams.items() if d.get("state") == "broken")
    offline = sorted(cid for cid, d in cams.items() if d.get("state") == "offline")
    ok = sorted(cid for cid, d in cams.items() if d.get("state") == "ok")
    idle = sorted(cid for cid, d in cams.items()
                  if d.get("state") in ("idle", "unknown"))
    try:
        storage = storage_state(force=force)
    except Exception as e:
        storage = {"dir": "", "ok": True, "error": "", "probe_failed": str(e)[:120]}
    reasons = []
    if broken:
        reasons.append("链路异常: %s" % "、".join(broken))
    if not storage.get("ok", True):
        reasons.append(storage.get("error") or "录像目录不可写")
    return {
        "healthy": not broken and storage.get("ok", True),
        "status": "broken" if (broken or not storage.get("ok", True))
                  else ("degraded" if offline else "ok"),
        "reason": "；".join(reasons),
        "counts": {"ok": len(ok), "offline": len(offline),
                   "broken": len(broken), "idle": len(idle)},
        "ok": ok, "offline": offline, "broken": broken, "idle": idle,
        "cams": cams,
        "storage": storage,
        "monitored": len(cams),
        "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at)) if at else "",
        "events": events(20),
    }


# ---------------------------------------------------------------- 告警分级
def _name(cid):
    cam = C.get_camera(cid) or {}
    return cam.get("name") or cid


def _notify_transitions(out, prev):
    """只在「进入 broken」时发邮件；offline 只记流水。

    主卧摄像头每晚被拔插头是预期事件，为它发邮件会把真正的问题淹掉。
    """
    for cid, d in out.items():
        old = prev.get(cid)
        st = d.get("state")
        if not old or old == st:
            continue
        try:
            import nw_notify as N
            if st == "broken":
                N.notify_fault(d.get("name") or _name(cid),
                               d.get("reason") or "链路异常")
            elif st == "ok" and old == "broken":
                N.notify("【HomeEye】%s 链路已恢复" % (d.get("name") or _name(cid)),
                         "摄像头「%s」链路恢复正常。\n时间：%s"
                         % (d.get("name") or _name(cid),
                            time.strftime("%Y-%m-%d %H:%M:%S")),
                         key="recover:%s" % cid, cooldown=0)
        except Exception:
            pass


# ---------------------------------------------------------------- 守护线程
def _loop(interval):
    while True:
        with _lock:
            _state["beat"] = time.time()
        try:
            refresh()
        except Exception:
            pass
        time.sleep(interval)


def start(interval=30.0):
    """启动后台巡检；重复调用只会启动一次。"""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _spawn(max(10.0, interval))


def _spawn(interval):
    """真正的起线程（调用方持锁）。"""
    global _thread
    _thread = threading.Thread(target=_loop, args=(interval,), daemon=True,
                               name="he-health")
    _state["loops"] += 1
    _thread.start()


def ensure_loop(stale_seconds=180.0, interval=30.0):
    """看门狗用：巡检线程没了、或者卡住不转了，就补一个。

    线程卡死时 `is_alive()` 一样是真的，所以判据必须是心跳而不是存活标志。
    最可能卡住的地方是小米云（SDK 没有超时参数）—— 一旦卡在里面，健康判定
    就永久停在旧结论上，而这恰恰是最需要它的时候。
    旧线程是 daemon，不用管它：`_state["probing"]` 只挡"结果还新鲜"时的重复探测。
    """
    with _lock:
        alive = _thread is not None and _thread.is_alive()
        beat = _state["beat"]
        age = time.time() - beat if beat else None
        if alive and (age is None or age <= stale_seconds):
            return False
        _spawn(max(10.0, interval))
        return True


def heartbeat():
    with _lock:
        return _state["beat"], _state["at"], _state["loops"]


def invalidate_ip_cache(did=None):
    """改过 go2rtc 的源之后必须调：否则最长 5 分钟里还在用旧地址判漂移。"""
    with _lock:
        if did:
            _ip_cache["map"].pop(str(did), None)
        else:
            _ip_cache["map"].clear()
            _ip_cache["at"] = 0.0


def broken_ids():
    """当前判定为 broken 的摄像头 id（看门狗的修复清单）。"""
    with _lock:
        return sorted(cid for cid, d in _state["cams"].items()
                      if d.get("state") == "broken")

