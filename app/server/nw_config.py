# -*- coding: utf-8 -*-
"""HomeEye · 路径与配置

设计约束：只用 Python 标准库（NAS 上 /usr/bin/python3 直接用，不需要 pip/venv）。
所有可写数据都落在 TRIM_PKGVAR（/vol1/@appdata/<appname>），
配置文件是单个 JSON，避免引入数据库依赖。
"""
import json
import os
import shutil
import tempfile
import threading

APPNAME = "homeeye"

# fnOS 应用中心会注入这些环境变量；直接跑脚本调试时用兜底路径
APPDEST = os.environ.get("TRIM_APPDEST") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# TRIM_PKGVAR = 飞牛应用中心注入；HE_DATA_DIR = Docker 版指定数据目录
PKGVAR = (os.environ.get("TRIM_PKGVAR")
          or os.environ.get("HE_DATA_DIR")
          or "/tmp/homeeye-var")
PKGETC = os.environ.get("TRIM_PKGETC") or "/tmp/homeeye-etc"

WWW = os.path.join(APPDEST, "www")
CONF = os.path.join(PKGVAR, "config.json")

DEFAULT = {
    "version": 1,
    "storage": {
        "dir": "",                  # 空 = PKGVAR/recordings
        "retention_days": 15,       # 0 = 不自动删除
        "segment_minutes": 5,       # 单段录像时长
        "budget_gb": 0,             # >0 时按总容量上限滚动删除
        "record_mode": "continuous",  # continuous 全时 | off 不录（motion 没实现，见 UNHONORED）
        "record_audio": True,       # 录像是否带音频
        "keep_events_days": 30,     # 事件明细/缩略图保留天数（1.2.0-D 起 nw_events 生效）
        "source": "PLAYED",         # 录哪一路：PLAYED=转码后的流 / RAW=摄像头原始流
    },
    "live": {
        "width": 1280,              # 实时画面转码宽度（0 = 保持原分辨率）
        "fps": 10,                  # 实时画面转码帧率（0 = 保持原帧率）
        "quality": 23,              # x264 CRF（1~31，越小越清晰；越小越吃 CPU/带宽）
        "transcode": "auto",        # auto 只在浏览器解不了时才转（见 nw_live._tmode）
        "idle_stop": 12,            # 无人观看多少秒后停掉取流进程
        "default_quality": "HIGH",  # 小米默认画质 HIGH / LOW
        "autoplay": True,           # 打开页面自动播放
        "audio": True,              # 实时画面是否取音频（关掉可省 CPU/带宽）
    },
    # 事件侦测有两条来源：设备侧（小米等，见 nw_motion，直接读写摄像头）和
    # HomeEye 本地分析（1.2.0-C 起，见 nw_events：对写完的分段做关键帧级
    # 画面差分，给回放页时间轴标事件块）。enabled/sensitivity 只管本地分析。
    "motion": {
        "enabled": False,           # 本地事件侦测总开关（关 = 不扫分段、时间轴无事件块）
        "sensitivity": 50,          # 1~100，越高越灵敏（=像素差阈值越低）
        "detect_persons": True,     # 人形检测（优先，退化到通用运动）—— 占位，见 UNHONORED
        "pre_seconds": 3,           # 事件前预录秒数 —— 占位（record_mode 无事件式）
        "post_seconds": 10,         # 事件后补录秒数 —— 占位
        "min_frames": 3,            # 连续多少帧变化才算事件 —— 占位
        "max_event_seconds": 120,   # 单个事件最长时长 —— 占位
    },
    # 说明：九宫格与铺满方式（旧 1.0.x 的 "ui" 组）不在这里 —— 那是**这台浏览器**
    # 的看片偏好，不是这台摄像头的属性，存服务端会让手机和电脑互相冲掉。
    # 现在它只存 localStorage（nw.grid / nw.fit），服务端不再收也不回显。
    "notify": {
        "enabled": False,           # 通知总开关
        "smtp_host": "",
        "smtp_port": 465,
        "smtp_ssl": True,
        "smtp_user": "",
        "smtp_pass": "",
        "from_addr": "",
        "to_addrs": "",             # 多个用逗号分隔
        "on_motion": True,          # 移动侦测时发通知
        "on_fault": True,           # 摄像头离线/取流失败时发通知
        "fault_cooldown_s": 300,    # 同一故障最短重发间隔
        "dnd_enabled": False,       # 免打扰
        "dnd_start": "22:00",
        "dnd_end": "08:00",
        # 1.2.0-D：多渠道 + 布防撤防 + 日报（配了哪个渠道就走哪个）
        "wecom_webhook": "",        # 企业微信群机器人 Webhook 地址
        "ding_webhook": "",         # 钉钉群机器人 Webhook 地址
        "ding_secret": "",          # 钉钉机器人加签密钥（可空）
        "bark_url": "",             # Bark 推送地址（https://api.day.app/<key>）
        "webhook_url": "",          # 通用 Webhook（POST JSON: title/text/time）
        "arming_mode": "always",    # always 全天布防 | sched 按时段 | off 撤防
        "arming_ranges": "",        # 布防时段："22:00-08:00,12:00-14:00"
        "digest_enabled": False,    # 每日日报
        "digest_hour": 20,          # 日报发送小时（0-23）
    },
    # 看门狗：管心跳（巡检/录像循环停转就补线程），也管自愈（断链重建、IP 漂移）。
    # 冷却与"最多修几次"是这一层的刹车 —— 没有它，一条真坏掉的链路会被无限重试，
    # 正是 1.0.x 断电期间刷出 2300 个 ffmpeg 进程的那种行为。
    "watchdog": {
        "enabled": True,
        "interval": 60,             # 看门狗自己多久转一圈
        "stale_seconds": 180,       # 心跳停了多久算卡死
        "repair": True,             # 允许重建 broken 摄像头的取流会话
        "drift": True,              # 允许按米家云的 localip 纠正源地址
        "repair_cooldown": 900,     # 同一台两次自动修复的最小间隔
        "drift_check_interval": 300,  # 查米家云地址的最小间隔（离线时再 ×3）
        "max_attempts": 3,          # 连修这么多次仍不通 → 交给人，不再刷尝试
    },
    # 登录闸门。密码只存 PBKDF2 派生值（不存明文）；`configured()` 为假时
    # 不拦人，等用户来认领 —— 否则装完打不开页面。
    "auth": {
        "enabled": True,
        "username": "admin",
        "salt": "",
        "pw_hash": "",
        "session_hours": 24,
    },
    # 云台（PTZ）侧车。小米云台走 Mi Home 的 miss 私有命令，需要米家账号的
    # userId + passToken（不是扫码登录拿到的 OAuth token）。装了 go2rtc 的机器
    # 可以在设置页一键从它的配置导入。
    "ptz": {
        "enabled": False,
        "mode": "auto",             # auto | go2rtc | sidecar
        "g2r_addr": "http://127.0.0.1:1984",   # 留空 = 探测常见地址（会白等连接超时）
        "g2r_user": "",             # go2rtc 的 api.username（配了 BasicAuth 才要）
        "g2r_pass": "",             # go2rtc 的 api.password
        "addr": "127.0.0.1:8899",
        "user": "",                 # 米家账号数字 ID
        "token": "",                # passToken，形如 V1:...
        "region": "cn",
        "step_seconds": 0.45,       # 点一下转多久
        "speed": "normal",          # normal 慢速(1-4) | fast 快速(5-11)
        "media_audio": False,       # 打开下行音频：能用音量回采自检喇叭，但更耗流量
        "idle_seconds": 45,        # 闲置多久释放云台会话（摄像头 P2P 上限 3 条）
    },
    "cameras": [],
}

_lock = threading.RLock()
_cache = {"mtime": None, "data": None}


# ---------------------------------------------------------------- 设置项诚实化
# 页面上能写进任意数、真正跑起来的却是另一套数，那就是骗人。所以范围校验放在
# **落盘前**（只在读的时候钳也不行：那样磁盘上留的是野值，页面回显的也是野值，
# 只有判定的那一瞬间是"合理"的）。钳过的值就是跑的值，两边同一个数。
# kind: int / num / bool / text；数值项给 (下限, 上限)，越界取边界并记账。
RULES = {
    "storage": {
        "retention_days": ("int", 0, 3650),      # 0 = 不自动删
        "segment_minutes": ("int", 1, 180),      # 小于 1 分钟会把磁盘 inode 刷爆
        "budget_gb": ("num", 0, 100000),         # 0 = 不限容量
        "record_mode": ("choice", ("continuous", "off")),
        "record_audio": ("bool",),
        "source": ("choice", ("PLAYED", "RAW")),
        "dir": ("text",),
    },
    "live": {
        "width": ("int", 0, 4096),               # 0 = 保持原分辨率
        "fps": ("int", 0, 60),                   # 0 = 保持原帧率
        "quality": ("int", 1, 31),               # x264 CRF
        "transcode": ("tmode",),                 # auto | always | never（兼容老布尔）
        "idle_stop": ("num", 3, 3600),
        "default_quality": ("choice", ("HIGH", "LOW")),
        "autoplay": ("bool",),
        "audio": ("bool",),
    },
    "notify": {
        "enabled": ("bool",),
        "smtp_port": ("int", 1, 65535),
        "smtp_ssl": ("bool",),
        "on_motion": ("bool",),
        "on_fault": ("bool",),
        "fault_cooldown_s": ("num", 30, 86400),
        "dnd_enabled": ("bool",),
        "dnd_start": ("hms",),
        "dnd_end": ("hms",),
        "to_addrs": ("text",),
        # 1.2.0-D 多渠道 + 布防撤防 + 日报
        "wecom_webhook": ("text",),
        "ding_webhook": ("text",),
        "ding_secret": ("text",),
        "bark_url": ("text",),
        "webhook_url": ("text",),
        "arming_mode": ("choice", ("always", "sched", "off")),
        "arming_ranges": ("text",),
        "digest_enabled": ("bool",),
        "digest_hour": ("int", 0, 23),
    },
    # 本地事件侦测（1.2.0-C 起）：事件时间轴上的事件块就是它跑出来的。
    # 剩下的占位键（预录/补录等）见 UNHONORED。
    "motion": {
        "enabled": ("bool",),
        "sensitivity": ("int", 1, 100),
    },
    "auth": {
        "enabled": ("bool",),
        "username": ("text",),
        "session_hours": ("num", 1, 720),
    },
    "ptz": {
        "enabled": ("bool",),
        "mode": ("choice", ("auto", "go2rtc", "sidecar")),
        "g2r_addr": ("text",),
        "addr": ("text",),
        "region": ("choice", ("cn", "sg", "de", "us", "in", "i2")),
        "step_seconds": ("num", 0.1, 5),
        "speed": ("choice", ("normal", "fast")),
        "media_audio": ("bool",),
        "idle_seconds": ("num", 10, 3600),
    },
}

# 页面上放了、但代码里还没有真的消费者 —— 与其让人调了半天没反应，不如直说。
# 值照旧存（将来实现时不用迁移配置），只是设置页要灰掉并写清原因。
UNHONORED = {
    "motion": {
        "keys": ("detect_persons", "pre_seconds", "post_seconds",
                 "min_frames", "max_event_seconds"),
        "why": "本地事件侦测（时间轴上的事件块）已经真的在跑，用的是上面的"
               "「启用」与「灵敏度」两个格子。剩下的预录/补录/最小帧数是给"
               "「事件式录像」准备的，而 record_mode 只有全时/不录两档 —— "
               "全时录像里不存在'事件前后'，这些分层参数没有东西可以套。",
    },
    # storage.keep_events_days 已转正（1.2.0-D）：事件明细与缩略图只保留这么多天；
    # notify.on_motion 已转正：本地事件扫出动静会真的发通知（布防时段内）。
}


def _coerce(kind, spec, value, fallback):
    """把 submitted 的原始值转成能跑的形态。改了什么由 diff_notes 负责说。"""
    if kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() not in ("", "0", "false", "no", "off")
        return bool(value)
    if kind == "tmode":
        # live.transcode 从布尔升级成三档。老配置 / 老前端传来的 true、false 必须
        # 翻成"照旧"的档位（always / never），不然用户只是升了个级，转码行为就
        # 悄悄变了 —— 真要是想省 CPU，得由他自己选「自动」。
        if isinstance(value, bool):
            return "always" if value else "never"
        txt = str(value if value is not None else "").strip().lower()
        if txt in ("1", "on", "yes", "true"):
            return "always"
        if txt in ("0", "off", "no", "false"):
            return "never"
        if txt in ("auto", "always", "never"):
            return txt
        return fallback if fallback in ("auto", "always", "never") else "auto"
    if kind == "choice":
        txt = str(value if value is not None else "").strip()
        low = txt.lower()
        for opt in spec:
            if low == opt.lower():
                return opt
        return fallback if fallback in spec else spec[0]
    if kind == "text":
        return "" if value is None else str(value).strip()
    if kind == "hms":
        txt = str(value or "").strip()
        if len(txt) == 5 and txt[2] == ":" and txt[:2].isdigit() and txt[3:].isdigit():
            hh, mm = int(txt[:2]), int(txt[3:])
            if hh <= 23 and mm <= 59:
                return "%02d:%02d" % (hh, mm)
        return fallback if fallback else "00:00"
    lo, hi = spec
    try:
        num = float(value)
    except (TypeError, ValueError):
        try:
            num = float(fallback)
        except (TypeError, ValueError):
            return value
    num = min(hi, max(lo, num))
    return int(round(num)) if kind == "int" else num


def normalize(group, values, base=None):
    """钳制落盘：返回可以直接写进配置的那一段。

    没在 RULES 里的键原样保留 —— 白名单在 nw_api 里已经把住，钳制这层只负责
    "值能不能真跑"，不负责"能不能出现"，两件事分开才不会互相漏。
    watchdog 一段由 nw_watchdog.normalize 钳（范围定义在运行时读的那一侧，
    不在这里重复一遍，免得两处上下限各自漂移）。
    """
    merged = dict(base or {})
    merged.update(values or {})
    rules = RULES.get(group) or {}
    default = (DEFAULT.get(group) or {})
    for key, rule in rules.items():
        if key not in merged:
            continue
        kind = rule[0]
        # 数值项写成 ("int", 下, 上)，其余写成 ("choice", (...)) / ("bool",)
        spec = rule[1:] if kind in ("int", "num") else \
            (rule[1] if len(rule) > 1 else None)
        merged[key] = _coerce(kind, spec, merged[key], default.get(key))
    return merged


def diff_notes(group, submitted, merged):
    """把人写的和真正存下的差额讲出来。

    钳制动了值却一声不响，用户下次打开页面看到的不是自己填的数，还是会以为
    程序改了他的设置 —— 报出来才是"诚实"的另一半。
    """
    notes = []
    for key, val in (submitted or {}).items():
        if key not in merged:
            continue
        now = merged[key]
        if isinstance(now, bool) or isinstance(val, bool):
            moved = bool(now) != bool(val)
        else:
            try:
                moved = abs(float(now) - float(val)) > 1e-9
            except (TypeError, ValueError):
                moved = str(now) != str(val)
        if moved:
            notes.append("%s.%s：%s → %s" % (group, key, val, now))
    return notes


_PSEUDO_FS = frozenset((
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2",
    "pstore", "securityfs", "debugfs", "tracefs", "configfs", "fusectl",
    "mqueue", "hugetlbfs", "bpf", "autofs", "binfmt_misc", "ramfs",
    "squashfs", "overlay", "overlayfs", "nsfs", "rpc_pipefs", "selinuxfs",
    "efivarfs", "fuse.lxcfs", "fuse.docker-overlayfs",
))
# 容器根盘是 overlay，写满它等于写满宿主 Docker 的存储池；这些路径不给选。
_ROOTISH = ("/", "/boot", "/etc", "/opt", "/srv", "/run", "/snap",
            "/var/lib/docker", "/usr", "/homeeye")


def _is_rootish(path):
    return path in _ROOTISH or any(path.startswith(p + "/") for p in _ROOTISH)


def probe_writable(path):
    """这个目录本身能不能真落文件（写探针再删）。不创建 path：探测一个别人的
    挂载点时顺手建目录，是在用户卷上留垃圾。"""
    try:
        if not os.path.isdir(path):
            return False, "目录不存在"
        probe = os.path.join(path, ".nw-write-test")
        with open(probe, "w") as f:
            f.write("1")
        os.unlink(probe)
        return True, ""
    except Exception as e:
        return False, str(e)[:160]


def ensure_writable(path):
    """要写之前先确认能写（父目录不存在就建）。返回 (可用路径, 错误)。"""
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as e:
        return False, "%s（%s）" % (path, e)
    return probe_writable(path)


def disk_roots():
    """容器里看得见、并且能报出容量的挂载点。设置页的录像目录选择器用它。

    飞牛的 /fs 只是显示层，bind mount 必须走真实 /volN 路径；而容器里能不能看到
    那个路径，取决于 compose 挂了没有 —— 所以这里如实列出"容器看得见"的那些，
    看不见的一概不提（提了也选不了）。可写性原样回报，不藏着：写不进去的卷
    标出来比不列更诚实，运维需要知道"这块盘在，但当前这个容器写不进去"。
    """
    mps = []
    seen = set()
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            for line in f:
                p = line.split()
                if len(p) < 3:
                    continue
                mp, fstype = p[1], p[2]
                opts = p[3] if len(p) > 3 else ""
                if fstype in _PSEUDO_FS or mp in seen:
                    continue
                if any(o == "ro" for o in opts.split(",")):
                    continue
                seen.add(mp)
                mps.append(mp)
    except Exception:
        mps = []
    # 当前在用的录像目录（哪怕只是个 bind 进来的二层路径）也要在表里
    for cand in _ancestors(recordings_root()):
        if cand not in seen:
            seen.add(cand)
            mps.append(cand)
    out = []
    for mp in mps:
        if not os.path.isdir(mp):
            continue
        try:
            du = shutil.disk_usage(mp)
        except Exception:
            continue
        if du.total <= 0:
            continue
        ok, err = probe_writable(mp)
        out.append({"path": mp, "writable": ok, "error": err,
                    "risky": _is_rootish(mp),
                    "total_gb": round(du.total / (1024.0 ** 3), 1),
                    "free_gb": round(du.free / (1024.0 ** 3), 1)})
    out.sort(key=lambda x: (x["risky"], -x["free_gb"], x["path"]))
    return out


def _ancestors(path):
    res = []
    p = os.path.abspath(path or "/")
    while p:
        up = os.path.dirname(p)
        if up == p:            # 根（Linux 的 /、Windows 的 C:\）—— dirname 是自己
            break
        res.append(p)
        p = up
    return res


def settings_meta():
    """给设置页看的"哪些格子现在不顶事"清单（前端据此灰掉并写明原因）。"""
    return {g: {"keys": list(d["keys"]), "why": d["why"]}
            for g, d in UNHONORED.items()}


def recordings_root(cfg=None):
    """录像目录的三级取值：设置页填的 > 部署方指定的 HE_REC_DIR > 数据目录下面。

    HE_REC_DIR 是给 Docker/包安装这类"部署方决定盘在哪"的场景留的：compose 把大卷
    bind 成 /rec 并设 HE_REC_DIR=/rec，用户想换盘只改 .env 里一行宿主机路径，不用
    进容器改配置；而用户在设置页里真填过目录时，页面说的算（env 只是出厂默认，
    不能反过来盖掉用户的选择）。
    """
    cfg = cfg or load()
    d = (cfg.get("storage") or {}).get("dir") or ""
    if d:
        return d
    return os.environ.get("HE_REC_DIR") or os.path.join(PKGVAR, "recordings")


def ensure_dirs():
    for d in (PKGVAR, recordings_root()):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass


def load():
    """读配置；文件被外部改动（mtime 变）才重新解析，其余走内存缓存。"""
    with _lock:
        try:
            st = os.stat(CONF)
            mt = (st.st_mtime, st.st_size)
        except OSError:
            mt = None
        if _cache["data"] is not None and _cache["mtime"] == mt:
            return _cache["data"]
        data = json.loads(json.dumps(DEFAULT))  # deep copy
        user = {}
        if mt:
            try:
                with open(CONF, "r", encoding="utf-8") as f:
                    user = json.load(f)
                for k, v in user.items():
                    if isinstance(v, dict) and isinstance(data.get(k), dict):
                        data[k].update(v)
                    else:
                        data[k] = v
            except Exception:
                user = {}
        # 迁移：1.0.2 之前 live 组没有 transcode 字段，且 quality 语义是 JPEG 质量
        # （默认 7）。这里一次性转成 x264 CRF 语义（默认 23），避免老配置沿用
        # CRF=7 把 NAS 的 CPU 打满。
        old_live = user.get("live") if isinstance(user.get("live"), dict) else None
        if old_live is not None and "transcode" not in old_live:
            lv = data.setdefault("live", {})
            try:
                if int(float(lv.get("quality") or 0)) == 7:
                    lv["quality"] = 23
            except Exception:
                lv["quality"] = 23
            lv["transcode"] = True
        _cache["data"] = data
        _cache["mtime"] = mt
        return data


def save(cfg):
    """原子写：先写同目录临时文件再 rename，避免断电留下半个 JSON。"""
    with _lock:
        ensure_dirs()
        os.makedirs(os.path.dirname(CONF), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CONF), prefix=".cfg-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONF)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        _cache["mtime"] = (os.stat(CONF).st_mtime, os.stat(CONF).st_size)
        _cache["data"] = cfg
        return cfg


def get_camera(cam_id):
    for c in load().get("cameras") or []:
        if str(c.get("id")) == str(cam_id):
            return c
    return None


def new_camera_id(cfg=None):
    cfg = cfg or load()
    used = {str(c.get("id")) for c in cfg.get("cameras") or []}
    i = 1
    while ("c%d" % i) in used:
        i += 1
    return "c%d" % i
