# -*- coding: utf-8 -*-
"""HomeEye · 启动入口

只监听 127.0.0.1：对外访问一律走飞牛网关（自带登录态），
所以应用本身不需要再做一套账号体系，也不会被局域网随便直连。

进程模型：
  main.py  →  setsid 成为会话首进程（cmd/main 用负 PID 才能一次收掉
             自己和所有 ffmpeg 子进程）
"""
import os
import signal
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import nw_api  # noqa: E402
import nw_config as C  # noqa: E402

_httpd = None


def _bye(*_a):
    try:
        nw_api.shutdown_all()
    except Exception:
        pass
    # unix socket 下 serve_forever 的 select 无法被 shutdown() 可靠唤醒，
    # 清理（收 ffmpeg 子进程）已在 shutdown_all 里做完，这里直接硬退出。
    os._exit(0)


def main():
    global _httpd
    port = int(os.environ.get("HE_PORT") or 17071)
    host = os.environ.get("HE_HOST") or "127.0.0.1"
    sock = os.environ.get("GATEWAY_SOCKET") or ""

    # 独立进程组：父脚本按 -PID 一次性结束整棵树（Windows 上没有 setsid，跳过）
    if hasattr(os, "setsid"):
        try:
            os.setsid()
        except OSError:
            pass

    C.ensure_dirs()
    if sock:
        # 飞牛统一网关模式：监听 unix socket，不对外暴露 TCP 端口
        _httpd = nw_api.build_gateway_server(sock)
        sys.stderr.write("[homeeye] v%s gateway on unix://%s\n" % (nw_api.VERSION, sock))
    else:
        _httpd = nw_api.build_server(port, host)
        sys.stderr.write("[homeeye] v%s listening on http://%s:%d\n" % (nw_api.VERSION, host, port))
    sys.stderr.write("[homeeye] dest=%s var=%s rec=%s\n"
                     % (C.APPDEST, C.PKGVAR, C.recordings_root()))
    sys.stderr.flush()

    # 云台侧车（heptz）：小米云台/对讲走 miss 私有命令，官方 SDK 不开放，
    # 用独立小进程持有自己的一条 P2P 会话，与取流互不干扰。
    nw_api.start_sidecars()

    # 对讲要用麦克风，而浏览器只在安全上下文（HTTPS / localhost）下才给麦克风权限。
    # 局域网 http://ip:port 不是安全上下文，所以再挂一个 HTTPS 端口（默认 8443）。
    # 设 HE_TLS_PORT=0 可关掉。
    _tls = os.environ.get("HE_TLS_PORT")
    tls_port = 8443 if _tls is None else int(_tls or 0)
    if tls_port and not sock:
        try:
            cert, key = nw_api.ensure_selfsigned(os.path.join(C.PKGVAR, "tls"))
            if cert:
                _tlsd = nw_api.build_tls_server(tls_port, host, cert, key)
                threading.Thread(target=_tlsd.serve_forever,
                                 kwargs={"poll_interval": 0.5}, daemon=True).start()
                sys.stderr.write("[homeeye] HTTPS on https://%s:%d （对讲用麦克风需要它）\n"
                                 % (host, tls_port))
            else:
                sys.stderr.write("[homeeye] HTTPS 未启用：生成自签证书失败（缺 openssl？）\n")
        except Exception as e:
            sys.stderr.write("[homeeye] HTTPS 启动失败：%s\n" % e)

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _bye)
        except Exception:
            pass

    try:
        _httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        nw_api.shutdown_all()
        try:
            _httpd.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
