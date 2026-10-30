# -*- coding: utf-8 -*-
"""运行环境自检。

装完能不能正常跑，取决于三样东西：ffmpeg、Python 版本、小米 SDK（含它那堆
第三方依赖）。它们由谁装，两种安装方式完全不同：

* Docker 镜像：ffmpeg 和 Python 依赖都烤进镜像了，用户不用管；
  小米 SDK 因许可原因不随镜像分发，要用户自己 fetch + 重建。
* fpk：一律用宿主机的 ffmpeg / python3，fpk 只拷自己的代码，不装 Python 包，
  所以小米那一路要用户在宿主机上自己 pip 装依赖。

这里只做**探测**：不改环境、不装东西，目的是出问题时能一句话说清缺什么，
而不是让用户对着 "小米一直 0 帧" 猜。结果只在启动时打日志、登录后进
/api/health，未登录的 healthcheck 看不到（不对外泄露宿主机路径）。
"""
import os
import shutil
import subprocess
import sys
import threading
import time

_lock = threading.Lock()
_cache = {"at": 0.0, "data": None}
_TTL = 60.0

# 小米 SDK 取流链路真正会 import 的第三方包（见 docker/requirements.txt）。
# 用 find_spec 而不是 import：只查存在性，不执行模块，不会拖慢启动、
# 也不会因为某个包 import 时打日志污染输出。
XIAOMI_DEPS = (
    ("aiohttp", "aiohttp"),
    ("aiofiles", "aiofiles"),
    ("requests", "requests"),
    ("cryptography", "cryptography"),
    ("av", "av (PyAV)"),
    ("numpy", "numpy"),
    ("pydantic", "pydantic"),
    ("yaml", "PyYAML"),
    ("psutil", "psutil"),
    ("qrcode", "qrcode"),
    ("PIL", "Pillow"),
)


def _importable(mod):
    try:
        from importlib.util import find_spec
        return find_spec(mod) is not None
    except Exception:
        return False


def _ffmpeg():
    """宿主机/镜像里的 ffmpeg。取流、转码、切片全靠它，缺了整站没有画面。"""
    path = shutil.which("ffmpeg") or ""
    if not path:
        return {"ok": False, "path": "", "version": ""}
    kwargs = {}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = 0x08000000     # 不弹控制台窗口
    try:
        p = subprocess.run([path, "-version"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=5, **kwargs)
        first = (p.stdout or b"").decode("utf-8", "replace").splitlines()
        ver = first[0].replace("ffmpeg version ", "").split(" ")[0] if first else ""
        return {"ok": True, "path": path, "version": ver}
    except Exception as e:
        return {"ok": True, "path": path, "version": "", "error": str(e)[:120]}


def _install_kind():
    if os.path.exists("/.dockerenv"):
        return "docker"
    if os.environ.get("TRIM_APPDEST"):
        return "fnos"
    return "manual"


def _xiaomi():
    """小米那一路的可用性：SDK 装没装、依赖齐不齐。

    先只查目录和包是否存在，两样都齐了才真去 import SDK —— 自检是启动时跑的，
    不能为了判断可用性把 SDK（连带 numpy/av）先加载一遍。
    """
    import nw_xiaomi as X
    sdk_found = os.path.isdir(X.SDK_PATH)
    missing = [label for mod, label in XIAOMI_DEPS if not _importable(mod)]
    out = {"sdk": X.SDK_PATH, "sdk_found": sdk_found, "missing": missing,
           "ok": False, "reason": ""}
    if not sdk_found:
        out["reason"] = "SDK 目录不存在"
        return out
    if missing:
        # 依赖缺失是 fpk 最常见的坑（包没装），单独列出来才好给出可执行的命令
        out["reason"] = "缺 Python 依赖：" + "、".join(missing)
        return out
    try:
        out["ok"], out["reason"] = X.xiaomi_available()
    except Exception as e:
        out["reason"] = str(e)[:200]
    return out


def report(force=False):
    now = time.time()
    with _lock:
        if not force and _cache["data"] is not None and now - _cache["at"] < _TTL:
            return dict(_cache["data"])
    py = "%d.%d.%d" % sys.version_info[:3]
    ff = _ffmpeg()
    try:
        xi = _xiaomi()
    except Exception as e:
        xi = {"ok": False, "reason": str(e)[:200], "sdk": "", "sdk_found": False,
              "missing": []}
    warnings = []
    notes = []
    if not ff["ok"]:
        warnings.append("缺少 ffmpeg：直播、录像、时间轴全部不可用（Docker 镜像自带；"
                        "fpk/裸跑需要系统里有 ffmpeg 4.x+）")
    if sys.version_info[:2] < (3, 9):
        warnings.append("Python %s 过旧：后端需要 3.9+（fpk 用宿主机的 python3）" % py)
    if not xi["ok"]:
        if not xi["sdk_found"]:
            # 只装 RTSP/ONVIF 摄像头的人本来就不需要它，别当故障报
            notes.append("小米 SDK 未安装：只用 RTSP/ONVIF 摄像头可忽略；"
                         "要用小米请先跑 tools/fetch-miloco-sdk.sh")
        elif xi["missing"]:
            warnings.append("小米 SDK 缺 Python 依赖：%s（fpk 需在宿主机 pip 安装，"
                            "Docker 镜像已内置）" % "、".join(xi["missing"]))
        else:
            warnings.append("小米 SDK 加载失败：%s" % (xi["reason"] or "未知原因"))
    data = {
        "install": _install_kind(),
        "python": py,
        "ffmpeg": ff,
        "xiaomi": xi,
        "warnings": warnings,
        "notes": notes,
    }
    with _lock:
        _cache["data"] = data
        _cache["at"] = now
    return dict(data)


def startup_log(log):
    """启动时把自检结论写进日志：出问题时的第一现场。

    log 是「写一段文本」的回调（main.py 传的是 sys.stderr.write），
    换行由这里补 —— 日志是给人看的，粘成一行就没法看了。
    """
    def out(s):
        log(s + "\n")

    d = report(force=True)
    out("[env] install=%s python=%s ffmpeg=%s%s" % (
        d["install"], d["python"],
        d["ffmpeg"]["version"] or ("有路径但探测失败" if d["ffmpeg"]["ok"] else "缺失"),
        " (%s)" % d["ffmpeg"]["path"] if d["ffmpeg"]["path"] else ""))
    x = d["xiaomi"]
    out("[env] xiaomi=%s%s" % (
        "ok" if x["ok"] else "unavailable",
        "" if x["ok"] else "（%s）" % (x["reason"] or "")[:160]))
    for w in d["warnings"]:
        out("[env] 警告: " + w)
    for n in d["notes"]:
        out("[env] 提示: " + n)
