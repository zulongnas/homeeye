# -*- coding: utf-8 -*-
"""HomeEye · 登录与会话闸门

为什么要加这一层：Docker 版直接监听 0.0.0.0:8088，局域网里任何设备打开
`/api/config` 就能看到 go2rtc 的米家 passToken、把录像整段拖走，而且 frpc
还把入口透出到公网。原来的实现是「谁连上谁就是管理员」。

设计要点
--------
* 密码**不落盘**：只存 `pbkdf2_hmac(sha256)` 的盐 + 派生值，12 万轮。
  配置文件被备份/翻出来也拿不到密码。
* 会话是随机 token（`secrets.token_urlsafe`）放内存表，Cookie 里带
  `HttpOnly + SameSite=Strict`。SameSite=Strict 顺带把 CSRF 挡了：
  别的站点发起的请求根本不会带上这个 Cookie，所以不用再配一套 CSRF token。
* 还没设过密码时处在**认领窗口**：只放行「设密码」这一条 API，其它数据一样要
  登录。放行整套 API 会让"忘了设密码"的机器在局域网里裸奔，而认领只需要用户
  打开页面填一次密码。
* 飞牛统一网关那一路（unix socket）**不闸门**：网关自己已经要求登录飞牛账号，
  再套一层会变成两道密码，而且 iframe 里的 Cookie 会被浏览器当第三方 Cookie
  处理掉。用 `client_address == ("unix", 0)` 识别这条路径。

口令在 8088 上是明文穿过局域网的，所以设完密码请改用 **https://…:8443**
（自签证书，浏览器点一次"继续访问"即可，对讲本来也必须走 HTTPS）。
"""
import hashlib
import hmac
import os
import secrets
import threading
import time

import nw_config as C

COOKIE = "he_session"
ROUNDS = 120000
_MAX_SESSIONS = 200
_LOGIN_WINDOW = {}        # "ip|user" -> [失败时刻...]
_LOGIN_LIMIT = 8          # 15 分钟内允许的失败次数
_LOGIN_COOLDOWN = 30

_lock = threading.RLock()
_sessions = {}            # token -> 过期时刻（monotonic）


def _auth_cfg():
    return dict(C.load().get("auth") or {})


def enabled():
    """闸门总开关。默认开；显式 enabled=false 才关掉（网关模式不需要它）。"""
    a = _auth_cfg()
    return bool(a.get("enabled", True))


def username():
    return (_auth_cfg().get("username") or "admin").strip() or "admin"


def configured():
    """是否已经设过密码。没设过就不拦（首次运行认领）。"""
    a = _auth_cfg()
    return bool(a.get("pw_hash")) and bool(a.get("salt"))


def required():
    """当前是否需要登录才有意义。"""
    return enabled() and configured()


# ---------------------------------------------------------------- 口令
def _derive(password, salt_hex):
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        salt = b""
    return hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"),
                               salt, ROUNDS).hex()


def set_password(password, old_password=None):
    """设置/修改登录密码。已设过密码时必须带旧密码（未设过时免检）。

    返回 (ok, 说明)。
    """
    password = password or ""
    if len(password) < 6:
        return False, "密码至少 6 位"
    if len(password) > 128:
        return False, "密码太长"
    a = _auth_cfg()
    if configured():
        if not verify(a.get("username"), old_password or ""):
            return False, "原密码不正确"
    salt = secrets.token_hex(16)
    cfg = C.load()
    cfg["auth"] = dict(cfg.get("auth") or {})
    cfg["auth"].update({
        "enabled": True,
        "username": username(),
        "salt": salt,
        "pw_hash": _derive(password, salt),
        "session_hours": float(cfg["auth"].get("session_hours") or 24),
    })
    C.save(cfg)
    with _lock:
        _sessions.clear()      # 改了密码，旧会话全部作废
    return True, "密码已设置"


def verify(user, password):
    a = _auth_cfg()
    if not a.get("pw_hash") or not a.get("salt"):
        return False
    if str(user or "").strip().lower() != username().lower():
        return False
    want = bytes.fromhex(str(a.get("pw_hash")))
    got = bytes.fromhex(_derive(password or "", a.get("salt")))
    return hmac.compare_digest(want, got)


# ---------------------------------------------------------------- 会话
def _ttl_seconds():
    try:
        hours = float(_auth_cfg().get("session_hours") or 24)
    except (TypeError, ValueError):
        hours = 24.0
    return max(300.0, min(hours, 24 * 30) * 3600.0)


def _prune(now):
    for token in [t for t, exp in _sessions.items() if exp <= now]:
        _sessions.pop(token, None)


def login(user, password, peer=""):
    """校验口令，成功返回新会话 token。失败返回 None（并计入限流）。"""
    key = "%s|%s" % (peer, str(user or "").strip().lower())
    now = time.monotonic()
    with _lock:
        hits = [t for t in _LOGIN_WINDOW.get(key, []) if now - t < _LOGIN_COOLDOWN * 60]
        _LOGIN_WINDOW[key] = hits
        if len(hits) >= _LOGIN_LIMIT:
            return None
    ok = verify(user, password)
    with _lock:
        if ok:
            _LOGIN_WINDOW.pop(key, None)
            token = secrets.token_urlsafe(32)
            _prune(now)
            while len(_sessions) >= _MAX_SESSIONS:
                _sessions.pop(next(iter(_sessions)), None)
            _sessions[token] = now + _ttl_seconds()
            return token
        _LOGIN_WINDOW.setdefault(key, []).append(now)
    return None


def logout(token):
    with _lock:
        _sessions.pop(token or "", None)


def check(token):
    if not token:
        return False
    now = time.monotonic()
    with _lock:
        exp = _sessions.get(token)
        if exp is None:
            return False
        if exp <= now:
            _sessions.pop(token, None)
            return False
        # 滑动续命：还在用就不该在半路掉线
        _sessions[token] = now + _ttl_seconds()
        return True


def token_from_headers(headers):
    raw = headers.get("Cookie") or ""
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            return v.strip()
    return ""


def cookie(token, logout=False):
    """Set-Cookie 头值。logout 时把有效期压成立刻过期。"""
    max_age = 0 if logout or not token else int(_ttl_seconds())
    return "%s=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d" % (
        COOKIE, token or "", max_age)


# ---------------------------------------------------------------- 闸门
_OPEN_PREFIXES = ("/api/login", "/api/logout", "/api/health", "/api/auth")


def is_gateway(handler):
    """unix socket（飞牛统一网关）那一路不闸门：网关已经鉴过一次。"""
    try:
        addr = getattr(handler, "client_address", None) or ()
        return bool(addr) and str(addr[0]) == "unix"
    except Exception:
        return False


def metrics_token():
    """Prometheus 抓取令牌：部署方用 HE_METRICS_TOKEN 指定的环境变量。空 = 未开。"""
    return (os.environ.get("HE_METRICS_TOKEN") or "").strip()


def metrics_token_ok(handler, q):
    """带对了 Bearer 令牌（或 ?token=）就放行 /api/metrics。"""
    want = metrics_token()
    if not want:
        return False
    got = ""
    authz = str(handler.headers.get("Authorization") or "")
    if authz.lower().startswith("bearer "):
        got = authz[7:].strip()
    if not got:
        try:
            v = (q or {}).get("token")
            # parse_qs 给的是 list；直接 str(list) 会把括号也带进来
            got = str(v[0]) if isinstance(v, (list, tuple)) and v else str(v or "")
        except Exception:
            got = ""
    if not got:
        return False
    return hmac.compare_digest(got.encode(), want.encode())


def allows(handler, path, method, q):
    """这个方法/路径是否需要登录。返回 True 表示放行。"""
    if not enabled():
        return True
    if is_gateway(handler):
        return True
    if path.startswith(_OPEN_PREFIXES):
        return True
    # 静态：非 /api /live /rec /replay 的一律放行（页面自己要知道"还没登录"）
    if not (path.startswith("/api") or path.startswith("/live")
            or path.startswith("/rec") or path.startswith("/replay")):
        return True
    if path.startswith("/api/metrics") and metrics_token_ok(handler, q):
        # Prometheus 抓取：部署方给了 HE_METRICS_TOKEN 就认 Bearer 令牌。
        # 放在「认领窗口」前面是故意的：监控应当从部署那一刻就能抓，不必
        # 等有人第一次登录设完密码。
        return True
    if not configured():
        # 认领窗口：还没设密码时只留「设密码」这一条路。放行整套 API 等于
        # 没设密码前局域网里谁都能翻录像和米家凭据，那才是真正裸奔的一段。
        return path.startswith("/api/password")
    # 录像文件 / 直播流 / 其它 API 全部要会话
    return check(token_from_headers(handler.headers))


def public_state(logged_in=False):
    """给前端判断"要不要弹登录框"用，未登录也能查。"""
    return {"enabled": enabled(), "configured": configured(),
            "logged_in": bool(logged_in),
            "username": username() if configured() else ""}
