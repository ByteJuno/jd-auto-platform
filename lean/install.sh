#!/usr/bin/env bash
# install.sh — 极简版：没有青龙、没有 docker、没有面板，只有 systemd timer + node。
#
#   sudo bash install.sh                    # 默认 /data/apps/jd-cron
#   sudo APP_DIR=/opt/jd-cron bash install.sh
#
# 它干的事：拉脚本 → 装 node 依赖 → 建 .env → 生成 5 个 systemd timer → 启用。
# 幂等：重复跑只更新脚本和单元，不覆盖 .env。
set -euo pipefail

APP_DIR=${APP_DIR:-/data/apps/jd-cron}
REPO=${REPO:-https://github.com/shufflewzc/faker2.git}
BRANCH=${BRANCH:-main}
REPO_DIR=${REPO_DIR:-shufflewzc_faker2_main}
OWNER=${OWNER:-$(logname 2>/dev/null || echo root)}

ok()   { printf '\033[32m[ok]\033[0m %s\n' "$*"; }
info() { printf '\033[36m[..]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "用 root 跑: sudo bash install.sh"
command -v node >/dev/null || die "宿主没 node，先装: apt install -y nodejs npm"
command -v systemctl >/dev/null || die "没有 systemd，改用手写 crontab"

# ---------------------------------------------------------------- 1. 目录
info "部署到 $APP_DIR"
mkdir -p "$APP_DIR"/{scripts,logs,tools,ck_sessions}
HERE=$(dirname "$(readlink -f "$0")")
for f in run.sh sync_ck.sh panel.py; do
  [ -f "$HERE/$f" ] && cp -f "$HERE/$f" "$APP_DIR/$f" 2>/dev/null || true
done
chmod +x "$APP_DIR"/run.sh "$APP_DIR"/sync_ck.sh 2>/dev/null || true
# ck_qr.js 单独放 tools/，它的 puppeteer-core 装在同一目录下
[ -f "$HERE/ck_qr.js" ] && cp -f "$HERE/ck_qr.js" "$APP_DIR/tools/ck_qr.js" 2>/dev/null || true

# ---------------------------------------------------------------- 2. 脚本
if [ -d "$APP_DIR/scripts/$REPO_DIR/.git" ]; then
  info "更新已有仓库"
  git -C "$APP_DIR/scripts/$REPO_DIR" fetch --depth 1 origin "$BRANCH" -q \
    && git -C "$APP_DIR/scripts/$REPO_DIR" reset --hard "origin/$BRANCH" -q
else
  info "克隆 $REPO"
  rm -rf "$APP_DIR/scripts/$REPO_DIR"
  git clone --depth 1 -b "$BRANCH" -q "$REPO" "$APP_DIR/scripts/$REPO_DIR"
fi
ok "脚本: $(ls "$APP_DIR/scripts/$REPO_DIR" | wc -l) 个文件"

# ---------------------------------------------------------------- 3. 依赖
info "安装 node 依赖（宿主机直装，不经过任何面板）"
# got 必须锁 11.x：sendNotify.js 用的是 got v11 的 CommonJS 接口，
# 装上 got@14 会直接 `got.extend is not a function`。
( cd "$APP_DIR/scripts" && npm install --legacy-peer-deps --no-audit --no-fund \
    axios ws png-js moment dotenv crypto-js date-fns jsdom undici got@11 >/tmp/jd-npm.log 2>&1 ) \
  || warn "npm 失败，见 /tmp/jd-npm.log"
[ -d "$APP_DIR/scripts/node_modules/axios" ] && ok "依赖就位" || warn "axios 没装上，任务会报 Cannot find module"

# ---------------------------------------------------------------- 4. 环境变量
if [ ! -f "$APP_DIR/.env" ]; then
  cat > "$APP_DIR/.env" <<'EOF'
# 京东 CK，多账号用 & 连接。用 ./sync_ck.sh 从 ck.txt 生成，别手拼。
JD_COOKIE=
# 通知（二选一，留空就只在日志里看）
NOTIFY_TYPE=
WEWORK_WEBHOOK=
TG_BOT_TOKEN=
TG_CHAT_ID=
EOF
  chmod 600 "$APP_DIR/.env"
  ok "生成 .env（记得填 JD_COOKIE）"
else
  info ".env 已存在，保留"
fi
chown -R "$OWNER":"$OWNER" "$APP_DIR" 2>/dev/null || true
chmod 600 "$APP_DIR/.env"

# ---------------------------------------------------------------- 5. 定时器
# 格式: 名字|OnCalendar|提前多久随机抖动(秒)|脚本
TASKS=(
  "checkck|*-*-* 07:30:00|600|$REPO_DIR/jd_CheckCK.js"
  "sign|*-*-* 09:08:00|600|$REPO_DIR/jd_dpqd_sign.js"
  "beanhome|*-*-* 10:23:00|900|$REPO_DIR/jd_bean_home.js"
  "onceapply|*-*-* 11:55:00|900|$REPO_DIR/jd_OnceApply.js"
  "beanchange|*-*-* 21:40:00|600|$REPO_DIR/jd_bean_change.js"
)

for t in "${TASKS[@]}"; do
  IFS='|' read -r key cal delay script <<< "$t"
  cat > "/etc/systemd/system/jd-$key.service" <<EOF
[Unit]
Description=JD task $key ($script)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$OWNER
WorkingDirectory=$APP_DIR
Environment=APP_DIR=$APP_DIR
ExecStart=$APP_DIR/run.sh $script
Nice=10
TimeoutStartSec=2400
EOF
  cat > "/etc/systemd/system/jd-$key.timer" <<EOF
[Unit]
Description=Timer for JD task $key

[Timer]
OnCalendar=$cal Asia/Shanghai
RandomizedDelaySec=$delay
AccuracySec=30s
Persistent=true
Unit=jd-$key.service

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now "jd-$key.timer" >/dev/null 2>&1
  ok "jd-$key.timer  ($cal + 抖动≤$((delay/60))min)"
done

systemctl daemon-reload

# ---------------------------------------------------------------- 6. 看板 + 扫码
info "装看板依赖（puppeteer-core，配合系统 chromium）"
( cd "$APP_DIR/tools" && npm install --no-audit --no-fund puppeteer-core >/tmp/jd-pup.log 2>&1 ) \
  || warn "puppeteer-core 装失败，见 /tmp/jd-pup.log"
command -v chromium >/dev/null && ok "chromium: $(command -v chromium)" \
  || warn "没 chromium，扫码功能不可用：apt install -y chromium"

# 任务清单给 panel.py 读
python3 - "$APP_DIR" "${TASKS[@]}" <<'PY'
import json, sys, re
app, rows = sys.argv[1], sys.argv[2:]
out = []
labels = {"checkck": "京东CK检测", "sign": "京东店铺签到", "beanhome": "京东升级赚京豆",
          "onceapply": "京东一键价保", "beanchange": "京东资产统计"}
for r in rows:
    key, cal, delay, script = r.split("|")
    hhmm = re.search(r"(\d{2}:\d{2}):00", cal)
    out.append({"id": key, "name": labels.get(key, key), "script": script,
                "cal": "每天 " + (hhmm.group(1) if hhmm else cal),
                "unit": f"jd-{key}.service", "timer": f"jd-{key}.timer"})
json.dump(out, open(f"{app}/tasks.json", "w"), ensure_ascii=False, indent=1)
print(f"  tasks.json: {len(out)} 条")
PY

# 面板密码：没有就生成一个，写进 .env
if ! grep -q '^PANEL_PASS=' "$APP_DIR/.env" 2>/dev/null; then
  # 注意：别用 tr|head 生成，pipefail 下 head 提前退出会让整条管道失败、set -e 直接中断
  PW=$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')
  {
    echo "PANEL_USER=admin"
    echo "PANEL_PASS=$PW"
    echo "PANEL_PORT=${PANEL_PORT:-8088}"
    echo "PANEL_BIND=${PANEL_BIND:-}"
  } >> "$APP_DIR/.env"
  ok "面板密码已生成，写在 $APP_DIR/.env 的 PANEL_PASS"
fi
chmod 600 "$APP_DIR/.env"

# 监听地址：优先 tailscale，其次第一块网卡
BIND_VAL=$(grep '^PANEL_BIND=' "$APP_DIR/.env" | cut -d= -f2-)
if [ -z "$BIND_VAL" ]; then
  BIND_VAL=$(tailscale ip -4 2>/dev/null | head -1)
  [ -z "$BIND_VAL" ] && BIND_VAL=$(hostname -I | awk '{print $1}')
  sed -i "s|^PANEL_BIND=.*|PANEL_BIND=$BIND_VAL|" "$APP_DIR/.env"
fi
PORT_VAL=$(grep '^PANEL_PORT=' "$APP_DIR/.env" | cut -d= -f2-)

# 面板只能通过 sudo 起/停自己的这几个单元
cat > /etc/sudoers.d/jd-panel <<EOF
$OWNER ALL=(root) NOPASSWD: /usr/bin/systemctl start --no-block jd-*.service
$OWNER ALL=(root) NOPASSWD: /bin/systemctl start --no-block jd-*.service
EOF
chmod 440 /etc/sudoers.d/jd-panel

cat > /etc/systemd/system/jd-panel.service <<EOF
[Unit]
Description=JD panel (dashboard + QR login)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$OWNER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
Environment=APP_DIR=$APP_DIR
Environment=PATH=/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/python3 $APP_DIR/panel.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now jd-panel.service >/dev/null 2>&1
sleep 2
systemctl is-active --quiet jd-panel.service && ok "jd-panel 已启动" || warn "jd-panel 没起来：journalctl -u jd-panel -n 30"

cat <<EOF

==================== 装完了（无青龙版）====================
目录     $APP_DIR
看板     http://$BIND_VAL:$PORT_VAL   账号 admin / 密码见 .env 的 PANEL_PASS
脚本     $APP_DIR/scripts/$REPO_DIR
日志     $APP_DIR/logs/<脚本名>-<日期>.log
账号     进看板点「添加账号（扫码）」，手机京东 APP 扫一下；扫不动就手动粘 cookie

命令：
  systemctl list-timers 'jd-*'          # 下次跑点
  systemctl start jd-checkck.service    # 立刻跑一次
  journalctl -u jd-panel -f             # 看板日志
  sudo $APP_DIR/run.sh $REPO_DIR/jd_CheckCK.js   # 命令行单跑
=========================================================
EOF
