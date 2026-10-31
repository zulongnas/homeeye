# -*- coding: utf-8 -*-
"""HomeEye · 访问审计（谁在什么时候看了哪段、改了什么）

为什么用 JSONL 而不是数据库：与录像索引同一个理由 —— 标准库直接跑，NAS 上不
用装任何东西，断电最多丢最后一行。一天一个文件，按天读取和清理都简单。

写什么：登录成功/失败、改密、摄像头增删改、设置变更、回放/导出/分享访问、
用户管理、归档与完整性扫描。**不写**口令、passToken、流地址里的密码 —— 调用方
负责脱敏，这里只落已经能公开看的字段。

放哪：<PKGVAR>/audit/YYYYMMDD.jsonl，保留 90 天（超过的在写日志时顺手清）。
"""
import json
import os
import threading
import time

import nw_config as C

RETENTION_DAYS = 90
_RING_MAX = 400
_lock = threading.RLock()
_ring = []                 # 最近若干条，给设置页秒开用（免得每次都读盘）
_last_prune = 0.0


def _dir():
    return os.path.join(C.PKGVAR, "audit")


def _path(day=None):
    day = day or time.strftime("%Y%m%d")
    return os.path.join(_dir(), "%s.jsonl" % day)


def _clean(fields):
    """只留标量，顺手把明显的凭据字段抹掉（宁可少记，不可错记）。"""
    out = {}
    for k, v in (fields or {}).items():
        lk = str(k).lower()
        if any(s in lk for s in ("pass", "token", "secret", "hash", "salt")):
            out[k] = "***"
            continue
        if v is None or v == "":
            continue
        if isinstance(v, (bool, int, float)):
            out[k] = v
        elif isinstance(v, (list, tuple)):
            out[k] = [str(x)[:80] for x in v][:20]
        elif isinstance(v, dict):
            out[k] = {str(a)[:40]: str(b)[:120] for a, b in list(v.items())[:20]}
        else:
            out[k] = str(v)[:400]
    return out


def log(kind, actor="", ip="", **fields):
    """追加一条审计。失败不抛：审计不能把业务请求带崩。"""
    rec = {
        "ts": round(time.time(), 3),
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kind": str(kind)[:40],
        "actor": str(actor or "anonymous")[:80],
        "ip": str(ip or "")[:64],
    }
    rec.update(_clean(fields))
    line = json.dumps(rec, ensure_ascii=False)
    with _lock:
        try:
            os.makedirs(_dir(), exist_ok=True)
            with open(_path(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        _ring.append(rec)
        if len(_ring) > _RING_MAX:
            del _ring[:len(_ring) - _RING_MAX]
        _maybe_prune()
    return rec


def _maybe_prune():
    global _last_prune
    now = time.time()
    if now - _last_prune < 3600:
        return
    _last_prune = now
    cutoff = time.strftime("%Y%m%d", time.localtime(now - RETENTION_DAYS * 86400))
    try:
        for n in os.listdir(_dir()):
            if n.endswith(".jsonl") and n[:8].isdigit() and n[:8] < cutoff:
                try:
                    os.unlink(os.path.join(_dir(), n))
                except OSError:
                    pass
    except OSError:
        pass


def read(days=7, kind=None, actor=None, limit=500):
    """最近若干天的审计（新的在前）。days=0 只读内存环。"""
    limit = max(1, min(int(limit or 500), 5000))
    rows = []
    if days and days > 0:
        with _lock:
            ring = list(_ring)
        # 内存环通常已经覆盖"最近"，但重启后为空；两种来源合并去重
        rows.extend(ring)
        seen = {(r.get("ts"), r.get("kind"), r.get("actor"),
                 r.get("cam"), r.get("file")) for r in rows}
        for i in range(int(days)):
            day = time.strftime("%Y%m%d", time.localtime(time.time() - i * 86400))
            p = _path(day)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            r = json.loads(line)
                        except Exception:
                            continue
                        key = (r.get("ts"), r.get("kind"), r.get("actor"),
                               r.get("cam"), r.get("file"))
                        if key in seen:
                            continue
                        seen.add(key)
                        rows.append(r)
            except OSError:
                continue
    else:
        with _lock:
            rows = list(_ring)
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    if actor:
        rows = [r for r in rows if r.get("actor") == actor]
    rows.sort(key=lambda r: r.get("ts") or 0, reverse=True)
    return rows[:limit]


def kinds():
    """近 7 天出现过的类型，给筛选下拉用。"""
    return sorted({r.get("kind") for r in read(days=7, limit=5000) if r.get("kind")})


def status():
    today = 0
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            for _ in f:
                today += 1
    except OSError:
        pass
    with _lock:
        mem = len(_ring)
    return {"today": today, "in_memory": mem, "retention_days": RETENTION_DAYS}
