#!/bin/sh
# 取小米 Miloco SDK 到 app/sdk/miloco-sdk-main。
#
#   sh tools/fetch-miloco-sdk.sh
#   GIT_REF=<tag 或 commit> sh tools/fetch-miloco-sdk.sh     # 想固定版本就传
#
# 为什么不直接把 SDK 打进本仓库：它自述"基于 Xiaomi Miloco 开源框架开发，因此必须
# 遵守 Xiaomi Miloco License Agreement"，里面还有小米的预编译原生库
# libmiot_camera_lite.so。那份许可不是 OSI 开源许可，再分发权利未经核实 ——
# 所以由使用者自己从上游取，条款由你直接对小米负责。本仓库不代你接受它。
#
# 不需要小米摄像头的话，这个脚本不用跑：RTSP / ONVIF / 其他流都不依赖 SDK，
# 设置页只会把"小米"那一栏标成不可用，别的路照常工作。
set -e

UPSTREAM="${UPSTREAM:-https://github.com/dairoot/miloco-sdk.git}"
GIT_REF="${GIT_REF:-main}"
REPO="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
DEST="$REPO/app/sdk/miloco-sdk-main"

if [ "${AGREE:-0}" != "1" ]; then
  cat <<'EOF'
接下来要下载的是小米 Miloco SDK，受 Xiaomi Miloco License Agreement 约束，
不是可自由再分发的开源许可。继续即表示你已阅读并接受那份许可对自己的约束，
并且你下载它是为了接入你自己拥有或有权使用的设备。

  条款原文（上游仓库内）：https://github.com/dairoot/miloco-sdk
EOF
  printf '同意就输入 yes：'
  read -r ans
  [ "$ans" = "yes" ] || { echo "已取消。"; exit 1; }
fi

command -v git >/dev/null 2>&1 || { echo "需要 git"; exit 1; }

if [ -d "$DEST/.git" ]; then
  echo "== 已存在，更新 $DEST =="
  git -C "$DEST" fetch --tags origin
  git -C "$DEST" checkout "$GIT_REF"
  git -C "$DEST" reset --hard "origin/$GIT_REF" 2>/dev/null || true
else
  echo "== clone $UPSTREAM ($GIT_REF) -> $DEST =="
  rm -rf "$DEST"
  mkdir -p "$REPO/app/sdk"
  git clone --depth 1 --branch "$GIT_REF" "$UPSTREAM" "$DEST" 2>/dev/null \
    || { git clone "$UPSTREAM" "$DEST"; git -C "$DEST" checkout "$GIT_REF"; }
fi

SHA="$(git -C "$DEST" rev-parse HEAD)"
echo "   commit = $SHA      （要可复现，就把这个 sha 记下来，下次 GIT_REF 传它）"

# 原生库必须可执行，否则非 root 进程 dlopen 失败，表现是"小米一直 0 帧"
find "$DEST" -name "*.so*" -exec chmod a+rx {} + 2>/dev/null || true

cat <<EOF

SDK 就位。现在重新构建镜像（要顺带装它的 Python 依赖）：

  docker compose -f docker/docker-compose.yml build --build-arg HE_WITH_MILOCO_SDK=1 homeeye
  docker compose -f docker/docker-compose.yml up -d

或直接编辑 docker/docker-compose.yml，把 build.args 那两行注释放开。
然后浏览器里：设置 → 摄像头 → 「＋ 添加小米摄像头」扫码授权。
EOF
