# -*- coding: utf-8 -*-
"""HomeEye · 看门狗：管心跳，也管自愈

复盘里 P0-1 指的是三件事，这一层把它们放在一起做：

1. **看门狗**：录像循环和巡检循环一旦停转是**静默丢录像** —— 页面照样开、
   `/api/state` 照样 200，只是磁盘上不再长文件。判据用心跳而不是线程存活：
   卡死在小米云调用里（SDK 没有超时参数）的线程 `is_alive()` 一样是真的。

2. **退避**：断电期间不再白起 ffmpeg（1.1.0 已有，见 `nw_record._backoff`），
   这一层补上"修复动作"自己的退避 —— 同一台摄像头两次自动修复之间至少隔
   `repair_cooldown`。否则一条真坏掉的链路会被无限重试，正是当年把 CPU 和
   日志刷爆的那种行为。

3. **IP 漂移自愈**：go2rtc 的小米源里硬绑了写配置那一刻的局域网地址
   （`xiaomi://<账号>:cn@192.168.1.23?did=…`），而 tutk 直接对这个地址发 UDP
   （go2rtc `pkg/tutk/conn.go:13`）。路由器重新分配地址、或者主卧那台每晚被
   拔插头白天再插上换了 IP —— 源就永久失败，而且永远不会自己好。
   米家云知道设备现在的真实地址（`GET /api/xiaomi?id=&region=` 返回的
   `localip`）：云侧地址 ≠ 源里地址，就把源换成云侧给的完整 URL。

不动健康判定本身：断电仍然算 **offline**（只记流水、不发邮件、容器照样
healthy）。这一层只处理"设备可达但链路不通"的 broken，以及漂移这件事。

为什么敢在生产上改 go2rtc 的配置
---------------------------------
只走 `PUT /api/streams?name=&src=`：它等价于内存里换掉这一条流 + 磁盘上只改
这一个键（`New(name, src)` 然后 `app.PatchConfig(["streams", name], src)`），
不重启进程、不碰别的流、不碰 `xiaomi:` 的 passToken 段。
（`POST /api/config` 是整份覆盖且**不重载**，要生效只能 `/api/restart`，
那会同时打断所有摄像头的 P2P 会话 —— 所以不用它。）
写之前记下全部流名与账号段，写完立刻复核，复核不过就把旧源写回去。
这一条是拿 go2rtc 1.9.15 实测出来的行为，不是读文档推的。
"""
import collections
import re
import sys
import threading
import time

import nw_config as C

_lock = threading.RLock()
_thread = None
_state = {
    "beat": 0.0,
    "loops": 0,
    "actions": collections.deque(maxlen=100),   # 自愈流水（/api/health 里可见）
    "stats": {"supervise": 0, "restart_threads": 0, "repairs": 0,
              "drift_fixed": 0, "reverted": 0},
    "next": {},          # cam_id -> 最早下次允许修复的时刻
    "stuck": {},         # cam_id -> 修了但没恢复的连续次数
    "cloud": {"at": 0.0, "map": {}},   # 云侧设备表缓存：一轮里几台共用一次调用
}
_drift_seen = {}         # did -> 上次查过云侧地址的时刻
_rx_seen = {}            # 流名 -> (时刻, bytes_recv 累计)，用来算增量

# 源地址在 ARP 表里还完整时不必打云侧接口；离线摄像头也别每 5 分钟问一次云
_CLOUD_TTL = 300.0
_OFFLINE_DRIFT_FACTOR = 3
_IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


# ---------------------------------------------------------------- 配置
def _wd(cfg=None):
    """watchdog 配置，带边界钳制：间隔太短会把云台会话名额挤爆。"""
    cfg = cfg or C.load()
    w = dict(cfg.get("watchdog") or {})

    def num(key, default, lo, hi=None):
        try:
            v = float(w.get(key) or default)
        except (TypeError, ValueError):
            v = float(default)
        return max(lo, v) if hi is None else min(hi, max(lo, v))

    return {
        "enabled": bool(w.get("enabled", True)),
        "interval": num("interval", 60, 15, 3600),
        "stale": num("stale_seconds", 180, 90, 7200),
        "repair": bool(w.get("repair", True)),
        "drift": bool(w.get("drift", True)),
        "cooldown": num("repair_cooldown", 900, 60, 86400),
        "drift_interval": num("drift_check_interval", 300, 120, 7200),
        "max_attempts": int(num("max_attempts", 3, 1, 10)),
    }


def normalize(section, base=None):
    """用户提交的 watchdog 设置钳到安全范围后才落盘。

    只在读的时候钳是不够的：设置页回显的是磁盘上那一份。写了 interval=3、
    页面显示 3、实际跑 15，就是复盘里说的"设置项不诚实"。所以下限在这里就
    落实，存进去的等于跑起来的。上限也要钳：手滑写成 99999999 不等于关掉看门狗。
    """
    merged = dict(base or {})
    merged.update(section or {})
    w = _wd({"watchdog": merged})
    merged.update({
        "enabled": w["enabled"],
        "interval": w["interval"],
        "stale_seconds": w["stale"],
        "repair": w["repair"],
        "drift": w["drift"],
        "repair_cooldown": w["cooldown"],
        "drift_check_interval": w["drift_interval"],
        "max_attempts": w["max_attempts"],
    })
    return merged


def _ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _log(kind, cam, detail, ok=True):
    with _lock:
        _state["actions"].appendleft(
            {"at": _ts(), "kind": kind, "cam": str(cam or ""),
             "detail": str(detail or "")[:300], "ok": bool(ok)})
    try:
        sys.stderr.write("[watchdog] %s %s %s\n"
                         % (kind, cam or "-", str(detail or "")[:300]))
        sys.stderr.flush()
    except Exception:
        pass


# ---------------------------------------------------------------- 判据
def _src_ip(src):
    """小米源地址里那个硬绑的局域网地址（@ 后面、? 前面那段）。"""
    tail = (src or "").split("//", 1)[-1]
    tail = tail.split("@", 1)[-1] if "@" in tail else tail
    m = _IP_RE.search(tail.split("?", 1)[0])
    return m.group(1) if m else ""


def _streams_table():
    try:
        import nw_ptz as P
        raw = P._g2r_call("/api/streams?json", timeout=6)
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _live_producers(entry):
    """这路流里"真的跑起来的" producer。

    配置写过的流即使设备断了电，go2rtc 的流表里仍会留一条只有 url 的占位记录
    （实测 `{"producers":[{"url":"xiaomi://***@192.168.1.23?did=…"}]}`）。
    它只说明"这条流被注册过"，不代表有会话，所以运行期字段一个都没有就不算。
    """
    out = []
    for p in (entry or {}).get("producers") or []:
        if isinstance(p, dict) and (p.get("bytes_recv") is not None
                                    or p.get("format_name") is not None):
            out.append(p)
    return out


def _flowing(streams, name):
    """这条流现在有没有在出数据（字节在涨）。

    首轮没有基线时无从比较，先按"有活 producer 就是在流"处理 —— 宁可这一轮
    不动它，也不要凭一次采样就把正常工作的会话拆掉。
    """
    prods = _live_producers((streams or {}).get(name))
    if not prods:
        return False
    total = sum(int(p.get("bytes_recv") or 0) for p in prods)
    now = time.monotonic()
    with _lock:
        prev = _rx_seen.get(name)
        _rx_seen[name] = (now, total)
    if prev is None or now - prev[0] <= 0:
        return True
    return total > prev[1]


def _cloud_devices(w):
    """云侧设备表 `{did: {ip, name, url}}`；缓存 5 分钟，取不到返回空表。

    账号 ID 取自 go2rtc 自己的 `xiaomi:` 段：HomeEye 存的是 go2rtc 的 API
    凭据，不一定存米家凭据，而能改流就一定能用它的账号查地址。
    """
    import nw_ptz as P
    now = time.time()
    with _lock:
        if _state["cloud"]["map"] and now - _state["cloud"]["at"] < _CLOUD_TTL:
            return dict(_state["cloud"]["map"])
    out = {}
    try:
        region = (C.load().get("ptz") or {}).get("region") or "cn"
        for user in (P.xiaomi_accounts() or []):
            got = P.xiaomi_cloud_devices(user, region) or {}
            out.update(got)
    except Exception:
        out = {}
    if out:
        with _lock:
            _state["cloud"]["at"] = now
            _state["cloud"]["map"] = dict(out)
    return out


def _forget_cloud():
    with _lock:
        _state["cloud"]["at"] = 0.0


def _alive_at(ip):
    """ARP 表里这个地址还有完整 MAC = 这个地址仍然有设备在网。"""
    import nw_health as H
    return H._arp_state(ip) is True


# ---------------------------------------------------------------- 修复
def _safe_patch(base, new_src, old_src, kind, cam):
    """改一条流的源，并复核"别的东西没被带坏"；带坏了就把旧源写回去。

    这条路径唯一的破坏面是 go2rtc 的配置文件。它一旦少了 `xiaomi:` 段，
    所有摄像头都取不到流；所以复核不是"锦上添花"，是敢自动动手的前提。
    """
    import nw_ptz as P
    before = P.g2r_stream_sources() or {}
    accounts_before = P.xiaomi_accounts() or []
    try:
        P.patch_stream_src(base, new_src)
    except Exception as e:
        _log(kind, cam, "改 %s 的源失败：%s" % (base, e), ok=False)
        return False
    after = P.g2r_stream_sources() or {}
    lost = [n for n in before if n not in after]
    lost_accounts = bool(accounts_before) and not (P.xiaomi_accounts() or [])
    if not lost and not lost_accounts:
        return True
    reverted = False
    if old_src:
        try:
            P.patch_stream_src(base, old_src)
            reverted = True
        except Exception:
            pass
    with _lock:
        _state["stats"]["reverted"] += 1
    _forget_cloud()
    what = "、".join(lost[:6]) if lost else "xiaomi 账号段"
    _log(kind, cam, "写回后配置少了 %s，已%s" % (what, "回退" if reverted else "尝试回退"),
         ok=False)
    return False


def _cooldown(cid, w):
    now = time.time()
    with _lock:
        if now < _state["next"].get(str(cid), 0.0):
            return False
        _state["next"][str(cid)] = now + w["cooldown"]
    return True


def _cooldown_left(cid):
    with _lock:
        return max(0.0, _state["next"].get(str(cid), 0.0) - time.time())


def _mark_recovered(cid):
    with _lock:
        _state["stuck"].pop(str(cid), None)
        _state["next"].pop(str(cid), None)


def _restart_recorder(cid):
    """让录像进程断开重连：它手里那条连接属于被换掉的旧 stream。"""
    try:
        import nw_api as API
        rec = getattr(API, "_rec", None)
        if rec is not None:
            rec.restart(cid)
    except Exception:
        pass


def repair(cam, reason="", w=None, streams=None):
    """对这路摄像头做一次"不重启 go2rtc"的链路重建。

    返回 (动作, 详情)，动作 ∈ drift / rebuild / none。
    """
    import nw_ptz as P
    import nw_health as H
    w = w or _wd()
    cid = str(cam.get("id"))
    did = str(cam.get("did") or "")
    if not did:
        return "none", "该摄像头没有 DID，定位不到 go2rtc 的源"
    hit = P.find_g2r_stream(did)
    if not hit:
        return "none", "go2rtc 配置里没有 did=%s 对应的流" % did
    base = hit.get("base") or hit.get("name")
    old_src = hit.get("url") or ""
    if not base or not old_src:
        return "none", "找不到 %s 这条流的原始源" % cid

    streams = streams if streams is not None else _streams_table()
    if _flowing(streams, base):
        return "none", "%s 正在出数据，不动它" % base

    # 刹车放在动手之前：整条修复链（查云侧 → 改源 → 复核 → 重启录像）都要花
    # 几十秒，没有这道闸门时 broken 每轮都会重来一次，正是当年刷爆 CPU 的那种行为。
    left = _cooldown_left(cid)
    if left > 0:
        return "none", "距上次修复不足，还有 %d 秒（设置里可调）" % int(left)
    _cooldown(cid, w)

    # 1) 地址漂了：换成云侧给的完整 URL（model 等参数一起带上）
    if w["drift"]:
        cloud = _cloud_devices(w).get(did) or {}
        cip, curl = cloud.get("ip") or "", cloud.get("url") or ""
        sip = _src_ip(old_src)
        if cip and sip and cip != sip:
            new_src = curl or re.sub(re.escape(sip), cip, old_src, count=1)
            if _safe_patch(base, new_src, old_src, "drift", cid):
                with _lock:
                    _state["stats"]["drift_fixed"] += 1
                    _state["stats"]["repairs"] += 1
                H.invalidate_ip_cache(did)
                _forget_cloud()      # 手里这份云侧表已经"用掉了"，下一轮重新查
                _restart_recorder(cid)
                _log("drift", cid, "%s 源地址 %s → %s（云侧给的地址）"
                     % (base, sip, cip))
                return "drift", "%s 源地址从 %s 改成 %s" % (base, sip, cip)
            return "none", "漂移修复没通过配置复核，已回退"

    # 2) 地址没错，就是 producer 僵了或根本没起来：用同一条源重建会话
    if not w["repair"]:
        return "none", "自动修复已关闭（设置页可开）"
    if _safe_patch(base, old_src, old_src, "rebuild", cid):
        with _lock:
            _state["stats"]["repairs"] += 1
        _restart_recorder(cid)
        _log("rebuild", cid, "%s 源未变，重建取流会话（原因：%s）"
             % (base, reason or "链路不通"))
        return "rebuild", "已重建 %s 的取流会话" % base
    return "none", "重建没通过配置复核，已回退"


# ---------------------------------------------------------------- 漂移扫描
def _scan_drift(w, streams):
    """离线摄像头也扫：主卧那台插上电换了 IP，这时修好就不用等broken超时。

    省钱的做法：先用 ARP 筛 —— 源里的地址仍然有设备在网，就不值得为此打一次
    小米云；查不到才去问云侧。真离线（断电中）的摄像头单独降频到 3 倍间隔。
    """
    import nw_ptz as P
    import nw_health as H
    now = time.time()
    sources = P.g2r_stream_sources() or {}
    for cam in (C.load().get("cameras") or []):
        if not cam.get("enabled", True):
            continue
        did = str(cam.get("did") or "")
        cid = str(cam.get("id"))
        if not did:
            continue
        st = (H.statuses().get(cid) or {})
        offline = st.get("state") == "offline"
        gap = w["drift_interval"] * (_OFFLINE_DRIFT_FACTOR if offline else 1)
        with _lock:
            if now - _drift_seen.get(did, 0.0) < gap:
                continue
            _drift_seen[did] = now
        if not w["drift"]:
            continue
        play = H._stream_of_cam(cam)
        raw_name = None
        for name, src in sources.items():
            if ("did=%s" % did) in str(src) and not str(src).startswith("ffmpeg:"):
                raw_name = name
                break
        sip = _src_ip(sources.get(raw_name or "") or "") or H._lan_ip_of(cam, streams)
        if not sip:
            continue
        if not offline and _alive_at(sip) and _flowing(streams, play):
            continue        # 地址在网、画面在流 —— 没有漂移
        _forget_cloud()
        cloud = _cloud_devices(w).get(did) or {}
        cip = cloud.get("ip") or ""
        if not cip or cip == sip:
            continue
        # 冷却闸门在 repair() 动手前，这里不重复消耗名额
        what, detail = repair(cam, "IP 漂移", w, streams=streams)
        if what == "none":
            _log("drift", cid, "检测到 %s → %s 但没修：%s" % (sip, cip, detail),
                 ok=False)


# ---------------------------------------------------------------- 主循环
def _supervise(w):
    """心脏：巡检与录像循环还转不转。返回被补起的线程名。"""
    import nw_api as API
    import nw_health as H
    fixed = []
    try:
        if H.ensure_loop(stale_seconds=w["stale"]):
            fixed.append("health")
    except Exception as e:
        _log("supervise", "", "巡检心跳检查失败：%s" % e, ok=False)
    try:
        rec = getattr(API, "_rec", None)
        if rec is not None:
            fixed += ["record:" + k for k in rec.ensure_loops(stale_seconds=w["stale"])]
    except Exception as e:
        _log("supervise", "", "录像心跳检查失败：%s" % e, ok=False)
    return fixed


def _notify_stuck(cam, st, times):
    """自动修复证明无效才惊动人。

    进入 broken 时 `nw_health` 已经发过一封；这一封的语义不同：
    前一封是"坏了"，这一封是"自动修了 N 次没修好，要人工看"。
    """
    try:
        import nw_notify as N
        name = cam.get("name") or cam.get("id") or ""
        N.notify("【HomeEye】%s 自动修复无效" % name,
                 "摄像头「%s」已自动重建过 %d 次链路，仍然不通。\n"
                 "最后一次判定：%s\n时间：%s\n"
                 "建议按顺序检查：设备是否被别的客户端挤掉 P2P 会话、"
                 "go2rtc 日志、米家账号的 passToken 是否过期。"
                 % (name, times, st.get("reason") or "-", _ts()),
                 key="stuck:%s" % cam.get("id"), cooldown=0)
    except Exception:
        pass


def _tick():
    w = _wd()
    import nw_health as H
    with _lock:
        _state["beat"] = time.time()
        _state["stats"]["supervise"] += 1

    fixed = _supervise(w)
    if fixed:
        with _lock:
            _state["stats"]["restart_threads"] += len(fixed)
        _log("thread", "", "心跳超时，已补起线程：%s" % "、".join(fixed))

    streams = _streams_table()

    # 链路恢复的：清掉冷却与"修不好"计数，下一轮真坏了还能立刻修
    for cid, d in (H.statuses() or {}).items():
        if d.get("state") != "ok":
            continue
        with _lock:
            had = bool(_state["stuck"].get(cid) or _state["next"].get(cid))
        if had:
            _log("recovered", cid, "链路恢复，清除修复冷却")
            _mark_recovered(cid)

    _scan_drift(w, streams)

    for cid in H.broken_ids():
        st = (H.statuses().get(cid) or {})
        if _cooldown_left(cid) > 0:
            continue
        with _lock:
            n = int(_state["stuck"].get(cid, 0))
        if n >= w["max_attempts"]:
            continue        # 已经证明修不好，不再白刷尝试
        cam = C.get_camera(cid) or {"id": cid}
        what, detail = repair(cam, st.get("reason") or "", w, streams=streams)
        with _lock:
            _state["stuck"][cid] = n + 1
            escalated = (_state["stuck"][cid] == w["max_attempts"])
        if what == "none":
            _log("giveup", cid, detail, ok=False)
            if escalated:
                _notify_stuck(cam, st, n + 1)


def _loop():
    while True:
        w = _wd()
        if w["enabled"]:
            try:
                _tick()
            except Exception as e:
                _log("tick", "", "本轮异常：%s" % e, ok=False)
        time.sleep(w["interval"])


def start():
    """启动看门狗；重复调用只会启动一次。"""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _state["loops"] += 1
        _thread = threading.Thread(target=_loop, daemon=True, name="he-watchdog")
        _thread.start()


def brief():
    """给 /api/health 用的精简结论：看门狗是不是在转圈、修过几次、上次修了啥。"""
    s = state()
    acts = s.get("actions") or []
    return {
        "enabled": s["enabled"], "repair": s["repair"], "drift": s["drift"],
        "beat_age": s["beat_age"], "stats": s["stats"], "hearts": s["hearts"],
        "stuck": s["stuck"], "cooldown_left": s["cooldown_left"],
        "last": acts[0] if acts else None,
    }


def state():
    """给 /api/health 与 /api/watchdog 用的自检结论。"""
    import nw_api as API
    import nw_health as H
    w = _wd()
    now = time.time()
    hb, hat, hloops = H.heartbeat()
    rec = getattr(API, "_rec", None)
    rb = rec.heartbeat() if rec is not None else {}

    def age(ts):
        return round(now - ts, 1) if ts else None

    with _lock:
        actions = list(_state["actions"])[:20]
        stats = dict(_state["stats"])
        left = {k: int(v - now) for k, v in _state["next"].items() if v > now}
        stuck = dict(_state["stuck"])
        beat = _state["beat"]
    return {
        "enabled": w["enabled"], "repair": w["repair"], "drift": w["drift"],
        "interval": w["interval"], "cooldown": w["cooldown"],
        "max_attempts": w["max_attempts"], "loops": _state["loops"],
        "beat_age": age(beat),
        "stats": stats,
        "hearts": {
            "watchdog_age": age(beat),
            "health_age": age(hb),
            "health_result_age": age(hat),
            "health_loops": hloops,
            "record_watch_age": age(rb.get("watch")),
            "record_clean_age": age(rb.get("clean")),
        },
        "cooldown_left": left, "stuck": stuck, "actions": actions,
    }
