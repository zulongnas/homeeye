# -*- coding: utf-8 -*-
"""HomeEye · 邮件通知（纯标准库 smtplib）

设计要点：
  * 只用标准库 —— NAS 自带 Python 不需要装任何东西。
  * 所有失败都吞掉并返回 (False, 原因)，绝不因为发信失败影响录像/取流主流程。
  * 支持免打扰时段（可跨零点，例如 22:00-08:00）与故障重发冷却。
"""
import smtplib
import threading
import time
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import nw_config as C

# 同一类通知的最短重发间隔（key -> 上次发送时间戳）
_last_sent = {}
_send_lock = threading.Lock()


def _in_dnd(start, end, now=None):
    """判断当前是否处于免打扰时段。支持跨零点，如 22:00 ~ 08:00。"""
    try:
        sh, sm = [int(x) for x in (start or "22:00").split(":")[:2]]
        eh, em = [int(x) for x in (end or "08:00").split(":")[:2]]
    except Exception:
        return False
    t = now or time.localtime()
    cur = t.tm_hour * 60 + t.tm_min
    a = sh * 60 + sm
    b = eh * 60 + em
    if a == b:
        return False
    if a < b:
        return a <= cur < b
    # 跨零点：22:00-08:00 表示 cur >= 22:00 或 cur < 08:00
    return cur >= a or cur < b


def send(subject, text):
    """直接发一封信，不管开关和免打扰（给「测试发送」用）。返回 (ok, err)。"""
    cfg = C.load()
    nt = cfg.get("notify") or {}
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

    try:
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
    except Exception as e:
        return False, str(e)


def notify(subject, text, key="", cooldown=None):
    """业务通知入口：会检查总开关、通知时机、免打扰、冷却时间。

    key       —— 用于冷却归类的键，例如 "fault:c1"
    cooldown  —— 覆盖配置里的 fault_cooldown_s
    """
    cfg = C.load()
    nt = cfg.get("notify") or {}
    if not nt.get("enabled"):
        return False, "通知未启用"

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
    """摄像头故障通知。"""
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
    """移动侦测通知。"""
    cfg = C.load()
    nt = cfg.get("notify") or {}
    if not nt.get("on_motion", True):
        return False, "未开启移动侦测通知"
    return notify(
        "【HomeEye】%s 检测到移动" % cam_name,
        "摄像头「%s」在 %s 检测到移动。\n\n可登录 HomeEye 查看实时画面与录像。"
        % (cam_name, when or time.strftime("%Y-%m-%d %H:%M:%S")),
        key="motion:%s" % cam_name,
        cooldown=60,
    )
