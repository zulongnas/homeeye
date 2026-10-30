#!/bin/sh
# 构建 HomeEye 定制版 go2rtc 镜像 + 云台侧车 heptz。
#
#   sh tools/build-go2rtc-ptz.sh
#   TAG=homeeye/go2rtc:1.1.3-ptz sh tools/build-go2rtc-ptz.sh
#
# 为什么要有这个脚本：小米云台控制与语音对讲走 Mi Home 的 miss 私有命令，官方
# go2rtc 不开放，所以我们在 upstream v1.9.15 上打了 15 个文件的补丁。补丁文件
# （third_party/go2rtc-ptz/homeeye-ptz.patch）就是改动的唯一来源，本脚本负责把它
# 应用到一个内容寻址、可校验的上游源码树，然后在 Docker 里编译 —— 宿主机不需要 Go。
#
# 为什么镜像在官方 master-hardware 上只替换一个二进制：那层里的 ffmpeg（带
# qsv/vaapi）、intel-media-driver、mesa-va-drivers、tini 是硬件转码要的；我们的
# 补丁只改 Go 源码，叠加是等价且最小的做法。从零构建很容易把驱动丢掉。
#
# ⚠️ 千万不要把编译好的二进制 bind mount 覆盖进**正在运行**的容器 —— 会 "Text file
#    busy"。改完要走镜像：换 compose 里的 image，再 up -d --force-recreate。
set -e

VER="${VER:-v1.9.15}"
# upstream tarball 的 sha256（2026-10-29 取自 codeload 的 refs/tags/v1.9.15）。
# 上游如果重新打 tag，这里会当场报错而不是悄悄编出别的东西 —— 那正是要的效果。
UPSTREAM_SHA256="${UPSTREAM_SHA256:-08901ecc03ec031b1af13268e6e62370ba7f887b30f98866624d683e0486c084}"
TAG="${TAG:-homeeye/go2rtc:1.1.3-ptz}"
BASE="${BASE:-alexxit/go2rtc:master-hardware}"
GOIMG="${GOIMG:-golang:1.26-alpine}"
GOPROXY_="${GOPROXY:-https://goproxy.cn,direct}"
SMOKE_PORT="${SMOKE_PORT:-19850}"
SMOKE_IN=1985

REPO="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
PATCH="$REPO/third_party/go2rtc-ptz/homeeye-ptz.patch"
WORK="${WORK:-/tmp/he-g2r}"
SRC="$WORK/src"

[ -f "$PATCH" ] || { echo "缺补丁文件：$PATCH"; exit 1; }

echo "== 1. 取上游源码并校验 =="
rm -rf "$WORK"
mkdir -p "$SRC" "$WORK/out" "$WORK/ctx/config"
URL="https://codeload.github.com/AlexxIT/go2rtc/tar.gz/refs/tags/$VER"
echo "   $URL"
curl -fsSL --retry 3 -o "$WORK/up.tgz" "$URL"
if command -v sha256sum >/dev/null 2>&1; then
  printf '%s  %s\n' "$UPSTREAM_SHA256" "$WORK/up.tgz" | sha256sum -c -
elif command -v shasum >/dev/null 2>&1; then
  printf '%s  %s\n' "$UPSTREAM_SHA256" "$WORK/up.tgz" | shasum -a 256 -c -
else
  echo "   !! 没有 sha256 工具，跳过校验（不推荐）"
fi
tar xzf "$WORK/up.tgz" -C "$SRC" --strip-components=1
# website/ 是文档站，编译不需要，删掉能少一堆噪声
rm -rf "$SRC/website"

echo "== 2. 应用 HomeEye 补丁 =="
if command -v git >/dev/null 2>&1; then
  ( cd "$SRC" && git apply --whitespace=nowarn "$PATCH" )
else
  patch -p1 -d "$SRC" < "$PATCH"
fi
# 补丁没打上就等于在编官方版本，那云台接口一定不存在 —— 当场停，别把锅留给运行时
[ -d "$SRC/cmd/heptz" ] || { echo "FAIL: 补丁没生效（没有 cmd/heptz）"; exit 1; }

echo "== 3. 在容器里编译两个二进制 =="
docker run --rm -v "$SRC":/src:ro -v "$WORK/out":/out -v /tmp/he-gocache:/go \
  -w /src -e GOPROXY="$GOPROXY_" -e CGO_ENABLED=0 -e GOFLAGS=-mod=mod \
  "$GOIMG" go build -trimpath -ldflags "-s -w" -o /out/go2rtc ./
docker run --rm -v "$SRC":/src:ro -v "$WORK/out":/out -v /tmp/he-gocache:/go \
  -w /src -e GOPROXY="$GOPROXY_" -e CGO_ENABLED=0 -e GOFLAGS=-mod=mod \
  "$GOIMG" go build -trimpath -ldflags "-s -w" -o /out/heptz ./cmd/heptz
ls -l "$WORK/out/go2rtc" "$WORK/out/heptz"
sha256sum "$WORK/out/go2rtc" "$WORK/out/heptz" 2>/dev/null || true

echo "== 4. 构建镜像 $TAG =="
docker image inspect "$BASE" >/dev/null 2>&1 || docker pull "$BASE"
cp "$WORK/out/go2rtc" "$WORK/ctx/go2rtc"
chmod 755 "$WORK/ctx/go2rtc"
cat > "$WORK/ctx/Dockerfile" <<EOF
FROM $BASE
COPY go2rtc /usr/local/bin/go2rtc
RUN chmod 755 /usr/local/bin/go2rtc
EOF
docker build -q -t "$TAG" "$WORK/ctx"
docker image inspect "$TAG" --format '   built: {{.Id}} size={{.Size}}'

echo "== 5. 冒烟回归：补丁接口必须在，且不能撞到你正在跑的 go2rtc =="
# 独立 bridge 容器 + 只绑 127.0.0.1 的临时端口，绝不碰 1984 上那个在跑的实例。
cat > "$WORK/ctx/config/go2rtc.yaml" <<EOF
api:
  listen: ":$SMOKE_IN"
EOF
docker rm -f he-g2r-smoke >/dev/null 2>&1 || true
docker run -d --name he-g2r-smoke -v "$WORK/ctx/config":/config \
  -p 127.0.0.1:$SMOKE_PORT:$SMOKE_IN "$TAG" >/dev/null
sleep 4
PTZ=$(curl -s --max-time 8 "http://127.0.0.1:$SMOKE_PORT/api/xiaomi/ptz" || echo "")
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "http://127.0.0.1:$SMOKE_PORT/api/xiaomi/motor?did=0&op=0" || echo 000)
STREAMS=$(curl -s --max-time 8 "http://127.0.0.1:$SMOKE_PORT/api/streams" | head -c 120)
docker rm -f he-g2r-smoke >/dev/null 2>&1 || true
echo "   /api/xiaomi/ptz   : $PTZ"
echo "   /api/xiaomi/motor : HTTP $code"
echo "   /api/streams      : $STREAMS"
case "$PTZ" in
  ""|*"not found"*|*"404"*) echo "FAIL: /api/xiaomi/ptz 不在 —— 这个镜像没带上云台补丁"; exit 1 ;;
esac
[ "$code" = "404" ] && { echo "FAIL: /api/xiaomi/motor 404，补丁没生效"; exit 1; }
[ -n "$STREAMS" ] || { echo "FAIL: /api/streams 空，本体没起来"; exit 1; }

echo "== 6. 出 heptz =="
# HomeEye 的镜像里 /app/bin/heptz 就是这个二进制。想把它固化进仓库，复制覆盖即可。
DEST="$REPO/app/bin/heptz"
if [ "${KEEP_BIN:-1}" = "1" ]; then
  cp "$WORK/out/heptz" "$DEST"
  chmod 755 "$DEST"
  echo "   heptz -> $DEST"
else
  echo "   heptz 留在 $WORK/out/heptz（KEEP_BIN=0）"
fi

echo ""
echo "OK: $TAG / heptz 已冒烟通过"
echo "下一步：把 docker/docker-compose.yml 里 go2rtc 的 image 改成 $TAG，"
echo "         然后 cd docker && docker compose up -d --force-recreate go2rtc"
echo "源码与逐条补丁说明见 third_party/go2rtc-ptz/（go2rtc 是 MIT，改动已在此披露）。"
