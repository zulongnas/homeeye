# -*- coding: utf-8 -*-
"""HomeEye · 隐私遮蔽（烧进画面，而不是前端盖一层）

为什么必须烧进码流：前端盖一个黑块只骗眼睛 —— 同一条流换个播放器、或者把
录像文件直接拖进播放软件，原画还是原画。真正的遮蔽只能让 ffmpeg 在编码前把
那块像素本身涂掉，所以实时、录像、导出三条链路全部走这里生成的滤镜。

代价说清楚：一旦配了遮蔽，这一路**必须重编码**（copy 码流没法改像素）。
nw_live / nw_record / nw_xiaomi 因此会在有遮蔽时强制转码，并在状态里说明原因。

区域用归一化坐标（0~1），换分辨率不用重配：
    {"x": 0.1, "y": 0.6, "w": 0.3, "h": 0.35}
"""
MAX_MASKS = 8


def _num(v, lo, hi, default=0.0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, f))


def sanitize(raw):
    """前端提交的区域 → 能跑的列表。越界/重叠不管，ffmpeg 自己会裁。"""
    if not isinstance(raw, (list, tuple)):
        return []
    out = []
    for m in raw[:MAX_MASKS]:
        if not isinstance(m, dict):
            continue
        x = _num(m.get("x"), 0.0, 0.99)
        y = _num(m.get("y"), 0.0, 0.99)
        w = _num(m.get("w"), 0.01, 1.0)
        h = _num(m.get("h"), 0.01, 1.0)
        if x + w > 1.0:
            w = 1.0 - x
        if y + h > 1.0:
            h = 1.0 - y
        if w <= 0.001 or h <= 0.001:
            continue
        out.append({"x": round(x, 4), "y": round(y, 4),
                    "w": round(w, 4), "h": round(h, 4)})
    return out


def masks_of(cam):
    p = (cam or {}).get("privacy") or {}
    return sanitize(p.get("masks"))


def has(cam):
    return bool(masks_of(cam))


def filters(cam):
    """ffmpeg 滤镜片段列表；没有遮蔽就是空列表。

    drawbox 的表达式在运行时按输入尺寸算，所以同一份配置在主/子码流上都对。
    """
    out = []
    for m in masks_of(cam):
        out.append(
            "drawbox=x=iw*%.4f:y=ih*%.4f:w=iw*%.4f:h=ih*%.4f:color=black@1:t=fill"
            % (m["x"], m["y"], m["w"], m["h"]))
    return out


def vf_join(base_filters, cam):
    """把已有滤镜链与遮蔽拼起来（保持顺序：先缩放再涂，涂的坐标不受缩放影响）。"""
    parts = [x for x in (base_filters or []) if x]
    parts.extend(filters(cam))
    return ",".join(parts)


def summary(cam):
    """给页面/日志看的一句话，说明是否真的烧进画面。"""
    n = len(masks_of(cam))
    if not n:
        return "未启用"
    return "%d 块区域已烧进实时与录像（这一路会重编码）" % n
