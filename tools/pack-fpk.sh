#!/bin/sh
# 出飞牛 fnOS 的 .fpk 安装包。
#
#   sh tools/pack-fpk.sh                 # 用 PATH 里或 ../ 下的 fnpack
#   FNPACK=/path/to/fnpack sh tools/pack-fpk.sh
#
# 仓库根本身就是 fnpack 的源目录（app/ + cmd/ + config/ + manifest + ICON*.png），
# 多余目录（docker/ docs/ tools/）不会进包 —— fnpack 只打 app.tgz 与生命周期脚本。
#
# 为什么用官方 fnpack 而不是自己 tar：它会在 manifest 末尾注入
# `checksum = md5(app.tgz)`，并且会按官方规则校验目录与入口 ID。手搓 tar 少算
# 一个 checksum 字段，装的时候才炸，那不值。
#
# fnpack 是飞牛官方命令行工具，许可与下载：https://developer.fnnas.com/docs/cli/fnpack/
# 本仓库不代它分发。
set -e

REPO="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$REPO"

FNPACK="${FNPACK:-}"
if [ -z "$FNPACK" ]; then
  for c in fnpack fnpack.exe ./fnpack ./fnpack.exe; do
    if command -v "$c" >/dev/null 2>&1 || [ -x "$c" ]; then FNPACK="$c"; break; fi
  done
fi
if [ -z "$FNPACK" ]; then
  cat <<'EOF'
找不到官方打包工具 fnpack。

  下载：https://developer.fnnas.com/docs/cli/fnpack/
  然后：FNPACK=/path/to/fnpack sh tools/pack-fpk.sh
EOF
  exit 1
fi

echo "== 1. 出包前体检 =="
fail=0
# 运行态与凭据：这些东西进包 = 把别人的家交给安装者
for p in data/config.json go2rtc/go2rtc.yaml; do
  if [ -e "$p" ]; then echo "   !! $p 存在，绝不能进包（先删或移出仓库）"; fail=1; fi
done
if find app -name "__pycache__" -o -name "*.py[co]" | grep -q .; then
  echo "   !! app/ 里有字节码（__pycache__/*.pyc），清掉再出包"; fail=1
fi
if find app -name "*.mp4" | grep -q .; then
  echo "   !! app/ 里有录像"; fail=1
fi
# 小米 SDK 不在许可允许再分发的范围内（NOTICE.md 第三节）
if [ -d app/sdk/miloco-sdk-main ]; then
  echo "   !! app/sdk/miloco-sdk-main 存在 —— 出公开包前先移走"; fail=1
fi
# manifest 不支持注释：# 开头的行会被当成垃圾键
if grep -nE "^\s*#" manifest; then
  echo "   !! manifest 里有 # 注释行（见上），删掉"; fail=1
fi
# 生命周期脚本依赖包里的这个助手；漏了它，安装向导填了也不落地
[ -f app/scripts/apply_storage_dir.sh ] || \
  { echo "   !! 缺 app/scripts/apply_storage_dir.sh（cmd/install_callback 要用）"; fail=1; }
# 向导文件是 JSON：语法错一个字符，飞牛的安装界面就是一片空白，而且报错很难查。
# Windows 上 `python3` 常常是应用商店的执行别名（跑起来什么都不打印就返回非 0），
# 所以先真的执行一次再决定用不用；找不到能用的解释器就明说，别假装体检过了。
PYC=""
for c in python3 python /usr/bin/python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c "import sys" >/dev/null 2>&1; then
    PYC="$c"; break
  fi
done
if [ -n "$PYC" ]; then
  for w in wizard/*; do
    [ -f "$w" ] || continue
    "$PYC" - "$w" <<'PYEOF' || fail=1
import json, sys
p = sys.argv[1]
try:
    steps = json.load(open(p, encoding="utf-8"))
except Exception as e:
    print("   !! %s 不是合法 JSON：%s" % (p, e)); sys.exit(1)
if not isinstance(steps, list) or not steps:
    print("   !! %s 要是非空数组" % p); sys.exit(1)
for s in steps:
    for it in s.get("items") or []:
        f = it.get("field")
        if f and not str(f).startswith("wizard_"):
            # 飞牛把 field 名原样变成环境变量；不带 wizard_ 前缀迟早跟人撞名
            print("   !! %s 的 field=%s 少了 wizard_ 前缀" % (p, f)); sys.exit(1)
print("   向导 %s ok" % p)
PYEOF
  done
else
  echo "   （没有 python，跳过向导 JSON 语法检查）"
fi
grep -q "^appname" manifest || { echo "   !! manifest 缺 appname"; fail=1; }
grep -q "^version" manifest || { echo "   !! manifest 缺 version"; fail=1; }
# 图标尺寸是审核项：64 与 256，正方形
for ic in ICON.PNG ICON_256.PNG; do
  [ -f "$ic" ] || { echo "   !! 缺 $ic"; fail=1; }
done
[ "$fail" = "0" ] || { echo "体检没过，不出包。"; exit 1; }
grep -qE "^version\s*=\s*[0-9]" manifest && \
  echo "   manifest: $(grep -m1 '^version' manifest) / $(grep -m1 '^platform' manifest)"

echo "== 2. fnpack build =="
"$FNPACK" build -d .
OUT="$(ls -t ./*.fpk 2>/dev/null | head -1)"
[ -f "$OUT" ] || { echo "fnpack 说成功了但没找到 .fpk"; exit 1; }

echo "== 3. 出包后复核 =="
echo "   文件：$OUT ($(wc -c < "$OUT") bytes)"
if tar tzf "$OUT" | grep -E "__pycache__|\.pyc$|config\.json|go2rtc\.yaml|\.mp4$|miloco"; then
  echo "   !! 包里出现了不该有的东西 —— 别分发这个包"; exit 1
fi
echo "   成员：$(tar tf "$OUT" | tr '\n' ' ')"
echo "   checksum 行：$(tar xzf "$OUT" -O manifest | grep '^checksum' || echo '(缺！)')"

echo ""
echo "OK: $OUT"
echo "本地试装（官方定位是调试用途，公开分发请走应用中心上架，见 docs/DEPLOY-FNOS.md）"
echo "真机验证清单：docs/DEPLOY-FNOS.md 最后一节"
