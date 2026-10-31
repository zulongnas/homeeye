# -*- coding: utf-8 -*-
"""HomeEye · 冷层归档（rclone 上云）

只做一件事：把**已经过了保留期、但还没删**的旧分段复制到远端，复制并校验
成功后才允许删本地。rclone 不在时如实说"未安装"，绝不假装归档成功 ——
用户以为数据在云上、实际只在本地才是真正危险的状态。

配置（设置页「存储可靠性」）：
    archive.enabled      总开关
    archive.remote       rclone 远端，形如 remote:homeeye
    archive.older_days   只归档 N 天前的分段
    archive.delete_local 校验成功后删本地

校验方式：`rclone lsjson <dest> --stat` 拿远端大小与本地比对。大小一致才算
成功（不做哈希：几 GB 的文件每次全读一遍，NAS 上不值当）。
"""
import json
import os
import shutil
import subprocess
import threading
import time

import nw_config as C
import nw_record as R

_lock = threading.RLock()
MAX_FILES = 300              # 单次最多搬这么多，避免一个按钮把 NAS 占满几天
_state = {"running": False, "at": 0.0, "totals": {}, "error": "",
          "progress": "", "last": None}
STATE_PATH = lambda: os.path.join(C.PKGVAR, "archive_state.json")


def binary():
    p = (os.environ.get("HE_RCLONE") or "").strip()
    if p and os.path.isfile(p):
        return p
    for cand in ("/usr/local/bin/rclone", "/usr/bin/rclone",
                 "/vol1/@appcenter/rclone/bin/rclone"):
        if os.path.isfile(cand):
            return cand
    return shutil.which("rclone") or ""


def _load_state():
    if _state["last"] is not None:
        return
    try:
        with open(STATE_PATH(), "r", encoding="utf-8") as f:
            _state["last"] = json.load(f)
            _state["at"] = float((_state["last"] or {}).get("at") or 0)
    except Exception:
        _state["last"] = None


def _save_state(data):
    try:
        os.makedirs(C.PKGVAR, exist_ok=True)
        tmp = STATE_PATH() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, STATE_PATH())
    except OSError:
        pass


def _locked(root):
    """被事件引用的分段不搬（事件时间轴还要点得开）。"""
    out = set()
    ev = os.path.join(root, "events")
    if not os.path.isdir(ev):
        return out
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
                            out.add(str(fn).replace("\\", "/"))
            except OSError:
                continue
    return out


def _candidates(root, older_days, cam_id=None, locked=None):
    cutoff = time.time() - older_days * 86400
    locked = locked or set()
    out = []
    try:
        cids = os.listdir(root)
    except OSError:
        return out
    for cid in cids:
        if cid == "events" or (cam_id and cid != cam_id):
            continue
        base = os.path.join(root, cid)
        if not os.path.isdir(base):
            continue
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for n in names:
            if not n.endswith(".mp4") or not R.SEG_RE.match(n):
                continue
            rel = "%s/%s" % (cid, n)
            if rel in locked:
                continue
            p = os.path.join(base, n)
            try:
                mt = os.path.getmtime(p)
                sz = os.path.getsize(p)
            except OSError:
                continue
            if mt > cutoff or sz <= 0:
                continue
            out.append((mt, cid, n, p, sz))
    out.sort()
    return out[:MAX_FILES]


def _rclone(bin_path, args, timeout=600):
    try:
        p = subprocess.run([bin_path] + args, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout)
        return p.returncode, (p.stdout or b"").decode("utf-8", "replace"), \
            (p.stderr or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return -1, "", "rclone 超时（%d 秒）" % timeout
    except Exception as e:
        return -1, "", str(e)


def _remote_ok(bin_path, remote):
    rc, out, err = _rclone(bin_path, ["listremotes"], timeout=30)
    if rc != 0:
        return False, "rclone 列不出远端：%s" % (err.strip()[-200:] or rc)
    name = remote.split(":", 1)[0].strip()
    if not name:
        return False, "远端格式应为 remote:path"
    remotes = [x.strip().rstrip(":") for x in out.splitlines() if x.strip()]
    if name not in remotes:
        return False, "rclone 里没有名为 %s 的远端（现有：%s）" % (
            name, ", ".join(remotes[:8]) or "无")
    return True, ""


def run(cam=None, older_days=None):
    """启动一次归档（后台）。返回 {"started":bool,"error":...}。"""
    cfg = C.load()
    ar = cfg.get("archive") or {}
    if not ar.get("enabled"):
        return {"started": False, "error": "归档没有启用"}
    remote = str(ar.get("remote") or "").strip()
    if ":" not in remote:
        return {"started": False, "error": "远端格式应为 remote:path"}
    bin_path = binary()
    if not bin_path:
        return {"started": False,
                "error": "这台机器没有 rclone（可装一个或在 Docker 里加装）"}
    ok, why = _remote_ok(bin_path, remote)
    if not ok:
        return {"started": False, "error": why}
    try:
        days = int(older_days or ar.get("older_days") or 30)
    except (TypeError, ValueError):
        days = 30
    days = max(1, min(days, 3650))
    with _lock:
        if _state["running"]:
            return {"started": False, "error": "上一轮归档还没结束"}
        _state["running"] = True
        _state["error"] = ""
        _state["progress"] = "准备中…"

    def _job():
        started = time.time()
        root = C.recordings_root(cfg)
        locked = _locked(root)
        files = _candidates(root, days, (str(cam) if cam else None), locked)
        totals = {"files": len(files), "copied": 0, "bytes": 0,
                  "deleted": 0, "failed": 0, "skipped": 0}
        errors = []
        for idx, (mt, cid, name, p, sz) in enumerate(files, 1):
            if not _state["running"]:
                break
            dest = remote.rstrip("/") + "/" + cid + "/" + name
            with _lock:
                _state["progress"] = "(%d/%d) %s/%s" % (idx, len(files), cid, name)
            rc, _out, err = _rclone(bin_path, [
                "copyto", p, dest, "--stats", "0", "--retries", "2",
                "--low-level-retries", "3", "--timeout", "10m", "--no-traverse",
            ])
            if rc != 0:
                totals["failed"] += 1
                errors.append("%s/%s：%s" % (cid, name, (err or "").strip()[-160:]))
                continue
            # 校验远端大小
            rc2, out2, err2 = _rclone(bin_path, [
                "lsjson", dest, "--stat", "--no-modtime", "--no-mimetype"], timeout=120)
            rsize = -1
            if rc2 == 0:
                try:
                    j = json.loads(out2 or "{}")
                    rsize = int(j.get("Size", -1))
                except Exception:
                    rsize = -1
            if rsize != sz:
                totals["failed"] += 1
                errors.append("%s/%s：远端大小 %s ≠ 本地 %s（%s）" % (
                    cid, name, rsize, sz, (err2 or "").strip()[-100:]))
                continue
            totals["copied"] += 1
            totals["bytes"] += sz
            if ar.get("delete_local"):
                try:
                    os.unlink(p)
                    totals["deleted"] += 1
                except OSError as e:
                    errors.append("%s/%s：删本地失败 %s" % (cid, name, e))
        data = {
            "at": started,
            "at_text": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
            "remote": remote, "older_days": days,
            "delete_local": bool(ar.get("delete_local")),
            "totals": totals, "errors": errors[:50],
            "seconds": round(time.time() - started, 1),
        }
        with _lock:
            _state["running"] = False
            _state["at"] = started
            _state["last"] = data
            _state["progress"] = ""
            _state["totals"] = totals
            _state["error"] = errors[0] if errors and not totals["copied"] else ""
        _save_state(data)
        try:
            import nw_audit as AU
            AU.log("archive", "system", remote=remote,
                   copied=totals["copied"], failed=totals["failed"],
                   deleted=totals["deleted"], bytes=totals["bytes"])
        except Exception:
            pass

    threading.Thread(target=_job, daemon=True, name="he-archive").start()
    return {"started": True}


def status():
    _load_state()
    cfg = C.load()
    ar = cfg.get("archive") or {}
    with _lock:
        st = dict(_state)
    b = binary()
    return {
        "config": {"enabled": bool(ar.get("enabled")),
                   "remote": ar.get("remote") or "",
                   "older_days": ar.get("older_days", 30),
                   "delete_local": bool(ar.get("delete_local"))},
        "binary": b,
        "available": bool(b),
        "running": st.get("running"),
        "progress": st.get("progress") or "",
        "error": st.get("error") or "",
        "at": st.get("at") or 0,
        "last": st.get("last"),
    }


def cancel():
    with _lock:
        _state["running"] = False
    return {"ok": True}


# ---------------------------------------------------------------- 定时归档
_auto_started = False
_auto_last_day = ""


def _auto_loop():
    """每天在 archive.auto_hour 那一小时跑一轮（开了 auto 才跑）。

    按"今天有没有跑过"判重而不是按小时精确对齐：容器可能整晚重启，卡死在
    04:00:00 这一秒的判断上，就会一天都不搬。
    """
    global _auto_last_day
    while True:
        try:
            cfg = C.load()
            ar = cfg.get("archive") or {}
            now = time.localtime()
            want_hour = int(ar.get("auto_hour") or 4)
            today = time.strftime("%Y-%d-%m", now)
            if (ar.get("enabled") and ar.get("auto")
                    and now.tm_hour == want_hour and _auto_last_day != today
                    and not _state["running"]):
                _auto_last_day = today
                run()
        except Exception:
            pass
        time.sleep(300.0)


def start_auto():
    global _auto_started
    if _auto_started:
        return
    _auto_started = True
    threading.Thread(target=_auto_loop, daemon=True, name="he-archive-auto").start()
