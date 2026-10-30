#!/usr/bin/env bash
# run.sh — 跑一个京东脚本。不依赖青龙、不依赖 docker。
#
#   ./run.sh shufflewzc_faker2_main/jd_CheckCK.js
#   JITTER_MIN=10 ./run.sh .../jd_dpqd_sign.js    # 随机等 0~10 分钟再跑
#
# 干了四件事：单实例锁（flock）、抖动错峰、日志留档、超时兜底。
set -uo pipefail

APP_DIR=${APP_DIR:-/data/apps/jd-cron}
[ $# -ge 1 ] || { echo "usage: run.sh <script> [args...]"; exit 2; }
SCRIPT=$1; shift

name=$(basename "$SCRIPT"); name=${name%.js}; name=${name%.py}
mkdir -p "$APP_DIR/logs"

# 同一个脚本不允许并发（青龙默认是并发的，多账号叠加就是找死）
exec 9>"$APP_DIR/.lock.$name"
if ! flock -n 9; then
  echo "[skip] $name 还在跑"
  exit 0
fi

# 抖动：整点是风控最好抓的特征
JITTER_MIN=${JITTER_MIN:-0}
if [ "$JITTER_MIN" -gt 0 ]; then
  sleep $(( RANDOM % (JITTER_MIN * 60) ))
fi

# 加载环境变量（JD_COOKIE / 通知配置）
if [ -f "$APP_DIR/.env" ]; then
  set -a; . "$APP_DIR/.env"; set +a
fi

export NODE_PATH="$APP_DIR/scripts/node_modules:/usr/local/lib/node_modules"
export TZ=Asia/Shanghai

LOG="$APP_DIR/logs/${name}-$(date +%F).log"
{
  echo "===== $(date '+%F %T') start $SCRIPT $* ====="
  cd "$APP_DIR/scripts" || exit 1
  if [ "${SCRIPT%.py}" != "$SCRIPT" ]; then
    timeout "${TASK_TIMEOUT:-1800}" python3 "$SCRIPT" "$@"
  else
    timeout "${TASK_TIMEOUT:-1800}" node "$SCRIPT" "$@"
  fi
  rc=$?
  echo "===== $(date '+%F %T') end rc=$rc ====="
  exit $rc
} >> "$LOG" 2>&1
rc=$?

# 日志留 14 天
find "$APP_DIR/logs" -name '*.log' -mtime +14 -delete 2>/dev/null || true
exit $rc
