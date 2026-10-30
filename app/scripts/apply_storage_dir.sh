#!/bin/bash
# ============================================================================
# HomeEye · 把"录像存放目录"落到 config.json 里
#
# 谁调用：cmd/install_callback（安装向导填完）、cmd/config_callback（应用中心
# 里改配置）。飞牛把向导里每个 field 的名字原样变成环境变量，所以这里读
# $wizard_rec_dir。
#
# 为什么写 config.json 而不是留环境变量：飞牛只在安装/配置那一次把向导值注进来，
# 之后 start/status 不一定还看得见；而 HomeEye 自己按 mtime 重载配置，落盘的
# storage.dir 既能被服务立刻看到，又能在设置页里如实回显（"页面显示的就是生效的"
# 是这项目的规矩）。
#
# 三条不能破的规矩：
#   1. 钩子可能被重复执行 —— 同一个目录跑两次结果一样。
#   2. 目录写不进去就退回默认目录并说清楚，绝不"设置成功、从此不长录像"。
#   3. 只创建/改属主我们要用的那一级目录，不动用户已有的父目录。
# ============================================================================
set -u

MODE="${HE_APPLY_MODE:-install}"
VAR="${TRIM_PKGVAR:-/vol1/@appdata/homeeye}"
CONF="$VAR/config.json"
RUN_USER="${TRIM_USERNAME:-homeeye}"
RUN_GROUP="${TRIM_GROUPNAME:-homeeye}"
LOG="$VAR/info.log"
USERLOG="${TRIM_TEMP_LOGFILE:-}"
PY="${HE_PY:-$(command -v python3.12 || command -v python3)}"

say() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') [apply-storage:$MODE] $1" >>"$LOG" 2>/dev/null
  if [ -n "$USERLOG" ]; then
    echo "$1" >>"$USERLOG" 2>/dev/null
  fi
}

# 默认目录：飞牛为本应用开的共享目录（文件管理器里看得见那个）下的 recordings/。
# TRIM_DATA_SHARE_PATHS 是冒号分隔的，取第一个；没有就退到应用数据目录。
default_rec() {
  local s="${TRIM_DATA_SHARE_PATHS:-}"
  s="${s%%:*}"
  if [ -n "$s" ]; then
    printf '%s/recordings' "${s%/}"
  else
    printf '%s/recordings' "$VAR"
  fi
}

current_rec() {
  [ -f "$CONF" ] || return 0
  "$PY" - "$CONF" <<'PYEOF' 2>/dev/null
import json, sys
try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
    print((cfg.get("storage") or {}).get("dir") or "")
except Exception:
    print("")
PYEOF
}

write_conf() {
  "$PY" - "$CONF" "$1" <<'PYEOF'
import json, os, sys

conf, rec = sys.argv[1], sys.argv[2]
data = {}
if os.path.isfile(conf):
    try:
        with open(conf, encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict):
            data = loaded
    except Exception:
        # 配置坏了别覆盖：装这一步只负责 storage.dir，剩下的交给服务自己的兜底
        data = {}
st = data.get("storage")
if not isinstance(st, dict):
    st = {}
st["dir"] = rec
data["storage"] = st
os.makedirs(os.path.dirname(conf) or ".", exist_ok=True)
tmp = conf + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
os.replace(tmp, conf)
PYEOF
}

want="${wizard_rec_dir:-}"
# 去掉首尾空白（用户粘贴路径最常带的就是尾巴上一个空格）
want="${want#"${want%%[![:space:]]*}"}"
want="${want%"${want##*[![:space:]]}"}"
want="${want%/}"

if [ -z "$want" ]; then
  if [ "$MODE" = "config" ]; then
    # 配置向导留空 = 不改。已经存过就用已存的，没存过用默认。
    want="$(current_rec)"
  fi
  [ -n "$want" ] || want="$(default_rec)"
fi

if [ ! -d "$want" ]; then
  mkdir -p "$want" 2>/dev/null
  if [ "$(id -u 2>/dev/null || printf 1)" = "0" ]; then
    chown "$RUN_USER:$RUN_GROUP" "$want" 2>/dev/null || true
  fi
  chmod 750 "$want" 2>/dev/null || true
fi

if [ ! -w "$want" ]; then
  fb="$(default_rec)"
  if [ "$want" = "$fb" ]; then
    say "录像目录 $want 建不出来或写不进去 —— 装完先别录，看设置页的存储一栏为什么红"
    # 目录都不可写等于这台机的存储有问题，但不该把安装整个卡住：
    # 用户还能进去看健康检查里的原因。
    exit 0
  fi
  say "录像目录 $want 不可写（权限或卷没挂载），退回默认目录 $fb"
  want="$fb"
  mkdir -p "$want" 2>/dev/null
  [ "$(id -u 2>/dev/null || printf 1)" = "0" ] && chown "$RUN_USER:$RUN_GROUP" "$want" 2>/dev/null
fi

if write_conf "$want"; then
  [ "$(id -u 2>/dev/null || printf 1)" = "0" ] && [ -f "$CONF" ] && chown "$RUN_USER:$RUN_GROUP" "$CONF" 2>/dev/null
  free=$("$PY" -c "import shutil,sys;print(round(shutil.disk_usage(sys.argv[1]).free/1024.0**3,1))" "$want" 2>/dev/null || echo "?")
  say "录像目录 = $want（该卷剩余 ${free} GB）"
else
  say "写 $CONF 失败：录像目录 $want 没生效，装完去设置页里填一次"
  exit 0
fi
