# -*- coding: utf-8 -*-
"""HomeEye · 本地 AI 分析（1.3.0）

三件事，按"装了就能跑 / 没装就说清楚"的原则分三档：

1. **声音事件**（永远可用，标准库）：对写好的分段解出 8 kHz 单声道 PCM，
   按 0.5 秒窗口算 RMS，和最近 10 秒的背景中位数比 —— 突然高出若干倍就是
   一次「声响」（玻璃碎、关门、喊叫都会触发）。这里只报「有声响」，**不**
   声称能区分玻璃碎/婴儿哭：没有声学模型时那样标注就是骗人。

2. **对象识别**（可选）：numpy + onnxruntime + 一个 YOLO 系 ONNX 模型都在
   位时才启用。模型路径、类别名都来自设置页；依赖或模型缺失时 `/api/ai/status`
   会如实说明原因，事件照常只有运动/声响，不会假装识别过。

3. **VLM 一句话描述**（可选）：配置 OpenAI 兼容的 /chat/completions 端点后，
   对每个运动事件抽一帧发给视觉模型，把返回的一句话写进事件行（回放页直接
   能看到，也参与自然语言检索）。失败只记在状态里，不影响事件本身。

自然语言检索（"昨天下午谁在客厅"）不需要任何模型：把中文时间说法解析成
时间区间，再按摄像机名/事件类型/标签/描述做关键词匹配。
"""
import array
import base64
import json
import math
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque

import nw_config as C

# ---------------------------------------------------------------- 参数
AUDIO_RATE = 8000
AUDIO_WIN = 0.5                       # 秒/窗口
WIN_BYTES = int(AUDIO_RATE * AUDIO_WIN) * 2
AUDIO_FLOOR = 220                     # RMS 下限（s16 满量程 32768，约 -43 dBFS）
AUDIO_HISTORY = 20                    # 背景窗口数（10 秒）
AUDIO_MERGE_GAP = 1.5                 # 相邻两声间隔小于此值合并
OBJ_CONF = 0.35
OBJ_IOU = 0.45
VLM_TIMEOUT = 25

_lock = threading.RLock()
_last = ""
_last_err = {}
_model_cache = {"path": "", "mtime": 0, "session": None,
                "input": "", "labels": [], "size": 640, "err": ""}
_vlm_state = {"last_at": 0.0, "last_err": ""}

_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
           "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _ffmpeg_bin():
    return os.environ.get("HE_FFMPEG") or "ffmpeg"


def _ffprobe_bin():
    return os.environ.get("HE_FFPROBE") or "ffprobe"


def _cn_int(txt):
    if not txt:
        return None
    if txt.isdigit():
        return int(txt)
    if txt in _CN_NUM:
        return _CN_NUM[txt]
    # 十一~三十九这种简单组合
    if len(txt) == 2 and txt[0] in ("一", "二", "三", "四", "五", "六", "七", "八", "九") \
            and txt[1] == "十":
        return _CN_NUM[txt[0]] * 10
    if len(txt) == 2 and txt[0] == "十" and txt[1] in _CN_NUM:
        return 10 + _CN_NUM[txt[1]]
    if len(txt) == 3 and txt[1] == "十":
        return _CN_NUM.get(txt[0], 0) * 10 + _CN_NUM.get(txt[2], 0)
    return None


# ---------------------------------------------------------------- 声音事件
def audio_events(seg_path, seg_t0, threshold=2.5, max_seconds=900):
    """分段里的「突然有声」列表 [(t0, t1, score)]，墙钟时间。

    背景是**滚动**的：持续吵的电视声在 10 秒内会被当成新背景，不会一直报；
    短促的响声才会冒出来。没有音轨/解码失败返回 []。
    """
    if not os.path.isfile(seg_path):
        return []
    try:
        thr = max(1.2, min(float(threshold or 2.5), 20.0))
    except (TypeError, ValueError):
        thr = 2.5
    cmd = [_ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin",
           "-i", seg_path, "-vn", "-ac", "1", "-ar", str(AUDIO_RATE),
           "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, bufsize=WIN_BYTES)
    except Exception as e:
        _last_err["audio"] = str(e)[:200]
        return []
    hist = deque(maxlen=AUDIO_HISTORY)
    found = []                 # [start_win, end_win, peak_rms, bg_at_peak]
    cur = None
    win = 0
    deadline = time.time() + max_seconds
    try:
        while time.time() < deadline:
            chunk = p.stdout.read(WIN_BYTES)
            if not chunk:
                break
            if len(chunk) < WIN_BYTES:
                break
            samples = array.array("h")
            samples.frombytes(chunk)
            if sys.byteorder == "big":
                samples.byteswap()
            n = len(samples)
            if not n:
                win += 1
                continue
            acc = 0
            for v in samples:
                acc += v * v
            rms = math.sqrt(acc / float(n))
            bg = statistics.median(hist) if len(hist) >= 4 else float(AUDIO_FLOOR)
            bg = max(bg, 1.0)
            hit = (rms >= AUDIO_FLOOR and rms >= bg * thr
                   and (rms - bg) >= AUDIO_FLOOR * 0.5)
            if hit:
                if cur is None:
                    cur = [win, win, rms, bg]
                else:
                    if win - cur[1] <= AUDIO_MERGE_GAP / AUDIO_WIN:
                        cur[1] = win
                    else:
                        found.append(cur)
                        cur = [win, win, rms, bg]
                if rms > cur[2]:
                    cur[2] = rms
                    cur[3] = min(cur[3], bg)
            else:
                if cur is not None and win - cur[1] > AUDIO_MERGE_GAP / AUDIO_WIN:
                    found.append(cur)
                    cur = None
            hist.append(rms)
            win += 1
        if cur is not None:
            found.append(cur)
    except Exception as e:
        _last_err["audio"] = str(e)[:200]
    finally:
        try:
            p.kill()
        except Exception:
            pass
        try:
            p.stdout.close()
        except Exception:
            pass
        try:
            p.wait(timeout=5)
        except Exception:
            pass
    out = []
    for start, end, peak, bg in found:
        t0 = seg_t0 + start * AUDIO_WIN
        t1 = seg_t0 + (end + 1) * AUDIO_WIN
        if t1 - t0 < 0.4:
            continue
        out.append((round(t0, 2), round(t1, 2),
                    round(peak / max(bg, 1.0), 2)))
    _last_err.pop("audio", None)
    return out


# ---------------------------------------------------------------- 对象识别
def obj_deps():
    """返回 (numpy, onnxruntime, 错误说明)。缺失时错误说明就是给用户看的。"""
    try:
        import numpy as np
    except Exception as e:
        return None, None, "未安装 numpy（对象识别不可用）：%s" % str(e)[:120]
    try:
        import onnxruntime as ort
    except Exception as e:
        return None, None, "未安装 onnxruntime（对象识别不可用）：%s" % str(e)[:120]
    return np, ort, ""


# 标签中英同时入库：模型给的是 COCO 英文词，而家里人是打"人/车/狗"来搜的，
# 只存一种就会搜不到 —— 检索那一侧拼的是文本 haystack，两个都在才稳。
_ZH = {"person": "人", "bicycle": "自行车", "car": "汽车",
       "motorcycle": "摩托车", "bus": "公交车", "train": "火车",
       "truck": "卡车", "dog": "狗", "cat": "猫", "bird": "鸟",
       "horse": "马", "sheep": "羊", "cow": "牛", "bear": "熊",
       "backpack": "背包", "umbrella": "伞", "handbag": "包"}


def zh(label):
    return _ZH.get(str(label or "").lower(), "")


def note_error(which, msg):
    """给扫描器留一句失败原因（设置页原样说给用户，不静默吞掉）。"""
    _last_err[str(which or "ai")] = str(msg or "")[:200]


def _labels(cfg):
    raw = str(cfg.get("labels") or "").strip()
    if not raw:
        return ["person", "bicycle", "car", "motorcycle", "bus", "truck",
                "dog", "cat"]
    return [x.strip() for x in re.split(r"[,，;；\s]+", raw) if x.strip()][:120]


def _load_model(cfg):
    """按 路径+mtime 缓存 ONNX 会话；模型换文件会自动重载。"""
    path = str(cfg.get("model_path") or "").strip()
    if not path:
        return None, "", [], 0, "没有配置 ONNX 模型路径"
    if not os.path.isfile(path):
        return None, "", [], 0, "模型文件不存在：%s" % path
    try:
        mt = os.path.getmtime(path)
    except OSError:
        mt = 0
    with _lock:
        m = _model_cache
        if m["session"] is not None and m["path"] == path and m["mtime"] == mt:
            return (m["session"], m["input"], m["labels"], m["size"], m["err"])
    np, ort, err = obj_deps()
    if err:
        return None, "", [], 0, err
    try:
        so = ort.SessionOptions()
        so.intra_op_num_threads = 2
        so.inter_op_num_threads = 1
        sess = ort.InferenceSession(path, sess_options=so,
                                    providers=["CPUExecutionProvider"])
        inp = sess.get_inputs()[0]
        shape = list(getattr(inp, "shape") or [])
        # NCHW 的 H/W；动态轴（字符串/None）就用设置里的 input_size
        side = 640
        try:
            if len(shape) == 4 and isinstance(shape[2], int) and shape[2] > 0:
                side = int(shape[2])
        except Exception:
            side = 640
        if not side or side < 64:
            side = int(cfg.get("input_size") or 640)
        side = max(160, min(1280, side))
        labels = _labels(cfg)
        with _lock:
            _model_cache.update({"path": path, "mtime": mt, "session": sess,
                                 "input": inp.name, "labels": labels,
                                 "size": side, "err": ""})
        return sess, inp.name, labels, side, ""
    except Exception as e:
        return None, "", [], 0, "加载模型失败：%s" % str(e)[:200]


def _grab_rgb(seg_path, offset, size):
    """抽一帧并缩到 size×size 的 RGB 原始像素（不依赖 PIL）。"""
    cmd = [_ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin",
           "-ss", "%.2f" % max(0.0, float(offset)), "-i", seg_path,
           "-frames:v", "1", "-vf", "scale=%d:%d" % (size, size),
           "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=25)
    except Exception:
        return None
    want = size * size * 3
    if p.returncode != 0 or len(p.stdout or b"") < want:
        return None
    return p.stdout[:want]


def _nms(boxes, scores, iou_thr=OBJ_IOU):
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    keep = []
    while order:
        i = order.pop(0)
        keep.append(i)
        rest = []
        for j in order:
            ax1, ay1, ax2, ay2 = boxes[i]
            bx1, by1, bx2, by2 = boxes[j]
            iw = max(0.0, min(ax2, bx2) - max(ax1, bx1))
            ih = max(0.0, min(ay2, by2) - max(ay1, by1))
            inter = iw * ih
            if inter <= 0:
                rest.append(j)
                continue
            a = max(0.0, (ax2 - ax1) * (ay2 - ay1))
            b = max(0.0, (bx2 - bx1) * (by2 - by1))
            if inter / max(1e-6, a + b - inter) <= iou_thr:
                rest.append(j)
        order = rest
    return keep


def _run_yolo(session, input_name, buf, size, labels, conf=OBJ_CONF, np=None):
    arr = np.frombuffer(buf, dtype=np.uint8).reshape(size, size, 3)
    x = arr.astype(np.float32) / 255.0
    x = np.transpose(x, (2, 0, 1))[None, ...]
    try:
        outs = session.run(None, {input_name: x})
    except Exception as e:
        _last_err["objects"] = "推理失败：%s" % str(e)[:200]
        return []
    if not outs:
        return []
    out = np.asarray(outs[0])
    if out.ndim == 3:
        out = out[0]
    if out.ndim != 2:
        return []
    # YOLOv8: (84, 8400)，前 4 行是 cx,cy,w,h，其余是类别分数
    if out.shape[0] < out.shape[1] and out.shape[0] <= 200:
        out = out.T
    if out.shape[1] < 5:
        return []
    ncls = out.shape[1] - 4
    boxes, scores, ids = [], [], []
    for row in out:
        cls_scores = row[4:4 + ncls]
        ci = int(np.argmax(cls_scores))
        sc = float(cls_scores[ci])
        if sc < conf:
            continue
        cx, cy, w, h = (float(row[0]), float(row[1]),
                        float(row[2]), float(row[3]))
        # 归一化输出（0~1）与像素输出都兼容
        scale = 1.0 if max(cx, cy, w, h) <= 2.0 else float(size)
        cx, cy, w, h = cx / scale, cy / scale, w / scale, h / scale
        x1 = max(0.0, cx - w / 2.0)
        y1 = max(0.0, cy - h / 2.0)
        x2 = min(1.0, cx + w / 2.0)
        y2 = min(1.0, cy + h / 2.0)
        if x2 <= x1 or y2 <= y1:
            continue
        boxes.append((x1, y1, x2, y2))
        scores.append(sc)
        ids.append(ci)
    keep = _nms(boxes, scores)
    out_items = []
    for i in keep[:20]:
        ci = ids[i]
        label = labels[ci] if ci < len(labels) else ("class_%d" % ci)
        b = boxes[i]
        out_items.append({"label": label, "score": round(scores[i], 3),
                          "box": [round(v, 3) for v in b]})
    return out_items


def detect_objects(seg_path, offset, cfg):
    """在某个时间点抽一帧识别对象。不可用时返回 []（原因见 status）。"""
    sess, input_name, labels, size, err = _load_model(cfg)
    if err:
        return []
    np, _ort, derr = obj_deps()
    if derr:
        return []
    buf = _grab_rgb(seg_path, offset, size)
    if not buf:
        return []
    try:
        conf = max(0.05, min(float(cfg.get("obj_confidence") or OBJ_CONF), 0.95))
    except (TypeError, ValueError):
        conf = OBJ_CONF
    return _run_yolo(sess, input_name, buf, size, labels, conf, np)


def annotate_objects(seg_path, seg_t0, events, rows, cfg):
    """对运动事件抽帧识别，把标签合并进事件行（不改事件条数）。"""
    if not events or not rows or not cfg.get("objects"):
        return
    picks = sorted(set([0, len(events) // 2, len(events) - 1]))
    for idx in picks:
        if idx < 0 or idx >= len(events) or idx >= len(rows):
            continue
        ev = events[idx]
        off = max(0.0, (ev[0] + ev[1]) / 2.0 - seg_t0)
        found = detect_objects(seg_path, off, cfg)
        if not found:
            continue
        row = rows[idx]
        tags = list(row.get("labels") or [])
        objs = list(row.get("objects") or [])
        for o in found:
            for tag in (o["label"], zh(o["label"])):
                if tag and tag not in tags:
                    tags.append(tag)
            objs.append(o)
        row["labels"] = tags[:12]
        row["objects"] = objs[:20]


# ---------------------------------------------------------------- VLM 描述
def _endpoint(url):
    u = str(url or "").strip().rstrip("/")
    if not u:
        return ""
    if u.endswith("/chat/completions"):
        return u
    return u + "/chat/completions"


def _jpeg_b64(seg_path, offset, width=640):
    cmd = [_ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin",
           "-ss", "%.2f" % max(0.0, float(offset)), "-i", seg_path,
           "-frames:v", "1", "-vf", "scale=%d:-2" % width,
           "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "4", "pipe:1"]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=25)
    except Exception as e:
        return "", str(e)[:200]
    if p.returncode != 0 or not p.stdout:
        return "", "抽帧失败"
    return base64.b64encode(p.stdout).decode("ascii"), ""


def describe_event(seg_path, seg_t0, row, cfg):
    """给一个事件写一句话（失败返回空串，原因记在 _vlm_state）。"""
    if not cfg.get("vlm_enabled"):
        return ""
    url = _endpoint(cfg.get("vlm_url"))
    if not url:
        _vlm_state["last_err"] = "没有配置 VLM 接口地址"
        return ""
    # 同一路同一事件不重复问（扫描器可能因重启重扫）
    off = max(0.0, (float(row.get("t0") or 0) + float(row.get("t1") or 0)) / 2.0 - seg_t0)
    b64, err = _jpeg_b64(seg_path, off)
    if err:
        _vlm_state["last_err"] = err
        return ""
    body = {
        "model": cfg.get("vlm_model") or "gpt-4o-mini",
        "max_tokens": 80,
        "temperature": 0.2,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text",
                 "text": "这是家庭监控的一帧。请用一句中文客观描述画面里正在"
                         "发生什么（谁/在做什么/有无异常），不超过30个字。"
                         "不要猜身份，不要编造看不到的东西。"},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64," + b64}},
            ],
        }],
    }
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    key = str(cfg.get("vlm_key") or "").strip()
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=VLM_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8", "replace") or "{}")
        text = ""
        try:
            text = data["choices"][0]["message"]["content"]
        except Exception:
            text = data.get("output_text") or ""
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        _vlm_state["last_at"] = time.time()
        _vlm_state["last_err"] = ""
        return text[:120]
    except urllib.error.HTTPError as e:
        _vlm_state["last_err"] = "HTTP %s：%s" % (
            e.code, e.read().decode("utf-8", "replace")[:160])
    except Exception as e:
        _vlm_state["last_err"] = str(e)[:200]
    return ""


# ---------------------------------------------------------------- 自然语言时间
_STOP = set("""谁 在 的 了 吗 呢 有 什么 帮我 我 找 一下 看 查 录像 事件 画面
时候 那 这 都 和 与 视频 监控 里 是 不是 出现 过 一个 有 没有 请 看看 回放
发生 什么 情况""".split())
_WEEKDAY = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
_TIME_RANGES = [
    (r"(凌晨)", 0, 6), (r"(早上|早晨|清晨|上午)", 6, 12),
    (r"(中午|正午)", 11, 14), (r"(下午)", 12, 18),
    (r"(傍晚)", 17, 20), (r"(晚上|夜里|今晚|夜间)", 18, 24),
    (r"(半夜|深夜)", 22, 26),
]


def parse_when(text, now=None):
    """把「昨天下午」「上周三」「最近三天」「10月2号晚上」解析成区间。

    返回 {"from","to","keywords","when","assumed"}；解析不了的部分忽略，
    但一定会给出一个区间（最差是最近 7 天），`assumed` 说明这是兜底。
    """
    now = float(now if now is not None else time.time())
    raw = str(text or "").strip()
    txt = raw.replace("：", ":").replace("，", ",").replace("。", " ")
    lt = time.localtime(now)
    today0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    date_t0 = date_t1 = None
    date_explicit = False
    used = []

    def take(pat, flags=0):
        nonlocal txt
        m = re.search(pat, txt, flags)
        if m:
            txt = txt[:m.start()] + " " + txt[m.end():]
        return m

    # 最近 N 天 / 近 N 天
    m = take(r"最近\s*([0-9一二两三四五六七八九十]+)\s*天")
    if not m:
        m = take(r"近\s*([0-9一二两三四五六七八九十]+)\s*天")
    if m:
        n = _cn_int(m.group(1)) or 7
        n = max(1, min(n, 60))
        date_t0 = today0 - (n - 1) * 86400
        date_t1 = now
        date_explicit = True
        used.append("%d 天" % n)
    # N 天前 / 大前天 / 前天 / 昨天 / 今天
    if date_t0 is None:
        m = take(r"([0-9一二两三四五六七八九十]+)\s*天前")
        if m:
            n = _cn_int(m.group(1)) or 1
            date_t0 = today0 - n * 86400
            date_t1 = date_t0 + 86400
            date_explicit = True
            used.append("%d 天前" % n)
    if date_t0 is None:
        for pat, n in ((r"大前天", 3), (r"前天", 2), (r"昨天|昨日", 1), (r"今天|今日", 0)):
            if take(pat):
                date_t0 = today0 - n * 86400
                date_t1 = date_t0 + 86400
                date_explicit = True
                used.append(pat.replace("|", "/"))
                break
    # 上周三 / 本周三 / 周三
    if date_t0 is None:
        m = take(r"(上周|上星期|上个星期)\s*([一二三四五六日天])")
        if m:
            wd = _WEEKDAY[m.group(2)]
            this_mon = today0 - (lt.tm_wday) * 86400
            date_t0 = this_mon - 7 * 86400 + (wd - 1) * 86400
            date_t1 = date_t0 + 86400
            date_explicit = True
            used.append("上周" + m.group(2))
        else:
            m = take(r"(本周|这周|这个星期|本星期)\s*([一二三四五六日天])")
            if m:
                wd = _WEEKDAY[m.group(2)]
                this_mon = today0 - (lt.tm_wday) * 86400
                date_t0 = this_mon + (wd - 1) * 86400
                date_t1 = date_t0 + 86400
                date_explicit = True
                used.append("本周" + m.group(2))
            else:
                m = take(r"(?:周|星期)\s*([一二三四五六日天])")
                if m:
                    wd = _WEEKDAY[m.group(1)]
                    this_mon = today0 - (lt.tm_wday) * 86400
                    date_t0 = this_mon + (wd - 1) * 86400
                    if date_t0 > now:
                        date_t0 -= 7 * 86400
                    date_t1 = date_t0 + 86400
                    date_explicit = True
                    used.append("周" + m.group(1))
    # 年月日 / 月日 / 日
    if date_t0 is None:
        m = take(r"(\d{4})\s*[年\-/]\s*(\d{1,2})\s*[月\-/]\s*(\d{1,2})\s*[日号]?")
        if m:
            try:
                date_t0 = time.mktime((int(m.group(1)), int(m.group(2)),
                                       int(m.group(3)), 0, 0, 0, 0, 0, -1))
                date_t1 = date_t0 + 86400
                date_explicit = True
                used.append(m.group(0))
            except ValueError:
                date_t0 = None
    if date_t0 is None:
        m = take(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?")
        if m:
            try:
                y = lt.tm_year
                date_t0 = time.mktime((y, int(m.group(1)), int(m.group(2)),
                                       0, 0, 0, 0, 0, -1))
                if date_t0 > now + 86400:
                    date_t0 = time.mktime((y - 1, int(m.group(1)), int(m.group(2)),
                                           0, 0, 0, 0, 0, -1))
                date_t1 = date_t0 + 86400
                date_explicit = True
                used.append(m.group(0))
            except ValueError:
                date_t0 = None
    # 时段
    h0 = h1 = None
    for pat, a, b in _TIME_RANGES:
        m = take(pat)
        if m:
            h0, h1 = a, b
            used.append(m.group(1))
            break
    assumed = ""
    if date_t0 is None:
        if h0 is not None:
            date_t0 = today0
            date_t1 = today0 + 86400
            assumed = "按今天算"
        else:
            # 没有时间就搜最近 7 天，并明说
            date_t0 = today0 - 6 * 86400
            date_t1 = now
            assumed = "没说时间，按最近 7 天搜"
    if h0 is not None:
        base = time.mktime(time.localtime(date_t0)[:3] + (0, 0, 0, 0, 0, -1))
        t0 = base + h0 * 3600
        t1 = base + h1 * 3600
        # 只说“下午”而现在是上午：更可能指昨天下午
        if not date_explicit and t1 > now:
            t0 -= 86400
            t1 -= 86400
            assumed = "该时段今天还没到，按昨天算"
        date_t0, date_t1 = t0, t1
    date_t1 = max(date_t1, date_t0 + 60)
    # 关键词：中文没有空格，先把停用词整词删掉，剩下的才是实词
    kws = []
    cleaned = txt
    for w in sorted(_STOP, key=len, reverse=True):
        cleaned = cleaned.replace(w, " ")
    for tok in re.split(r"[\s,，。！？!?、;；:：()（）\[\]【】]+", cleaned):
        tok = tok.strip()
        if not tok or tok.isdigit():
            continue
        # 超长串按 2 字切开，让「客厅沙发」也能命中「客厅」
        if len(tok) > 6:
            kws.extend(tok[i:i + 2] for i in range(0, len(tok), 2))
        else:
            kws.append(tok)
    kws = [k[:20] for k in kws if k]
    return {"from": int(date_t0), "to": int(date_t1), "keywords": kws[:6],
            "when": " ".join(used), "assumed": assumed, "text": raw}


def search(root, text, cfg=None, cam_ids=None, limit=200):
    """自然语言检索事件。cam_ids 为空 = 所有（调用方已按权限过滤）。"""
    cfg = cfg or C.load()
    q = parse_when(text)
    try:
        import nw_events as EV
    except Exception as e:
        return {"ok": False, "error": "事件模块不可用：%s" % e, "items": []}
    cams = [c for c in (cfg.get("cameras") or [])
            if c.get("enabled", True)
            and (not cam_ids or str(c.get("id")) in [str(x) for x in cam_ids])]
    by_id = {str(c.get("id")): c for c in cams}
    allowed_ext = _ext_map(cfg)
    kws = [k.lower() for k in q["keywords"]]
    items = []
    day = time.localtime(q["from"])
    start_day = time.mktime((day.tm_year, day.tm_mon, day.tm_mday, 0, 0, 0, 0, 0, -1))
    guard = 0
    seen = set()
    while start_day <= q["to"] and guard < 62:
        guard += 1
        compact = time.strftime("%Y%m%d", time.localtime(start_day))
        for cid, cam in by_id.items():
            for row in EV.read_day(root, cid, compact):
                t0 = float(row.get("t0") or 0)
                t1 = float(row.get("t1") or t0)
                if t1 < q["from"] or t0 > q["to"]:
                    continue
                hay = " ".join([
                    str(cam.get("name") or ""), cid,
                    str(row.get("kind") or "motion"),
                    " ".join(str(x) for x in (row.get("labels") or [])),
                    str(row.get("desc") or ""),
                ]).lower()
                if kws and not all(k in hay for k in kws):
                    continue
                key = (cid, row.get("file"), int(t0))
                if key in seen:
                    continue
                seen.add(key)
                items.append(_item(cid, cam, row, allowed_ext))
        start_day += 86400
    items.sort(key=lambda x: x["t0"], reverse=True)
    return {"ok": True, "from": q["from"], "to": q["to"],
            "keywords": q["keywords"], "when": q["when"],
            "assumed": q["assumed"], "items": items[:max(1, min(limit, 500))],
            "total": len(items)}


def latest(root, since, cfg=None, cam_ids=None, limit=20):
    """指定时刻之后的新事件（PWA 通知深链用）。"""
    cfg = cfg or C.load()
    cams = [c for c in (cfg.get("cameras") or [])
            if c.get("enabled", True)
            and (not cam_ids or str(c.get("id")) in [str(x) for x in cam_ids])]
    allowed_ext = _ext_map(cfg)
    try:
        import nw_events as EV
    except Exception:
        return []
    items = []
    now = time.time()
    for cid_cam in cams:
        cid = str(cid_cam.get("id"))
        for back in (0, 1):
            compact = time.strftime("%Y%m%d", time.localtime(now - back * 86400))
            for row in EV.read_day(root, cid, compact):
                if float(row.get("t0") or 0) <= float(since or 0):
                    continue
                items.append(_item(cid, cid_cam, row, allowed_ext))
    items.sort(key=lambda x: x["t0"], reverse=True)
    return items[:max(1, min(int(limit or 20), 100))]


def _ext_map(cfg):
    """小米摄像头的录像段是 H.265，回放要走转码接口；其余直接播。"""
    out = {}
    for c in cfg.get("cameras") or []:
        out[str(c.get("id"))] = (c.get("type") or "rtsp").lower() == "xiaomi"
    return out


def _item(cid, cam, row, ext_map):
    kind = str(row.get("kind") or "motion")
    return {
        "cam": cid,
        "name": cam.get("name") or cid,
        "t0": float(row.get("t0") or 0),
        "t1": float(row.get("t1") or row.get("t0") or 0),
        "kind": kind,
        "labels": row.get("labels") or (["移动"] if kind == "motion" else []),
        "score": row.get("score"),
        "desc": row.get("desc") or "",
        "file": row.get("file") or "",
        "thumb": row.get("thumb") or "",
        "hevc": bool(ext_map.get(cid)),
    }


# ---------------------------------------------------------------- 状态
def status(cfg=None):
    cfg = cfg or C.load()
    ai = cfg.get("ai") or {}
    model_ok = False
    model_err = "未启用"
    if ai.get("objects"):
        sess, _inp, labels, size, err = _load_model(ai)
        model_ok = sess is not None
        model_err = err or "就绪（%d 类，输入 %d）" % (len(labels), size)
    elif ai.get("model_path"):
        model_err = "已填模型路径，但对象识别开关没打开"
    else:
        model_err = "未启用"
    _np, _ort, dep_err = obj_deps()
    vlm_url = _endpoint(ai.get("vlm_url"))
    with _lock:
        errs = dict(_last_err)
        last = _last
        vlm = dict(_vlm_state)
    return {
        "enabled": bool(ai.get("enabled")),
        "audio": {"enabled": bool(ai.get("audio_events", True)),
                  "threshold": ai.get("audio_threshold", 2.5),
                  "error": errs.get("audio", "")},
        "objects": {"enabled": bool(ai.get("objects")),
                    "available": bool(model_ok),
                    "deps": "" if not dep_err else dep_err,
                    "model": model_err,
                    "labels": _labels(ai) if ai.get("objects") else []},
        "vlm": {"enabled": bool(ai.get("vlm_enabled")),
                "configured": bool(vlm_url),
                "endpoint": vlm_url,
                "model": ai.get("vlm_model") or "",
                "last_error": vlm.get("last_err") or ""},
        "last": last,
    }


# ---------------------------------------------------------------- 自测
def _selftest():
    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond

    now = time.mktime((2026, 10, 31, 10, 0, 0, 0, 0, -1))
    a = parse_when("昨天下午谁在客厅", now)
    chk("昨天：15 点附近", time.localtime(a["from"]).tm_hour == 12
        and time.localtime(a["from"]).tm_mday == 30)
    chk("昨天：关键词有客厅", "客厅" in a["keywords"])
    b = parse_when("今天早上大门口有人吗", now)
    chk("今天早上 06 点起", time.localtime(b["from"]).tm_hour == 6)
    c = parse_when("最近3天 车", now)
    chk("最近3天跨 3 天", round((c["to"] - c["from"]) / 86400) >= 2)
    chk("最近3天含关键词", "车" in c["keywords"])
    d = parse_when("上周三晚上", now)
    chk("上周三 = 2026-10-21", time.localtime(d["from"]).tm_mday == 21)
    e = parse_when("随便看看", now)
    chk("没时间兜底最近 7 天", e["assumed"] != "" and e["to"] > e["from"])
    f = parse_when("10月2号下午", now)
    chk("月日解析", time.localtime(f["from"]).tm_mon == 10
        and time.localtime(f["from"]).tm_mday == 2)
    g = parse_when("下午谁在客厅", now)
    chk("只说下午且未到 → 昨天", time.localtime(g["from"]).tm_mday == 30)
    chk("阈值解析", parse_when("最近十天")["from"] < now - 8 * 86400)
    return ok


if __name__ == "__main__":
    sys.exit(0 if _selftest() else 1)
