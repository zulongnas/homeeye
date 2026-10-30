#!/bin/sh
# HomeEye · 构建与启动（薄封装，方便手不用打两行）
#   sh docker/build.sh            构建并后台起
#   sh docker/build.sh stop       停
#   sh docker/build.sh logs       跟日志
#   sh docker/build.sh restart    重启
#   sh docker/build.sh status     看容器与健康检查
#   sh docker/build.sh down       连网络一起收掉（数据在 data/ 里，不会丢）
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# 有 docker compose（v2 子命令）就用它，退回 docker-compose（v1）
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  echo "没找到 docker compose / docker-compose"; exit 1
fi

case "${1:-up}" in
  up)
    echo "==> 构建镜像"
    $DC build
    echo "==> 启动容器"
    $DC up -d
    sleep 3
    $DC ps
    echo ""
    echo "浏览器打开  http://<这台机器的IP>:8088"
    echo "对讲要用   https://<这台机器的IP>:8443   （自签证书，点一次继续访问）"
    echo "第一件事是设登录密码；设完之前数据接口都是空的。"
    ;;
  stop)  $DC stop ;;
  down)  $DC down ;;
  logs)  $DC logs -f --tail=100 ;;
  restart) $DC restart ;;
  status)
    $DC ps
    echo "--- 健康 ---"
    curl -s -m 8 http://127.0.0.1:8088/api/health | head -c 600 || echo "（起不来？看 sh docker/build.sh logs）"
    echo ""
    ;;
  *)
    echo "用法: $0 [up|stop|down|logs|restart|status]"; exit 1 ;;
esac
