# -*- coding: utf-8 -*-
"""HomeEye · ONVIF 设备发现（纯标准库实现）

为什么要自己写而不用 python-onvif：
  * 飞牛 NAS 的自带 Python 环境里没有 onvif 依赖，打包进 fpk 要么装不上、
    要么体积失控；而这里只需要「发现设备 + 取 RTSP 地址」两件事，
    用 socket + urllib + hashlib 完全够。

两件事：
  1. discover()            —— WS-Discovery 组播探测局域网内所有 ONVIF 设备
  2. get_stream_uris()     —— 用 GetProfiles + GetStreamUri 取该设备的 RTSP 地址

认证用 ONVIF 规定的 WS-UsernameToken PasswordDigest：
    Digest = Base64( SHA1( Nonce + Created + Password ) )
部分固件会先回一个带 Challenge 的 401，此时把 Challenge 当 Nonce 重发一次即可。
"""
import base64
import hashlib
import os
import re
import socket
import time
import urllib.request
import urllib.error
import urllib.parse

MULTICAST_ADDR = "239.255.255.250"
MULTICAST_PORT = 3702

_PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>uuid:%s</w:MessageID>
    <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body>
    <d:Probe>
      <d:Types>dn:NetworkVideoTransmitter</d:Types>
    </d:Probe>
  </e:Body>
</e:Envelope>"""


def _uuid():
    return re.sub(r"-", "", str(os.urandom(16).hex())).rjust(32, "0")


def discover(timeout=3.0, tries=2):
    """组播探测 ONVIF 设备。

    返回 [{"host": "192.168.1.64", "port": 80, "xaddrs": "...", "name": "..."}]
    name 可能为空（很多固件不回 Scopes 里的名字）。
    """
    found = {}
    msg = (_PROBE % _uuid()).encode("utf-8")
    for _attempt in range(max(1, tries)):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except OSError:
                pass
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.settimeout(timeout)
            sock.sendto(msg, (MULTICAST_ADDR, MULTICAST_PORT))
            deadline = time.time() + timeout
            while True:
                left = deadline - time.time()
                if left <= 0:
                    break
                sock.settimeout(left)
                try:
                    data, addr = sock.recvfrom(65535)
                except socket.timeout:
                    break
                except OSError:
                    break
                host = addr[0]
                if host in found:
                    continue
                try:
                    text = data.decode("utf-8", "replace")
                except Exception:
                    text = ""
                m = re.search(r"XAddrs[^>]*>([^<]+)<", text)
                xaddrs = m.group(1).strip() if m else ""
                # 优先用 XAddrs 里带的 host/port（有的设备回的是内网另一地址）
                h, p = host, 80
                if xaddrs:
                    mm = re.match(r"https?://([^:/]+)(?::(\d+))?", xaddrs)
                    if mm:
                        h = mm.group(1)
                        if mm.group(2):
                            p = int(mm.group(2))
                name = ""
                ms = re.search(r"onvif://www\.onvif\.org/name/([^/\s<]+)", text)
                if ms:
                    try:
                        name = urllib.parse.unquote(ms.group(1))
                    except Exception:
                        name = ms.group(1)
                found[host] = {"host": h, "port": p, "xaddrs": xaddrs, "name": name}
        except Exception:
            pass
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
    return list(found.values())


def _now_utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _digest(nonce_b64, created, password):
    raw = base64.b64decode(nonce_b64) + created.encode("utf-8") + password.encode("utf-8")
    return base64.b64encode(hashlib.sha1(raw).digest()).decode("ascii")


def _envelope(body_ns, body_xml, user=None, pwd=None, nonce_b64=None, created=None):
    head = ""
    if user is not None:
        nonce = nonce_b64 or base64.b64encode(os.urandom(16)).decode("ascii")
        cr = created or _now_utc()
        dig = _digest(nonce, cr, pwd or "")
        head = (
            '<s:Header><Security s:mustUnderstand="1" '
            'xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">'
            "<UsernameToken>"
            "<Username>%s</Username>"
            '<Password Type="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-username-token-profile-1.0#PasswordDigest">%s</Password>'
            '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-soap-message-security-1.0#Base64Binary">%s</Nonce>'
            '<Created xmlns="http://docs.oasis-open.org/wss/2004/01/'
            'oasis-200401-wss-wssecurity-utility-1.0.xsd">%s</Created>'
            "</UsernameToken></Security></s:Header>"
        ) % (_xml_escape(user), dig, nonce, cr)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
        'xmlns:tt="http://www.onvif.org/ver10/schema">'
        "%s<s:Body>%s</s:Body></s:Envelope>"
    ) % (head, body_xml)


def _xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _post(url, xml, action, timeout=6.0):
    req = urllib.request.Request(url, data=xml.encode("utf-8"), method="POST")
    req.add_header("Content-Type", 'application/soap+xml; charset=utf-8; action="%s"' % action)
    req.add_header("User-Agent", "HomeEye/1.0")
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")


def _service_url(host, port):
    for path in ("/onvif/device_service", "/onvif/services", ""):
        yield "http://%s:%s%s" % (host, port, path)


def get_stream_uris(host, port=80, user="", pwd="", timeout=6.0):
    """取设备各码流的 RTSP 地址。

    返回 [{"profile": "Profile_1", "name": "主码流", "url": "rtsp://..."}]，
    失败返回 []。整个过程是非关键的辅助能力，任何异常都不应该影响主流程。
    """
    out = []
    last_err = ""
    for base in _service_url(host, int(port or 80)):
        try:
            # 第一次：带随机 nonce 的 digest
            xml = _envelope("trt", "<trt:GetProfiles/>", user=user, pwd=pwd)
            txt = _post(base, xml, "http://www.onvif.org/ver10/media/wsdl/GetProfiles", timeout)
            tokens = re.findall(r"<(?:trt:)?Profile[^>]*\btoken=\"([^\"]+)\"", txt)
            if not tokens:
                tokens = re.findall(r"<(?:tt:)?ProfileToken>([^<]+)<", txt)
            if not tokens:
                continue
            for idx, tk in enumerate(tokens):
                body = (
                    '<trt:GetStreamUri>'
                    "<trt:ProfileToken>%s</trt:ProfileToken>"
                    "<trt:StreamSetup>"
                    "<tt:Stream>RTP-Unicast</tt:Stream>"
                    "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
                    "</trt:StreamSetup>"
                    "</trt:GetStreamUri>"
                ) % _xml_escape(tk)
                xml2 = _envelope("trt", body, user=user, pwd=pwd)
                t2 = _post(base, xml2,
                           "http://www.onvif.org/ver10/media/wsdl/GetStreamUri", timeout)
                m = re.search(r"<(?:tt:)?Uri>([^<]+)<", t2)
                if not m:
                    m = re.search(r"<(?:trt:)?Uri>([^<]+)<", t2)
                if not m:
                    continue
                uri = m.group(1).strip()
                label = "主码流" if idx == 0 else ("子码流" if idx == 1 else ("码流 %d" % (idx + 1)))
                # 很多设备回的地址不含账号密码，这里补上，省得用户再手填
                if user and "@" not in uri:
                    mm = re.match(r"^(rtsp://)(.*)$", uri)
                    if mm:
                        uri = mm.group(1) + _uri_user(user) + ":" + _uri_pass(pwd) + "@" + mm.group(2)
                out.append({"profile": tk, "name": label, "url": uri})
            if out:
                return out
        except urllib.error.HTTPError as e:
            last_err = "HTTP %s" % e.code
            # 401/400 带 challenge：用 challenge 当 nonce 重发
            try:
                hdr = e.headers.get("WWW-Authenticate", "") or ""
                mn = re.search(r'nonce="([^"]+)"', hdr)
                if mn and user:
                    nonce = base64.b64encode(mn.group(1).encode()).decode()
                    xml = _envelope("trt", "<trt:GetProfiles/>", user=user, pwd=pwd,
                                    nonce_b64=nonce)
                    txt = _post(base, xml,
                                "http://www.onvif.org/ver10/media/wsdl/GetProfiles", timeout)
                    tokens = re.findall(r"<(?:trt:)?Profile[^>]*\btoken=\"([^\"]+)\"", txt)
                    if tokens:
                        return [{"profile": t, "name": "码流", "url": ""} for t in tokens]
            except Exception:
                pass
        except Exception as e:
            last_err = str(e)
    return out


# --------------------------------------------------------------------------
# 常见品牌的 RTSP 地址模板：ONVIF 取不到时兜底用（用户仍可手工改）
# --------------------------------------------------------------------------
BRAND_TEMPLATES = [
    # (关键字, 显示名, RTSP 路径模板)
    ("hikvision", "海康威视", "/Streaming/Channels/{ch}01"),
    ("dahua", "大华", "/cam/realmonitor?channel={ch}&subtype=0"),
    ("tp-link", "TP-Link", "/stream1"),
    ("joan", "乔安", "/live/ch{ch}"),
    ("reolink", "Reolink", "/h264Preview_01_main"),
    ("foscam", "Foscam", "/videoMain"),
    ("xm", "小米/小方", "/live"),
]


def guess_rtsp(host, port=80, user="", pwd="", channel=1):
    """按常见品牌模板拼一组候选 RTSP 地址，供用户在下拉里试。"""
    cands = []
    for key, label, tpl in BRAND_TEMPLATES:
        path = tpl.replace("{ch}", str(channel))
        auth = ""
        if user:
            auth = _uri_user(user) + ":" + _uri_pass(pwd) + "@"
        cands.append({"name": label, "url": "rtsp://%s%s:554%s" % (auth, host, path)})
    return cands


def _uri_user(u):
    try:
        from urllib.parse import quote
    except ImportError:                                   # pragma: no cover
        from urllib import quote                          # type: ignore
    return quote(u.encode("utf-8"), safe="")


def _uri_pass(p):
    try:
        from urllib.parse import quote
    except ImportError:                                   # pragma: no cover
        from urllib import quote                          # type: ignore
    return quote((p or "").encode("utf-8"), safe="")
