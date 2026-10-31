# -*- coding: utf-8 -*-
"""HomeEye · 片段导出与带有效期的分享链接

导出：把 [from, to) 区间内的分段逐段裁出来重编码成 H.264/AAC，再用 concat
拼成一个 mp4。为什么要重编码而不是 `-c copy`：拷贝只能在关键帧处切，用户要
"14:03:20 那 40 秒"，copy 会给出前后各差一两秒的一大段；隐私遮蔽也必须在
编码前烧进像素（见 nw_privacy）。代价是导出比实时慢，所以放后台任务里跑，
页面轮询状态。

分享：导出完成后生成一个随机 token，写入 shares.json（带到期时刻）。
`/share/<token>` 是不需要登录的极简页面，`/share/<token>/video.mp4` 直接给
那段文件。链接一旦过期或被撤销就 410。这是"给家人看一段"而不是"把账号给
出去"，所以有效期默认 24 小时，最长 30 天，并且每次访问都记审计。

导出与分享目录都在 PKGVAR 下（不动录像盘），超过 7 天的导出文件在每次新建
任务时顺手清掉。
"""
import json
import os
import re
import secrets
import subprocess
import threading
import time

import nw_config as C
import nw_privacy as PV
import nw_record as R

MAX_SPAN_SECONDS = 6 * 3600          # 一次最多导 6 小时，防止把磁盘填满
MAX_ACTIVE = 2
SHARE_MAX_HOURS = 24 * 30
EXPORT_TTL = 7 * 86400

_lock = threading.RLock()
_jobs = {}                            # id -> job dict
_shares = None                        # token -> share dict
_SHARES_PATH = lambda: os.path.join(C.PKGVAR, "shares.json")


def _ffmpeg_bin():
    return os.environ.get("HE_FFMPEG") or "ffmpeg"


def exports_dir():
    d = os.path.join(C.PKGVAR, "exports")
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------- 分享存储
def _load_shares():
    global _shares
    if _shares is not None:
        return _shares
    try:
        with open(_SHARES_PATH(), "r", encoding="utf-8") as f:
            data = json.load(f)
        _shares = data if isinstance(data, dict) else {}
    except Exception:
        _shares = {}
    return _shares


def _save_shares():
    try:
        os.makedirs(C.PKGVAR, exist_ok=True)
        tmp = _SHARES_PATH() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_shares or {}, f, ensure_ascii=False)
        os.replace(tmp, _SHARES_PATH())
    except OSError:
        pass


def _prune_exports():
    """清掉超期的导出文件与对应分享。"""
    cutoff = time.time() - EXPORT_TTL
    base = os.path.join(C.PKGVAR, "exports")
    try:
        for n in os.listdir(base):
            p = os.path.join(base, n)
            if not os.path.isfile(p):
                continue
            try:
                if os.path.getmtime(p) < cutoff:
                    os.unlink(p)
            except OSError:
                pass
    except OSError:
        pass
    with _lock:
        shares = _load_shares()
        dead = [t for t, s in shares.items()
                if float(s.get("exp") or 0) < time.time()
                or not os.path.isfile(str(s.get("file") or ""))]
        for t in dead:
            shares.pop(t, None)
        if dead:
            _save_shares()


# ---------------------------------------------------------------- 导出
def _cam_or_err(cam_id):
    cam = C.get_camera(cam_id)
    if not cam:
        return None, "没有这路摄像头：%s" % cam_id
    return cam, ""


def create(cam_id, t0, t1, mask=True, note="", actor="", kind="export"):
    """建一个导出任务。返回 (job|None, 错误)。"""
    cam, err = _cam_or_err(cam_id)
    if err:
        return None, err
    try:
        t0 = int(float(t0))
        t1 = int(float(t1))
    except (TypeError, ValueError):
        return None, "时间参数不合法"
    if t1 <= t0:
        return None, "结束时间必须晚于开始时间"
    if t1 - t0 > MAX_SPAN_SECONDS:
        return None, "一次最多导 %d 小时（当前 %.1f 小时）" % (
            MAX_SPAN_SECONDS // 3600, (t1 - t0) / 3600.0)
    with _lock:
        active = [j for j in _jobs.values() if j["state"] == "running"]
        if len(active) >= MAX_ACTIVE:
            return None, "已有 %d 个导出在跑，稍后再试" % len(active)
    root = C.recordings_root()
    segs = R.Recorder(lambda: C.load(), lambda: root).playlist(
        str(cam_id), t0, t1)
    if not segs:
        return None, "这段时间没有录像"
    jid = secrets.token_hex(8)
    out = os.path.join(exports_dir(), "%s.mp4" % jid)
    job = {"id": jid, "cam": str(cam_id), "name": cam.get("name") or str(cam_id),
           "from": t0, "to": t1, "mask": bool(mask), "note": str(note or "")[:80],
           "state": "running", "pct": 0, "file": out, "error": "",
           "created": time.time(), "actor": actor, "kind": kind,
           "segments": len(segs), "bytes": 0}
    with _lock:
        _jobs[jid] = job
    threading.Thread(target=_run, args=(job, cam, root, segs),
                     daemon=True, name="he-export").start()
    return job, ""


def _set(job, **kw):
    with _lock:
        job.update(kw)


def _probe_size(path):
    """第一段画面的 (宽, 高)；探不到返回 None（那就各段保持原尺寸）。"""
    try:
        p = subprocess.run(
            ["ffprobe", "-hide_banner", "-loglevel", "error",
             "-select_streams", "v:0", "-show_entries", "stream=width,height",
             "-of", "csv=s=x:p=0", path],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
        w, _sep, h = (p.stdout or b"").decode().strip().partition("x")
        return (int(w), int(h)) if w.isdigit() and h.isdigit() else None
    except Exception:
        return None


def _run(job, cam, root, segs):
    t0, t1 = job["from"], job["to"]
    tmpdir = os.path.join(exports_dir(), job["id"] + ".d")
    os.makedirs(tmpdir, exist_ok=True)
    parts = []
    first = os.path.join(root, str(segs[0]["file"]).replace("/", os.sep))
    size = _probe_size(first) if os.path.isfile(first) else None
    try:
        for i, s in enumerate(segs):
            path = os.path.join(root, s["file"].replace("/", os.sep))
            if not os.path.isfile(path):
                _set(job, error="分段已不在：%s" % s["file"])
                continue
            off = max(0.0, t0 - s["start"])
            end = min(float(s["end"]), float(t1)) - s["start"]
            dur = max(0.5, end - off)
            part = os.path.join(tmpdir, "p%03d.mp4" % i)
            cmd = [_ffmpeg_bin(), "-hide_banner", "-loglevel", "error",
                   "-nostdin", "-ss", "%.2f" % off, "-t", "%.2f" % dur,
                   "-i", path]
            # 顺序要紧：遮蔽按源画面的 iw/ih 归一化，必须先画再缩放；缩放放最后
            # 是为了让所有分段尺寸一致 —— concat 用 `-c copy` 拼，尺寸不一致
            # 拼出来的文件会在切换点花屏或干脆放不出。
            vf = list(PV.filters(cam)) if job["mask"] else []
            if size:
                vf.append("scale=%d:%d" % size)
            cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
            if vf:
                cmd += ["-vf", ",".join(vf)]
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-pix_fmt", "yuv420p", "-r", "30"]
            cmd += ["-c:a", "aac", "-b:a", "64k", "-ar", "44100", "-ac", "1",
                    "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                    "-y", part]
            p = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, timeout=900)
            if p.returncode != 0 or not os.path.isfile(part):
                _set(job, error="第 %d 段处理失败：%s" % (
                    i + 1, (p.stderr or b"").decode("utf-8", "replace")[-160:]))
                continue
            parts.append(part)
            _set(job, pct=int(90.0 * len(parts) / max(1, len(segs))))
        if not parts:
            _set(job, state="error", error="没有可导出的画面（可能都被清理了）")
            return
        if len(parts) == 1:
            os.replace(parts[0], job["file"])
        else:
            lst = os.path.join(tmpdir, "list.txt")
            with open(lst, "w", encoding="utf-8") as f:
                for p in parts:
                    f.write("file '%s'\n" % os.path.basename(p))
            r = subprocess.run(
                [_ffmpeg_bin(), "-hide_banner", "-loglevel", "error",
                 "-f", "concat", "-safe", "0", "-i", lst,
                 "-c", "copy", "-movflags", "+faststart", "-y", job["file"]],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600)
            if r.returncode != 0 or not os.path.isfile(job["file"]):
                _set(job, state="error",
                     error="拼接失败：%s" % (r.stderr or b"")
                     .decode("utf-8", "replace")[-160:])
                return
        try:
            _set(job, state="done", pct=100,
                 bytes=os.path.getsize(job["file"]))
        except OSError:
            _set(job, state="done", pct=100)
        try:
            import nw_audit as AU
            AU.log("export", job.get("actor") or "", cam=job["cam"],
                   frm=job["from"], to=job["to"], mask=job["mask"],
                   seconds=job["to"] - job["from"])
        except Exception:
            pass
    except Exception as e:
        _set(job, state="error", error=str(e)[:200])
    finally:
        try:
            for n in os.listdir(tmpdir):
                try:
                    os.unlink(os.path.join(tmpdir, n))
                except OSError:
                    pass
            os.rmdir(tmpdir)
        except OSError:
            pass


def get(job_id):
    with _lock:
        return _jobs.get(str(job_id or ""))


def public_job(job):
    if not job:
        return None
    return {k: v for k, v in job.items() if k != "file"}


def list_jobs(limit=20):
    with _lock:
        rows = sorted(_jobs.values(), key=lambda j: j["created"], reverse=True)
    return [public_job(j) for j in rows[:limit]]


# ---------------------------------------------------------------- 分享
def share(job_id, hours=24, actor="", ip=""):
    """把已完成的导出变成一条带有效期的公开链接。"""
    job = get(job_id)
    if not job:
        return None, "导出任务不存在（可能服务重启过）"
    if job["state"] != "done":
        return None, "导出还没完成，稍后再试"
    try:
        h = float(hours or 24)
    except (TypeError, ValueError):
        h = 24.0
    h = max(0.1, min(h, SHARE_MAX_HOURS))
    fpath = str(job.get("file") or "")
    if not os.path.isfile(fpath):
        return None, "导出文件已被清理，请重新导出"
    token = secrets.token_urlsafe(18)
    rec = {"token": token, "job": job["id"], "cam": job["cam"],
           "name": job.get("name") or job["cam"],
           "from": job["from"], "to": job["to"],
           "file": fpath, "bytes": job.get("bytes") or 0,
           "created": time.time(), "exp": time.time() + h * 3600,
           "created_by": actor, "hits": 0, "note": job.get("note") or ""}
    with _lock:
        shares = _load_shares()
        shares[token] = rec
        _save_shares()
    try:
        import nw_audit as AU
        AU.log("share_create", actor, ip=ip, cam=rec["cam"],
               hours=int(h), frm=rec["from"], to=rec["to"])
    except Exception:
        pass
    return rec, ""


def share_of(token):
    shares = _load_shares()
    with _lock:
        rec = shares.get(str(token or ""))
        if not rec:
            return None, "notfound"
        if float(rec.get("exp") or 0) < time.time():
            shares.pop(str(token), None)
            _save_shares()
            return None, "expired"
        if not os.path.isfile(str(rec.get("file") or "")):
            shares.pop(str(token), None)
            _save_shares()
            return None, "gone"
        rec["hits"] = int(rec.get("hits") or 0) + 1
        _save_shares()
    return rec, "ok"


def shares():
    shares = _load_shares()
    now = time.time()
    with _lock:
        rows = sorted(shares.values(), key=lambda s: s.get("created") or 0,
                      reverse=True)
    out = []
    for s in rows[:50]:
        out.append({"token": s.get("token"), "cam": s.get("cam"),
                    "name": s.get("name"), "from": s.get("from"),
                    "to": s.get("to"), "exp": s.get("exp"),
                    "seconds": (s.get("to") or 0) - (s.get("from") or 0),
                    "hits": s.get("hits") or 0, "note": s.get("note") or "",
                    "alive": float(s.get("exp") or 0) > now,
                    "created_by": s.get("created_by") or ""})
    return out


def revoke(token):
    with _lock:
        shares = _load_shares()
        rec = shares.pop(str(token or ""), None)
        if rec:
            _save_shares()
    if not rec:
        return False, "没有这条分享（可能已过期自动清掉了）"
    try:
        import nw_audit as AU
        AU.log("share_revoke", "", cam=rec.get("cam"), token=str(token)[:6] + "…")
    except Exception:
        pass
    return True, "已撤销"


_SHARE_CSS = (
    'body{margin:0;background:#0b0f14;color:#dfe7ef;'
    'font:14px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;'
    'display:flex;flex-direction:column;min-height:100vh}'
    'header{padding:10px 14px;border-bottom:1px solid #223042}'
    'video{width:100%;max-height:72vh;background:#000;display:block;margin:0 auto}'
    '.meta{padding:10px 14px;color:#93a6bd}.mut{color:#5f748d}'
    '.warn{margin:14px;padding:10px 12px;border:1px solid #7a3b2a;'
    'border-radius:8px;color:#ffb4a2;background:#2a1410}')


def _share_page(rec, base):
    """免登录的分享页：一个原生 <video> + 过期提示，不加载整个应用。"""
    # CSS 里有裸的 `%`，不能整段走 %-格式化，否则 '100%;' 会被当成格式符报错
    f = lambda f_, ts: time.strftime(f_, time.localtime(ts))
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>HomeEye 分享片段</title>'
        '<style>' + _SHARE_CSS + '</style></head><body>'
        '<header><b>HomeEye · 分享片段</b></header>'
        '<video src="' + _esc(base) + '/video.mp4" controls playsinline preload="metadata"></video>'
        '<div class="meta">' + _esc(rec.get("name") or rec.get("cam") or "")
        + ' ｜ ' + f("%m-%d %H:%M", rec["from"]) + ' ~ ' + f("%H:%M", rec["to"])
        + ' ｜ 时长 ' + str(int(rec["to"] - rec["from"])) + ' 秒'
        + '<div class="mut">链接到期：' + f("%Y-%m-%d %H:%M", rec["exp"])
        + ' ｜ 访问次数：' + str(int(rec.get("hits") or 0))
        + ' ｜ 本链接任何人可看，到期或被撤销后自动失效</div></div>'
        '</body></html>')


def _esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def selftest():
    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond

    chk("导出上限常量", MAX_SPAN_SECONDS == 6 * 3600)
    chk("分享最长 30 天", SHARE_MAX_HOURS == 24 * 30)
    chk("token 长度够", len(secrets.token_urlsafe(18)) >= 20)
    chk("路径不会穿越", not re.search(r"[.]{2}", "/tmp/x/ab12.mp4"))
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
