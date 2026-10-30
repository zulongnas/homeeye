# -*- coding: utf-8 -*-
"""HomeEye · 通知中心（纯标准库：smtplib + urllib）

设计要点：
  * 只用标准库 —— NAS 自带 Python 不需要装任何东西。
  * 多渠道：SMTP 邮件 / 企业微信群机器人 / 钉钉群机器人 / Bark / 通用 Webhook。
    配了哪个渠道就走哪个，互不影响；单个渠道失败不影响其他渠道。
  * 所有失败都吞掉并返回 (False, 原因)，绝不因为发通知失败影响录像/取流主流程。
  * 支持免打扰时段（可跨零点）、布防时段（撤防时不发移动类通知）与故障重发冷却。
  * 每日日报：汇总昨天的本地事件与存储占用，在指定小时发出。
"""
import base64
import hashlib
import hmac
import json
import os
import smtplib
import threading
import time
import urllib.request
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import nw_config as C

# 同一类通知的最短重发间隔（key -> 上次发送时间戳）
_last_sent = {}
_send_lock = threading.Lock()

_dig_lock = threading.Lock()
_dig_last_date = ""
_dig_stop = threading.Event()
_dig_started = False


# ------------------------------------------------------------------ 时间窗

def _hhmm_to_min(s, fallback=None):
    try:
        h, m = [int(x) for x in str(s).split(":")[:2]]
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except Exception:
        pass
    return fallback


def _in_window(start, end, now=None):
    """单个 HH:MM-HH:MM 窗口判断，支持跨零点（22:00-08:00）。"""
    a = _hhmm_to_min(start)
    b = _hhmm_to_min(end)
    if a is None or b is None:
        return False
    t = now or time.localtime()
    cur = t.tm_hour * 60 + t.tm_min
    if a == b:
        return False
    if a < b:
        return a <= cur < b
    return cur >= a or cur < b


def _in_dnd(start, end, now=None):
    return _in_window(start or "22:00", end or "08:00", now)


def in_windows(ranges, now=None):
    """布防时段列表："22:00-08:00,12:00-14:00"，空串= 全天不布防。"""
    t = now or time.localtime()
    for part in str(ranges or "").split(","):
        part = part.strip()
        if not part:
            continue
        bits = part.replace("～", "-").replace("~", "-").split("-")
        if len(bits) == 2 and _in_window(bits[0], bits[1], t):
            return True
    return False


def armed(cfg=None):
    """布防状态：always= 全天布防；sched= 按时段；off= 撤防（只影响移动类通知，
    故障告警不受影响）。"""
    cfg = cfg or C.load()
    nt = cfg.get("notify") or {}
    mode = (nt.get("arming_mode") or "always").strip()
    if mode == "off":
        return False
    if mode == "sched":
        return in_windows(nt.get("arming_ranges"))
    return True


# ------------------------------------------------------------------ 渠道

def _post_json(url, obj, timeout=10):
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read(4096).decode("utf-8", "replace")
        return r.status, body


def _send_smtp(nt, subject, text):
    host = (nt.get("smtp_host") or "").strip()
    port = int(nt.get("smtp_port") or 465)
    user = (nt.get("smtp_user") or "").strip()
    pwd = nt.get("smtp_pass") or ""
    to_addrs = [a.strip() for a in (nt.get("to_addrs") or "").split(",") if a.strip()]
    from_addr = (nt.get("from_addr") or user).strip()
    if not host:
        return False, "未配置 SMTP 服务器"
    if not to_addrs:
        return False, "未配置收件人"
    if not user or not pwd:
        return False, "未配置 SMTP 账号或密码"
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("HomeEye", "utf-8")), from_addr or user))
    msg["To"] = ", ".join(to_addrs)
    if nt.get("smtp_ssl", True):
        srv = smtplib.SMTP_SSL(host, port, timeout=15)
    else:
        srv = smtplib.SMTP(host, port, timeout=15)
        try:
            srv.starttls()
        except Exception:
            pass
    try:
        srv.login(user, pwd)
        srv.sendmail(from_addr or user, to_addrs, msg.as_string())
    finally:
        try:
            srv.quit()
        except Exception:
            pass
    return True, ""


def _send_wecom(url, subject, text):
    st, body = _post_json(url, {"msgtype": "text",
                                "text": {"content": "%s\n%s" % (subject, text)}})
    if st != 200 or '"errcode":0' not in body.replace(" ", ""):
        return False, "HTTP %s %s" % (st, body[:120])
    return True, ""


def _send_ding(url, secret, subject, text):
    if secret:
        ts = str(int(time.time() * 1000))
        sign = base64.b64encode(hmac.new(
            secret.encode(), ("%s\n%s" % (ts, secret)).encode(),
            hashlib.sha256).digest()).decode()
        url = url + ("&" if "?" in url else "?") + "timestamp=" + ts + "&sign=" + urllib.request.quote(sign, safe="")
    st, body = _post_json(url, {"msgtype": "text",
                                "text": {"content": "%s\n%s" % (subject, text)}})
    if st != 200 or '"errcode":0' not in body.replace(" ", ""):
        return False, "HTTP %s %s" % (st, body[:120])
    return True, ""


def _send_bark(url, subject, text):
    # bark 支持直接 POST JSON 到根地址；URL 形如 https://api.day.app/<key>
    st, body = _post_json(url, {"title": subject, "body": text})
    if st != 200:
        return False, "HTTP %s %s" % (st, body[:120])
    return True, ""


def _send_webhook(url, subject, text):
    st, _ = _post_json(url, {"title": subject, "text": text,
                             "source": "homeeye",
                             "time": time.strftime("%Y-%m-%d %H:%M:%S")})
    if st < 200 or st >= 300:
        return False, "HTTP %s" % st
    return True, ""


def channels_status(nt=None):
    """哪些渠道配好了（给设置页与测试发送用）。"""
    nt = nt or (C.load().get("notify") or {})
    return {
        "smtp": bool((nt.get("smtp_host") or "").strip()
                     and (nt.get("to_addrs") or "").strip()),
        "wecom": bool((nt.get("wecom_webhook") or "").strip()),
        "ding": bool((nt.get("ding_webhook") or "").strip()),
        "bark": bool((nt.get("bark_url") or "").strip()),
        "webhook": bool((nt.get("webhook_url") or "").strip()),
    }


def send(subject, text):
    """向所有已配置的渠道发一条通知，不走开关/免打扰/冷却（给「测试发送」用）。

    返回 (是否有任一渠道成功, 各渠道结果说明)。
    """
    nt = C.load().get("notify") or {}
    results = []

    st = channels_status(nt)
    if st["smtp"]:
        try:
            ok, err = _send_smtp(nt, subject, text)
            results.append(("邮件", ok, err))
        except Exception as e:
            results.append(("邮件", False, str(e)))
    if st["wecom"]:
        try:
            ok, err = _send_wecom((nt.get("wecom_webhook") or "").strip(), subject, text)
            results.append(("企业微信", ok, err))
        except Exception as e:
            results.append(("企业微信", False, str(e)))
    if st["ding"]:
        try:
            ok, err = _send_ding((nt.get("ding_webhook") or "").strip(),
                                 (nt.get("ding_secret") or "").strip(), subject, text)
            results.append(("钉钉", ok, err))
        except Exception as e:
            results.append(("钉钉", False, str(e)))
    if st["bark"]:
        try:
            ok, err = _send_bark((nt.get("bark_url") or "").strip(), subject, text)
            results.append(("Bark", ok, err))
        except Exception as e:
            results.append(("Bark", False, str(e)))
    if st["webhook"]:
        try:
            ok, err = _send_webhook((nt.get("webhook_url") or "").strip(), subject, text)
            results.append(("Webhook", ok, err))
        except Exception as e:
            results.append(("Webhook", False, str(e)))

    if not results:
        return False, "没有配置任何通知渠道"
    detail = "；".join("%s%s%s" % (name, "OK" if ok else "失败",
                                   ("（%s）" % err) if (err and not ok) else "")
                       for name, ok, err in results)
    return any(ok for _n, ok, _e in results), detail


def notify(subject, text, key="", cooldown=None, need_arm=False):
    """业务通知入口：检查总开关、布防（可选）、免打扰、冷却时间。

    key       —— 用于冷却归类的键，例如 "fault:c1"
    cooldown  —— 覆盖配置里的 fault_cooldown_s
    need_arm  —— True 时撤防/不在布防时段就不发（移动类通知用；故障告警别开）
    """
    cfg = C.load()
    nt = cfg.get("notify") or {}
    if not nt.get("enabled"):
        return False, "通知未启用"
    if need_arm and not armed(cfg):
        return False, "撤防中"

    # 免打扰：到点就静默丢弃（不排队，避免早上补发一堆）
    if nt.get("dnd_enabled") and _in_dnd(nt.get("dnd_start"), nt.get("dnd_end")):
        return False, "免打扰时段"

    cd = int(cooldown if cooldown is not None else (nt.get("fault_cooldown_s") or 300))
    now = time.time()
    if key:
        with _send_lock:
            last = _last_sent.get(key, 0)
            if cd > 0 and (now - last) < cd:
                return False, "冷却中"
            _last_sent[key] = now

    ok, err = send(subject, text)
    if not ok and key:
        # 发送失败要撤销冷却标记，否则故障恢复后要等满冷却才会再试
        with _send_lock:
            _last_sent.pop(key, None)
    return ok, err


def notify_fault(cam_name, detail):
    """摄像头故障通知（不受布防影响——设备挂了要随时知道）。"""
    cfg = C.load()
    nt = cfg.get("notify") or {}
    if not nt.get("on_fault", True):
        return False, "未开启故障通知"
    return notify(
        "【HomeEye】摄像头异常：%s" % cam_name,
        "摄像头「%s」取流/录像异常。\n\n详情：%s\n时间：%s\n\n"
        "如果这是断电或断网导致的，恢复后无需处理；持续异常请检查摄像头电源与网络。"
        % (cam_name, detail, time.strftime("%Y-%m-%d %H:%M:%S")),
        key="fault:%s" % cam_name,
    )


def notify_motion(cam_name, when=None):
    """移动侦测通知（受布防影响）。"""
    cfg = C.load()
    nt = cfg.get("notify") or {}
    if not nt.get("on_motion", True):
        return False, "未开启移动侦测通知"
    return notify(
        "【HomeEye】%s 检测到移动" % cam_name,
        "摄像头「%s」在 %s 检测到移动。\n\n可登录 HomeEye 查看时间轴与录像。"
        % (cam_name, when or time.strftime("%Y-%m-%d %H:%M:%S")),
        key="motion:%s" % cam_name,
        cooldown=60,
        need_arm=True,
    )


def notify_local_event(cam_name, count, first_ts):
    """本地事件侦测扫出事件后的通知（受布防影响，与设备侧移动通知共用冷却键）。"""
    cfg = C.load()
    nt = cfg.get("notify") or {}
    if not nt.get("on_motion", True):
        return False, "未开启移动侦测通知"
    when = time.strftime("%H:%M:%S", time.localtime(first_ts))
    return notify(
        "【HomeEye】%s 检测到 %d 段动静" % (cam_name, count),
        "摄像头「%s」的录像分段里侦测到 %d 段有动静（第一段 %s）。\n\n"
        "可登录 HomeEye 在时间轴上直接跳到这些时段。"
        % (cam_name, count, when),
        key="motion:%s" % cam_name,
        cooldown=60,
        need_arm=True,
    )


# ------------------------------------------------------------------ 每日日报

def _events_summary(root, day_compact, cams_by_id):
    """读 events/<cid>/<day>.jsonl，按摄像头汇总事件数与首个/末个时间。"""
    out = []
    if not os.path.isdir(os.path.join(root, "events")):
        return out
    for cid in sorted(os.listdir(os.path.join(root, "events"))):
        p = os.path.join(root, "events", cid, "%s.jsonl" % day_compact)
        if not os.path.isfile(p):
            continue
        tss = []
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                        tss.append((float(e.get("t0") or 0),
                                    float(e.get("t1") or e.get("t0") or 0)))
                    except Exception:
                        continue
        except OSError:
            continue
        if not tss:
            continue
        cam = cams_by_id.get(cid) or {}
        out.append({
            "cam": cam.get("name") or cid,
            "n": len(tss),
            "first": min(t[0] for t in tss),
            "last": max(t[1] for t in tss),
        })
    return out


def _storage_summary(root):
    total = 0
    files = 0
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            if not n.endswith(".mp4"):
                continue
            try:
                total += os.path.getsize(os.path.join(dirpath, n))
                files += 1
            except OSError:
                pass
    return files, total


def _build_digest(day=None):
    """昨天的日报正文。day=None 表示昨天。"""
    root = C.recordings_root()
    cfg = C.load()
    cams_by_id = {c.get("id"): c for c in (cfg.get("cameras") or [])
                  if isinstance(c, dict) and c.get("id")}
    d = day or time.localtime(time.time() - 86400)
    compact = time.strftime("%Y%m%d", d)
    iso = time.strftime("%Y-%m-%d", d)
    rows = _events_summary(root, compact, cams_by_id)
    nfiles, nbytes = _storage_summary(root)
    lines = ["HomeEye 日报 · %s" % iso, ""]
    if rows:
        for r in rows:
            lines.append("· %s：%d 段动静（%s ~ %s）" % (
                r["cam"], r["n"],
                time.strftime("%H:%M:%S", time.localtime(r["first"])),
                time.strftime("%H:%M:%S", time.localtime(r["last"]))))
    else:
        lines.append("· 昨天没有侦测到动静")
    lines.append("")
    lines.append("· 录像占用：%d 个分段，约 %.1f GB" % (nfiles, nbytes / 1024.0 ** 3))
    st = cfg.get("storage") or {}
    if float(st.get("budget_gb") or 0) > 0:
        lines.append("· 容量上限：%.0f GB，保留 %s 天" % (
            float(st["budget_gb"]),
            ("%d" % int(st.get("retention_days") or 0)) if st.get("retention_days") else "不限"))
    lines.append("")
    lines.append("可登录 HomeEye 查看时间轴与事件卡片。")
    return "\n".join(lines)


def send_digest(day=None):
    """发日报（绕过布防与免打扰，走总开关）。返回 (ok, detail)。"""
    cfg = C.load()
    if not (cfg.get("notify") or {}).get("enabled"):
        return False, "通知未启用"
    body = _build_digest(day)
    return send("【HomeEye】每日日报", body)


def _digest_loop():
    global _dig_last_date
    while not _dig_stop.wait(300):
        try:
            cfg = C.load()
            nt = cfg.get("notify") or {}
            if not (nt.get("enabled") and nt.get("digest_enabled")):
                continue
            hour = int(nt.get("digest_hour") or 20)
            now = time.localtime()
            today = time.strftime("%Y-%m-%d", now)
            if now.tm_hour < hour:
                continue
            with _dig_lock:
                if _dig_last_date == today:
                    continue
                _dig_last_date = today
            send_digest()
        except Exception:
            pass


def start_digest():
    global _dig_started
    if _dig_started:
        return
    _dig_started = True
    threading.Thread(target=_digest_loop, name="he-digest", daemon=True).start()
