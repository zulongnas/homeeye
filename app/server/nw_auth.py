# -*- coding: utf-8 -*-
"""HomeEye · 登录、会话与按摄像头授权

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

1.3-10 的多用户
--------------
内置管理员还是老那一段 `auth`（升级零迁移：设过密码的机器不会突然登不进去），
其它人放在 `users` 列表里，每人一个盐与派生值。两种角色：

  admin  全部能看能改（等价于内置管理员）
  read   只能看自己被授权那几路的直播/回放/事件，设置、摄像头增删、用户、
         导出分享一律 403

`cams` 是空列表 = 这一路都不限制；非空 = 只允许列表里的摄像头 id。**授权判定
放在数据出口**（直播流、录像文件、事件列表、检索结果），不是只把页面上的按钮
藏起来 —— 只藏按钮等于让只读用户手输 `/live/c2/ts` 就能看到主卧。

口令在 8088 上是明文穿过局域网的，所以设完密码请改用 **https://…:8443**
（自签证书，浏览器点一次"继续访问"即可，对讲本来也必须走 HTTPS）。
"""
import hashlib
import hmac
import os
import re
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
_NAME_RE = re.compile(r"^[\w.\-@一-鿿]{1,32}$")
ROLES = ("admin", "read")

_lock = threading.RLock()
_sessions = {}            # token -> {"exp","user","role","cams","ip"}


def _audit(kind, actor="", ip="", **kw):
    """审计写不进去不该影响鉴权本身，所以整体吞掉异常。"""
    try:
        import nw_audit as AU
        AU.log(kind, actor, ip=ip, **kw)
    except Exception:
        pass


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


def _check_hash(user_hash, salt_hex, password):
    if not user_hash or not salt_hex:
        return False
    try:
        want = bytes.fromhex(str(user_hash))
    except ValueError:
        return False
    got = bytes.fromhex(_derive(password or "", salt_hex))
    return hmac.compare_digest(want, got)


def set_password(password, old_password=None):
    """设置/修改**内置管理员**的登录密码。已设过密码时必须带旧密码。

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
    drop_all_sessions()
    return True, "密码已设置"


def verify(user, password):
    a = _auth_cfg()
    if str(user or "").strip().lower() != username().lower():
        return False
    return _check_hash(a.get("pw_hash"), a.get("salt"), password)


# ---------------------------------------------------------------- 用户表
def _user_rows():
    return [u for u in (C.load().get("users") or []) if isinstance(u, dict)]


def users():
    """所有人（内置管理员排第一）。不含盐与派生值，可以安全地回给前端。"""
    rows = [{"name": username(), "role": "admin", "cams": [],
             "builtin": True, "has_password": configured(), "enabled": True}]
    for u in _user_rows():
        rows.append({"name": str(u.get("name") or ""),
                     "role": u.get("role") if u.get("role") in ROLES else "read",
                     "cams": [str(x) for x in (u.get("cams") or [])],
                     "builtin": False,
                     "enabled": bool(u.get("enabled", True)),
                     "has_password": bool(u.get("pw_hash") and u.get("salt")),
                     "created": u.get("created") or 0})
    return rows


def find_raw(name):
    for u in _user_rows():
        if str(u.get("name") or "").lower() == str(name or "").lower():
            return u
    return None


def _valid_name(name):
    name = str(name or "").strip()
    if not _NAME_RE.match(name):
        return "", "用户名只能是 1~32 个字（中英文数字、点、横线、下划线）"
    if name.lower() == username().lower():
        return "", "这个名字是内置管理员，不能重复建"
    return name, ""


def _valid_cams(cams):
    ids = {str(c.get("id")) for c in (C.load().get("cameras") or [])}
    out = []
    for x in cams or []:
        s = str(x or "").strip()
        if not s:
            continue
        if s not in ids:
            return None, "没有这路摄像头：%s" % s[:40]
        if s not in out:
            out.append(s)
    return out, ""


def create_user(name, password, role="read", cams=None):
    """新建一个家庭成员账号。返回 (user|None, 错误)。"""
    name, err = _valid_name(name)
    if err:
        return None, err
    if find_raw(name):
        return None, "已存在同名用户：%s" % name
    if len(password or "") < 6:
        return None, "密码至少 6 位"
    if len(password or "") > 128:
        return None, "密码太长"
    role = role if role in ROLES else "read"
    cams, err = _valid_cams(cams)
    if err:
        return None, err
    salt = secrets.token_hex(16)
    rec = {"name": name, "role": role, "cams": cams or [],
           "salt": salt, "pw_hash": _derive(password, salt),
           "enabled": True, "created": time.time()}
    cfg = C.load()
    cfg["users"] = _user_rows() + [rec]
    C.save(cfg)
    _audit("user_create", "", role=role, user=name, cams=len(rec["cams"]))
    return {k: v for k, v in rec.items() if k not in ("salt", "pw_hash")}, ""


def update_user(name, password=None, role=None, cams=None, enabled=None):
    """改一个用户。password 传了才改口令；其余字段不传保持原样。"""
    cfg = C.load()
    rows = _user_rows()
    hit = None
    for u in rows:
        if str(u.get("name") or "").lower() == str(name or "").lower():
            hit = u
            break
    if hit is None:
        return None, "没有这个用户：%s" % name
    if role is not None:
        if role not in ROLES:
            return None, "角色只能是 admin 或 read"
        hit["role"] = role
    if cams is not None:
        cams, err = _valid_cams(cams)
        if err:
            return None, err
        hit["cams"] = cams or []
    if enabled is not None:
        if enabled is False and hit.get("role") == "admin" and not _other_admin():
            return None, "至少要留一个管理员，否则没人能再进设置"
        hit["enabled"] = bool(enabled)
    if password:
        if len(password) < 6:
            return None, "密码至少 6 位"
        hit["salt"] = secrets.token_hex(16)
        hit["pw_hash"] = _derive(password, hit["salt"])
        with _lock:
            for tok, s in list(_sessions.items()):
                if str(s.get("user") or "").lower() == str(name).lower():
                    _sessions.pop(tok, None)
    cfg["users"] = rows
    C.save(cfg)
    if not password:
        # 没改口令也要动会话：管理员把某人的角色或摄像头范围收了，他手上那条
        # Cookie 还能按老范围继续看一整天 —— 那等于授权是摆设。停用更直接，
        # 立刻踢下线。
        with _lock:
            for tok, s in list(_sessions.items()):
                if str(s.get("user") or "").lower() != str(name).lower():
                    continue
                if enabled is False:
                    _sessions.pop(tok, None)
                    continue
                s["role"] = hit.get("role") or "read"
                s["name"] = hit.get("name") or s.get("name")
                s["cams"] = [str(x) for x in (hit.get("cams") or [])]
    _audit("user_update", "", user=str(name))
    return {k: v for k, v in hit.items() if k not in ("salt", "pw_hash")}, ""


def delete_user(name):
    cfg = C.load()
    rows = _user_rows()
    hit = find_raw(name)
    if hit is None:
        return False, "没有这个用户：%s" % name
    if hit.get("role") == "admin" and not _other_admin(rows, str(hit.get("name"))):
        return False, "这是最后一个管理员，删掉就没人能进设置了"
    cfg["users"] = [u for u in rows
                    if str(u.get("name") or "").lower() != str(name).lower()]
    C.save(cfg)
    with _lock:
        for tok, s in list(_sessions.items()):
            if str(s.get("user") or "").lower() == str(name or "").lower():
                _sessions.pop(tok, None)
    _audit("user_delete", "", user=str(name))
    return True, "已删除用户 %s" % name


def _other_admin(rows=None, exclude=""):
    """除 exclude 之外还有没有启用的管理员（内置管理员算一个）。"""
    if configured() and username().lower() != str(exclude).lower():
        return True
    for u in (rows if rows is not None else _user_rows()):
        if (u.get("role") == "admin" and u.get("enabled", True)
                and str(u.get("name") or "").lower() != str(exclude).lower()):
            return True
    return False


# ---------------------------------------------------------------- 会话
def _ttl_seconds():
    try:
        hours = float(_auth_cfg().get("session_hours") or 24)
    except (TypeError, ValueError):
        hours = 24.0
    return max(300.0, min(hours, 24 * 30) * 3600.0)


def _prune(now):
    for token in [t for t, s in _sessions.items() if s["exp"] <= now]:
        _sessions.pop(token, None)


def _verify_any(user, password):
    """先试内置管理员，再试 users 表。返回 (user dict|None)。"""
    if verify(user, password):
        return {"name": username(), "role": "admin", "cams": [], "builtin": True}
    u = find_raw(user)
    if not u or not u.get("enabled", True):
        return None
    if not _check_hash(u.get("pw_hash"), u.get("salt"), password):
        return None
    return {"name": str(u.get("name") or ""),
            "role": u.get("role") if u.get("role") in ROLES else "read",
            "cams": [str(x) for x in (u.get("cams") or [])],
            "builtin": False}


def login(user, password, peer=""):
    """校验口令，成功返回新会话 token。失败返回 None（并计入限流）。"""
    key = "%s|%s" % (peer, str(user or "").strip().lower())
    now = time.monotonic()
    with _lock:
        hits = [t for t in _LOGIN_WINDOW.get(key, []) if now - t < _LOGIN_COOLDOWN * 60]
        _LOGIN_WINDOW[key] = hits
        if len(hits) >= _LOGIN_LIMIT:
            _audit("login_locked", str(user or "")[:32], ip=peer)
            return None
    who = _verify_any(user, password)
    with _lock:
        if who:
            _LOGIN_WINDOW.pop(key, None)
            token = secrets.token_urlsafe(32)
            _prune(now)
            while len(_sessions) >= _MAX_SESSIONS:
                _sessions.pop(next(iter(_sessions)), None)
            _sessions[token] = {"exp": now + _ttl_seconds(),
                                "user": who["name"], "role": who["role"],
                                "cams": who["cams"], "ip": peer}
            _audit("login", who["name"], ip=peer, role=who["role"])
            return token
        _LOGIN_WINDOW.setdefault(key, []).append(now)
    _audit("login_fail", str(user or "")[:32], ip=peer)
    return None


def logout(token, peer=""):
    with _lock:
        s = _sessions.pop(token or "", None)
    if s:
        _audit("logout", s.get("user") or "", ip=peer)


def drop_all_sessions():
    """改了管理员密码：旧会话全部作废（别人的登录态也要重新来）。"""
    with _lock:
        _sessions.clear()


def current(token):
    """会话对应的用户（顺便滑动续期）。无效返回 None。"""
    if not token:
        return None
    now = time.monotonic()
    with _lock:
        s = _sessions.get(token)
        if s is None:
            return None
        if s["exp"] <= now:
            _sessions.pop(token, None)
            return None
        s["exp"] = now + _ttl_seconds()
        return dict(s)


def check(token):
    return current(token) is not None


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


# ---------------------------------------------------------------- 授权判定
def gateway_session():
    """网关那一路没有 Cookie，但已经在飞牛侧鉴过权 —— 当管理员看待。"""
    return {"name": "网关", "user": "网关", "role": "admin", "cams": [],
            "builtin": True, "gateway": True}


def open_session():
    """闸门被关掉（auth.enabled=false）时的匿名会话。

    必须有这个：按摄像头授权是在数据出口用会话判的，没有会话就等于一律拒绝 ——
    那样「关掉登录闸门」反而会让页面一台摄像头都看不到，逻辑正好拧了。
    """
    return {"name": "开放模式", "user": "开放模式", "role": "admin",
            "cams": [], "builtin": True, "open": True}


def is_admin(sess):
    return bool(sess) and str(sess.get("role") or "") == "admin"


def cam_ok(sess, cam_id):
    """这个会话能不能看这路摄像头。cams 为空 = 不限（管理员与老配置）。"""
    if not sess:
        return False
    cams = [str(x) for x in (sess.get("cams") or [])]
    return not cams or str(cam_id or "") in cams


def visible_cams(sess, cams=None):
    """按权限过滤摄像头列表；None 表示当前会话能看的全部 id。"""
    if sess is None:
        return []
    cams = cams if cams is not None else (C.load().get("cameras") or [])
    return [c for c in cams if cam_ok(sess, c.get("id"))]


# ---------------------------------------------------------------- 闸门
# /share/<token> 是"带有效期的公开链接"，故意免登录；/exports 与 /export
# 是管理员导出的原片，必须登录后才能取。
_OPEN_PREFIXES = ("/api/login", "/api/logout", "/api/health", "/api/auth",
                  "/share/")
_AUTH_PREFIXES = ("/api", "/live", "/rec", "/replay", "/export", "/rech264")


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
    """这个方法/路径是否需要登录。返回 True 表示放行。

    注意这里只管"要不要登录"，具体能看哪一路由 `cam_ok` 在数据出口判。
    """
    if path.startswith("/share/"):
        return True
    if not enabled():
        return True
    if is_gateway(handler):
        return True
    if path.startswith(_OPEN_PREFIXES):
        return True
    # 静态：非鉴权前缀的一律放行（页面自己要知道"还没登录"）
    if not path.startswith(_AUTH_PREFIXES):
        return True
    if path.startswith("/api/metrics") and metrics_token_ok(handler, q):
        # Prometheus 抓取：部署方给了 HE_METRICS_TOKEN 就认 Bearer 令牌。
        # 放在「认领窗口」前面是故意的：监控应当从部署那一刻就能抓，不必
        # 等有人第一次登录设完密码。
        return True
    if not configured() and not _user_rows():
        # 认领窗口：还没设密码时只留「设密码」这一条路。放行整套 API 等于
        # 没设密码前局域网里谁都能翻录像和米家凭据，那才是真正裸奔的一段。
        return path.startswith("/api/password")
    # 录像文件 / 直播流 / 导出原片 / 其它 API 全部要会话
    return check(token_from_headers(handler.headers))


def public_state(sess=None):
    """给前端判断"要不要弹登录框 / 该不该显示设置入口"用，未登录也能查。

    参数是 `current()` 拿到的会话（没登录传 None）。角色与自己那几路授权
    要回给前端：页面据此隐藏设置入口、过滤九宫格 —— 但真正的拦截在数据出口。
    """
    return {"enabled": enabled(), "configured": configured(),
            "logged_in": bool(sess),
            "username": (sess or {}).get("user") or (username() if configured() else ""),
            "role": (sess or {}).get("role") or "",
            "cams": [str(x) for x in ((sess or {}).get("cams") or [])],
            "admin": is_admin(sess)}


# ---------------------------------------------------------------- 自测
def _selftest():
    ok = True

    def chk(name, cond):
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond

    admin = {"user": "admin", "role": "admin", "cams": []}
    guest = {"user": "guest", "role": "read", "cams": ["c1", "c2"]}
    chk("管理员不限摄像头", cam_ok(admin, "c9"))
    chk("只读用户命中授权", cam_ok(guest, "c2"))
    chk("只读用户越界被拒", not cam_ok(guest, "c9"))
    chk("空会话什么都看不到", not cam_ok(None, "c1"))
    chk("角色判定", is_admin(admin) and not is_admin(guest))
    chk("口令派生可复现", _derive("abc123", "00" * 16) ==
        _derive("abc123", "00" * 16))
    chk("口令派生随盐变化", _derive("abc123", "00" * 16) !=
        _derive("abc123", "01" * 16))
    chk("比对不会误报", _check_hash(_derive("p", "ab" * 16), "ab" * 16, "p")
        and not _check_hash(_derive("p", "ab" * 16), "ab" * 16, "q"))
    n, e = _valid_name("  ")
    chk("空名字被拒", not n and bool(e))
    chk("超长名字被拒", not _valid_name("x" * 40)[0])
    chk("中文名可以", _valid_name("奶奶")[0] == "奶奶")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if _selftest() else 1)
