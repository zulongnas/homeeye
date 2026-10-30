# -*- coding: utf-8 -*-
"""HomeEye · 设备侧侦测与存储配置面（移动侦测 / 摄像头自身录像 / SD 卡）

为什么是"配置面"而不是"检测引擎"：
  小米摄像头的移动侦测在**设备里**跑，判定结果只进米家 App 的事件流。vendored 的
  miloco SDK 只有取流用的 P2P：既没有 MIoT 属性/事件订阅，也没有历史事件查询
  （plugin/miot/client.py 只有 lan 上下线回调，plugin/miot/camera.py 只有 stream）。
  在 4 核 NAS 上再跑一遍帧差/YOLO 去"发现"设备早就知道的事，既不更准也不划算。
  所以这一面做两件事：把设备上真正生效的开关读出来、改回去；并让人看清 SD 卡的
  录像状态与容量。读不到就明确说读不到，不猜值。

为什么不写死 siid/piid：
  手上两个型号（chuangmi.camera.039a01 / 069a01）恰好都是 siid=2/4/5，那是巧合。
  一律按 MIoT spec 的 URN 类型名定位（urn:miot-spec-v2:property:<name>:...），
  换型号或小米改编号都不会读错格子；这个型号没实现的服务就是不出现。
"""
import re
import threading
import time
from concurrent import futures

# 逻辑键 -> (服务类型名, 属性类型名)。顺序即设置页里的展示顺序。
PROPS = (
    ("enabled",         "motion-detection",          "motion-detection"),
    ("sensitivity",     "motion-detection",          "detection-sensitivity"),
    ("alarm_interval",  "motion-detection",          "alarm-interval"),
    ("start_time",      "motion-detection",          "motion-detection-start-time"),
    ("end_time",        "motion-detection",          "motion-detection-end-time"),
    ("record_mode",     "camera-control",            "recording-mode"),
    ("motion_tracking", "camera-control",            "motion-tracking"),
    ("sd_status",       "memory-card-management",    "status"),
    ("sd_total",        "memory-card-management",    "storage-total-space"),
    ("sd_free",         "memory-card-management",    "storage-free-space"),
    ("sd_used",         "memory-card-management",    "storage-used-space"),
)

# 能写回设备的只有这些；SD 那几项设备只报不写（spec 的 access 里没有 write）。
WRITABLE = ("enabled", "sensitivity", "alarm_interval", "start_time",
            "end_time", "record_mode", "motion_tracking")

LABELS = {
    "enabled": "移动侦测开关",
    "sensitivity": "侦测灵敏度",
    "alarm_interval": "告警间隔",
    "start_time": "生效起始时间",
    "end_time": "生效结束时间",
    "record_mode": "摄像头自身录像",
    "motion_tracking": "移动追踪",
    "sd_status": "SD 卡状态",
    "sd_total": "SD 卡总容量",
    "sd_free": "SD 卡剩余",
    "sd_used": "SD 卡已用",
}

# 只显示用的中文口径。以设备 spec 的 description 为准，这里只在它没给或给的是
# 英文时补一句人话 —— 判定和下发都不依赖它。
HINTS = {
    "enabled": "关掉后设备不再产生侦测事件，米家 App 里也不会有移动提醒。",
    "sensitivity": "设备自己的档位，不是 HomeEye 的 1~100 滑块：这台机器只有低/高两档。",
    "alarm_interval": "两次侦测告警之间的最小间隔，设备用它压消息量。",
    "start_time": "侦测生效时段的起点，超出时段设备自己不判。",
    "end_time": "侦测生效时段的终点。",
    "record_mode": "录在摄像头自己的 SD 卡上。选「仅移动侦测录像」可省卡；"
                   "HomeEye 的录像在 NAS 上，两者各存各的。",
    "motion_tracking": "云台跟随移动目标（部分型号不支持，读不到就是这台没有）。",
    "sd_status": "卡不在/满了，摄像头的自身录像就会停 —— HomeEye 补不了这段空洞。",
    "sd_total": "", "sd_free": "", "sd_used": "",
}

# 设置页分组标题：只在这台设备真的支持该服务时才出现（见 state() 里的 sections）。
SECTION_TITLES = {
    "motion-detection": "移动侦测（设备的判定条件）",
    "camera-control": "摄像头自身（录像与云台跟随）",
    "memory-card-management": "SD 卡（设备自己的存档）",
}

# 这些码值来自实测的 spec value-list（039a01 只到 7，069a01 到 11）。
SD_TEXT = {
    0: "正常", 1: "未插入", 2: "已满", 3: "异常/损坏", 4: "正在格式化",
    5: "已弹出", 6: "正在修复", 7: "正在弹出", 8: "未初始化",
    9: "空间不足", 10: "文件系统错误", 11: "格式不兼容",
}
REC_TEXT = {0: "全部录像", 1: "仅移动侦测录像", 2: "关闭录像"}
SENS_TEXT = {0: "低", 1: "高"}

_UNIT_DIV = {"b": 1024.0 ** 3, "kb": 1024.0 ** 2, "mb": 1024.0, "gb": 1.0,
             "kib": 1024.0 ** 2, "mib": 1024.0, "gib": 1.0}

# spec 的 unit 字段是英文码名，直接拼进中文标签会变成「告警间隔（minutes）」。
# 这里只翻译显示口径，判定与下发都不看它。
_UNIT_ZH = {"minutes": "分钟", "minute": "分钟", "hours": "小时", "hour": "小时",
            "seconds": "秒", "second": "秒", "percentage": "%", "decibels": "分贝"}

READ_TTL = 20.0        # 同一台设备 20 秒内不重复问云（设置页每 8 秒刷一次）
CALL_TIMEOUT = 25.0    # 单次云调用上限：超时就说"读不到"，不让人对着转圈的页面等
SPEC_TIMEOUT = 15.0    # 给 SDK 的 HTTP 会话钉上的超时

_map_lock = threading.Lock()
_MAPS = {}        # spec urn -> {key: meta}
_READ = {}        # did -> (时间戳, 结果)
_POOL = futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="he-motion")


class MotionError(Exception):
    """给用户看的一句人话错误，直接进 API 的 error 字段。"""


def _type_name(urn):
    """``urn:miot-spec-v2:property:on:00000006:chuangmi-069a01:2`` -> ``on``"""
    parts = str(urn or "").split(":")
    return parts[3] if len(parts) > 3 else ""


def _value_list(prop):
    """spec 的取值表 -> [(值, 设备自己的描述)]；没有取值表返回 None。"""
    raw = prop.get("value-list")
    if not isinstance(raw, list):
        return None
    out = []
    for item in raw:
        if not isinstance(item, dict) or item.get("value") is None:
            continue
        out.append((item.get("value"), str(item.get("description") or "")))
    return out or None


def _resolve(client, did):
    """按 spec 把这个设备支持的控制面解析成 {key: meta}，按 urn 缓存。"""
    urn = client.device.get_device_urn(did)
    hit = _MAPS.get(urn)
    if hit is not None:
        return hit
    spec = client.device.get_spec(urn)
    table = {}
    for svc in spec.get("services") or []:
        sname = _type_name(svc.get("type"))
        for prop in svc.get("properties") or []:
            key = next((k for k, s, p in PROPS if s == sname and p == _type_name(prop.get("type"))),
                       None)
            if not key:
                continue
            table[key] = {
                "siid": svc.get("iid"), "piid": prop.get("iid"), "svc": sname,
                "access": prop.get("access") or [],
                "unit": prop.get("unit") or "",
                "range": prop.get("range"),
                "list": _value_list(prop),
                "format": prop.get("format"),
                "desc": str(prop.get("description") or ""),
            }
    with _map_lock:
        _MAPS[urn] = table
    return table


def _client():
    import nw_xiaomi as X
    auth = X.get_auth()
    auth.ensure_client()
    client = auth._client
    if client is None:
        raise MotionError("米家还没授权：到设置页『小米云授权』扫码登录后才能读写设备配置。")
    client._access_token = auth.token
    _pin_timeout(client)
    return client


def _pin_timeout(client, seconds=SPEC_TIMEOUT):
    """给 SDK 的 HTTP 会话补一个超时。

    miloco 的 MijiaApi.api_request 调 requests.post 时不传 timeout
    （sdk/miloco-sdk-main/miloco_sdk/plugin/mijia.py:83-93），云端不响就永久占住
    调用线程。这一面只借它跑几个短请求，所以在这里包一层钉上默认超时 ——
    不改 vendored SDK 的源码。
    """
    http = getattr(client, "_http", None)
    if http is None or getattr(http, "_he_timeout", None):
        return
    orig = http.post

    def post(*a, **kw):
        kw.setdefault("timeout", seconds)
        return orig(*a, **kw)

    http.post = post
    http._he_timeout = seconds


def _call(fn, *args):
    """把同步云调用丢进单 worker 线程，并设硬超时。

    worker 只有一个：既让下发/回读天然串行（同一次改配置不会互相插队），
    又保证卡住的请求只堵自己，不会拖垮取流用的那条单例 loop。
    """
    fut = _POOL.submit(fn, *args)
    try:
        return fut.result(CALL_TIMEOUT)
    except futures.TimeoutError:
        # 已经在跑的请求 stop 不掉（线程不是可中断的），超时后它自己会被
        # _pin_timeout 的 socket 超时收掉；这里只是不再让 HTTP 处理线程陪着等。
        raise MotionError("小米云接口 %d 秒内没有回应（云端或网络在抽），"
                          "稍后点「重新读取」再看。" % int(CALL_TIMEOUT))


def _hms(raw):
    """'22:00' / '22:00:00' / '22' -> 'HH:MM:SS'（设备要三段）。"""
    txt = str(raw or "").strip()
    m = re.match(r"^(\d{1,2}):?(\d{2})?:?(\d{2})?$", txt)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2) or 0)
    ss = int(m.group(3) or 0)
    if hh > 23 or mm > 59 or ss > 59:
        return None
    return "%02d:%02d:%02d" % (hh, mm, ss)


def _in_range(meta, num):
    rng = meta.get("range")
    if not isinstance(rng, list) or len(rng) != 2:
        return num
    lo, hi = rng[0], rng[1]
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        return min(hi, max(lo, num))
    return num


def _coerce(key, meta, raw):
    """提交值 -> 设备能接受的形态。返回 (值, 说明) —— 说明非空表示被纠正过。"""
    if key in ("enabled", "motion_tracking"):
        if isinstance(raw, str):
            val = raw.strip().lower() not in ("", "0", "false", "no", "off")
        else:
            val = bool(raw)
        return val, ""
    if key in ("start_time", "end_time"):
        val = _hms(raw)
        if val is None:
            raise MotionError("%s 时间格式不对：%r（要 HH:MM，例如 22:00）" % (LABELS[key], raw))
        return val, ""
    try:
        num = int(round(float(raw)))
    except (TypeError, ValueError):
        raise MotionError("%s 要填整数：%r" % (LABELS[key], raw))
    opts = meta.get("list")
    if opts:
        allowed = [v for v, _ in opts]
        if num not in allowed:
            # 下拉框本不该出现非法值；手搓请求进来的要挡，并把设备的档位说清楚。
            raise MotionError("%s 只能是 %s 之一（设备自己的档位）" % (
                LABELS[key], "/".join(str(v) for v in allowed)))
        return num, ""
    clamped = _in_range(meta, num)
    note = "" if clamped == num else ("%s 超出设备范围 %s，按 %s 下发" % (
        LABELS[key], meta.get("range"), clamped))
    return clamped, note


def _kind(key, meta):
    if key in ("enabled", "motion_tracking"):
        return "bool"
    if key in ("start_time", "end_time"):
        return "time"
    if meta.get("list"):
        return "choice"
    if key.startswith("sd_"):
        return "size" if key != "sd_status" else "code"
    return "int"


def _unit(key, meta):
    """标签括号里的口径 —— 只影响显示。

    容量格子读数前已经折算成 GB，这里不能再照抄 spec 的原始单位，否则页面会
    出现「115.19 KB」这种自相矛盾的读数。
    """
    if _kind(key, meta) == "size":
        return "GB"
    raw = str(meta.get("unit") or "")
    if not raw:
        return "分钟" if key == "alarm_interval" else ""
    return _UNIT_ZH.get(raw.lower(), raw)


def _text(key, meta, val):
    """码值 -> 中文（带上设备自己的英文描述，便于核对 spec）。"""
    if val is None:
        return ""
    if _kind(key, meta) == "size":
        # 容量没有码值可翻译，原始计数由 values[key] 那行自己显示，
        # 再在这里返回一遍只会变成「120781184 ｜ 115.19 GB」。
        return ""
    zh = {"sensitivity": SENS_TEXT, "record_mode": REC_TEXT,
          "sd_status": SD_TEXT}.get(key, {}).get(val)
    en = dict(meta.get("list") or []).get(val, "") if meta.get("list") else ""
    if zh and en:
        return "%s（%s）" % (zh, en)
    return zh or en or str(val)


def _gb(meta, val):
    """容量按 spec 的 unit 折算成 GB；unit 缺失时按实测口径当 KB 处理。

    069a01 的 128G 卡报 storage-total-space=120781184，只有按 KB 解释才等于 115 GiB，
    所以默认 divisor 走 KB —— 但优先信 spec，别把结论写死在代码里。
    """
    if not isinstance(val, (int, float)):
        return None
    div = _UNIT_DIV.get(str(meta.get("unit") or "kb").lower(), 1024.0 ** 2)
    return round(val / div, 2)


def _schema(key, meta):
    rng = meta.get("range") if isinstance(meta.get("range"), list) else None
    return {
        "label": LABELS.get(key, key),
        "kind": _kind(key, meta),
        "svc": meta.get("svc") or "",
        "siid": meta.get("siid"), "piid": meta.get("piid"),
        "writable": key in WRITABLE and "write" in (meta.get("access") or []),
        "readable": "read" in (meta.get("access") or []) or not meta.get("access"),
        "unit": _unit(key, meta),
        "range": rng,
        "options": [{"v": v, "t": _disp(key, v, en)} for v, en in (meta.get("list") or [])],
        "hint": HINTS.get(key, ""),
        "spec": meta.get("desc") or "",
    }


def _disp(key, val, en):
    zh = {"sensitivity": SENS_TEXT, "record_mode": REC_TEXT,
          "sd_status": SD_TEXT}.get(key, {}).get(val)
    if zh and en:
        return "%s（%s）" % (zh, en)
    return zh or en or str(val)


def _client_of(cam):
    did = str(cam.get("did") or "")
    if not did:
        return None, "这路不是米家设备（没有 did）：设备侧配置只适用于米家摄像头。"
    import nw_xiaomi as X
    ok, msg = X.xiaomi_available()
    if not ok:
        return None, "小米 SDK 不可用：%s" % msg
    return did, ""


def _sections(keys):
    """按服务把已解析到的格子分组（顺序沿用 PROPS，不靠字典顺序碰运气）。"""
    out = []
    for key, svc, _ in PROPS:
        if key not in keys:
            continue
        hit = next((s for s in out if s["svc"] == svc), None)
        if hit is None:
            out.append({"svc": svc,
                        "title": SECTION_TITLES.get(svc, svc),
                        "keys": [key]})
        else:
            hit["keys"].append(key)
    return out


def state(cam, fresh=False):
    """读一台设备的控制面。任何一步失败都在 error/errors 里写明白，不给假数。"""
    cid = str(cam.get("id") or "")
    did, err = _client_of(cam)
    entry = {"cam": cid, "name": cam.get("name") or cid, "did": did or "",
             "online": None, "at": time.time(), "keys": {}, "sections": [],
             "values": {}, "errors": {}, "error": err, "sd": {}}
    if err:
        return entry
    stamp = _READ.get(did)
    if stamp and not fresh and (time.time() - stamp[0]) < READ_TTL:
        return stamp[1]

    online = None
    try:
        import nw_xiaomi as X
        online = X.cloud_online_map().get(did)
    except Exception:
        online = None
    entry["online"] = online

    try:
        table = _call(lambda: _resolve(_client(), did))
    except MotionError as e:
        entry["error"] = str(e)
        return entry
    except Exception as e:
        entry["error"] = "解析设备能力失败：%s" % e
        return entry

    for key, _, _ in PROPS:
        meta = table.get(key)
        if not meta:
            continue
        entry["keys"][key] = _schema(key, meta)
    entry["sections"] = _sections(entry["keys"])

    # 离线时不发起属性请求：小米云会挂着等超时，拿到的只是"读不到"，
    # 那不如当场就说读不到，也省下一次往返。
    if online is False:
        entry["error"] = "设备离线：读不到摄像头上的当前配置。下面的格子留空，" \
                         "不拿上次的值冒充现在生效的值。"
        _READ[did] = (time.time(), entry)
        return entry

    readable = [k for k, sch in entry["keys"].items() if sch["readable"]]
    if not readable:
        entry["error"] = "这台设备的 spec 里没有可读的侦测/存储属性。"
        _READ[did] = (time.time(), entry)
        return entry
    params = [{"did": did, "siid": table[k]["siid"], "piid": table[k]["piid"]}
              for k in readable]
    try:
        res = _call(lambda: _client().device.get_props(params))
    except MotionError as e:
        entry["error"] = str(e)
        return entry
    except Exception as e:
        entry["error"] = "读取设备属性失败：%s" % e
        return entry

    by_iid = {(table[k]["siid"], table[k]["piid"]): k for k in readable}
    for item in res or []:
        key = by_iid.get((item.get("siid"), item.get("piid")))
        if not key:
            continue
        code = item.get("code", 0)
        if code not in (0, None):
            entry["errors"][key] = "设备返回 code=%s（离线或该属性此时不可读）" % code
            continue
        val = item.get("value")
        meta = table[key]
        if key in ("sd_total", "sd_free", "sd_used"):
            entry["values"][key] = _gb(meta, val)
            entry["values"][key + "_raw"] = val
        elif key in ("enabled", "motion_tracking") and val is not None:
            entry["values"][key] = bool(val)
        else:
            entry["values"][key] = val
        text = _text(key, meta, val)
        if text:
            entry["values"][key + "_text"] = text
    if not entry["values"]:
        entry["error"] = entry["error"] or "设备一个属性都没回值（可能刚掉线）。"

    _sd_summary(entry, table)
    _READ[did] = (time.time(), entry)
    return entry


def _sd_summary(entry, table):
    """把 SD 那几项收成一句人话，顺带标出「卡没了 / 满了」这种会断录的状态。"""
    sd = {}
    for key in ("sd_status", "sd_total", "sd_free", "sd_used"):
        if key in entry["values"]:
            sd[key] = entry["values"][key]
        if key + "_text" in entry["values"]:
            sd[key + "_text"] = entry["values"][key + "_text"]
    if not sd:
        return
    status = sd.get("sd_status")
    sd["blocking"] = status in (1, 2, 3, 8, 9, 10, 11)
    mode = entry["values"].get("record_mode")
    if mode == 2:
        sd["note"] = "摄像头自身录像被设为「关闭录像」：卡上不会有东西，" \
                     "HomeEye 这边断链的那段也就无从补起。"
    elif status == 1:
        sd["note"] = "没有 SD 卡：摄像头侧不存档，只有 HomeEye 在 NAS 上的录像。"
    elif sd["blocking"]:
        sd["note"] = "SD 卡状态是「%s」：摄像头的自身录像此时会停。" % sd.get("sd_status_text", status)
    else:
        sd["note"] = "摄像头把自身的录像存在 SD 卡上，与 HomeEye 存在 NAS 的录像各管各的；" \
                     "HomeEye 断链期间漏掉的那段只在卡上，不回填到 NAS。"
    entry["sd"] = sd


def list_state(cams, fresh=False):
    """批量读（设置页进来一次就够）。串行：都走同一个账号的同一把云接口。"""
    out = []
    for cam in cams:
        out.append(state(cam, fresh=fresh))
    return out


def invalidate(did=None):
    """改完配置/切了设备后要清的读缓存；不传 did 就全清。"""
    if did is None:
        _READ.clear()
    else:
        _READ.pop(str(did), None)


def apply_changes(cam, changes):
    """下发到设备，并回读校验 —— 只有回读到的值才算改成了。

    逐条 set：批量 set 的返回里只有一条条的 code，出错时能指到具体哪一格；
    而且一台设备可能只接受其中几项（离线/越界），不该把能改的一起陪葬。
    """
    did, err = _client_of(cam)
    result = {"cam": str(cam.get("id") or ""), "applied": [], "notes": [],
              "errors": [], "readback": {}}
    if err:
        result["errors"].append(err)
        return result
    if not isinstance(changes, dict) or not changes:
        result["errors"].append("没有要下发的改动。")
        return result

    try:
        table = _call(lambda: _resolve(_client(), did))
    except MotionError as e:
        result["errors"].append(str(e))
        return result
    except Exception as e:
        result["errors"].append("解析设备能力失败：%s" % e)
        return result

    wanted = []
    for key, raw in changes.items():
        if key not in WRITABLE:
            result["errors"].append("%s 不由 HomeEye 下发（只读或不存在）。" % LABELS.get(key, key))
            continue
        meta = table.get(key)
        if not meta:
            result["errors"].append("这台设备的 spec 里没有「%s」，跳过。" % LABELS.get(key, key))
            continue
        if "write" not in (meta.get("access") or []):
            result["errors"].append("「%s」在设备上是只读的，跳过。" % LABELS[key])
            continue
        try:
            val, note = _coerce(key, meta, raw)
        except MotionError as e:
            result["errors"].append(str(e))
            continue
        if note:
            result["notes"].append(note)
        wanted.append((key, meta, val))

    for key, meta, val in wanted:
        def _set(meta=meta, val=val):
            client = _client()
            return client.device.set_props([{"did": did, "siid": meta["siid"],
                                             "piid": meta["piid"], "value": val}])
        try:
            res = _call(_set)
        except MotionError as e:
            result["errors"].append("%s 下发失败：%s" % (LABELS[key], e))
            continue
        except Exception as e:
            result["errors"].append("%s 下发失败：%s" % (LABELS[key], e))
            continue
        item = (res or [{}])[0]
        code = item.get("code", 0)
        if code not in (0, None):
            result["errors"].append("%s 被设备拒绝（code=%s，多半是此刻离线）" % (
                LABELS[key], code))
            continue
        result["applied"].append({"key": key, "value": val,
                                  "text": _text(key, meta, val)})

    if not result["applied"]:
        return result

    # 回读校验：下发 code=0 只代表云收到了，不代表设备上生效。
    invalidate(did)
    entry = state(cam, fresh=True)
    for item in result["applied"]:
        got = entry["values"].get(item["key"])
        result["readback"][item["key"]] = got
        if got != item["value"]:
            result["notes"].append("%s 回读是 %s，和下发的 %s 不一致 —— 以设备为准。" % (
                LABELS[item["key"]], got, item["value"]))
    result["state"] = entry
    return result
