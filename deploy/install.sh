#!/usr/bin/env bash
# install.sh — 在 instabox (Debian 13) 上一键部署京东自动化平台。
#
#   sudo bash install.sh                 # 默认 /opt/jd-auto, 端口 5700, 只绑 tailscale IP
#   sudo QL_PORT=5710 BIND_IP=0.0.0.0 bash install.sh
#   sudo APP_DIR=/srv/jd-auto bash install.sh
#
# 幂等：重复跑不会重建数据；ql-data 已存在则跳过初始化。
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/jd-auto}
QL_PORT=${QL_PORT:-5700}
BIND_IP=${BIND_IP:-auto}
SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

c_ok()   { printf '\033[32m[ok]\033[0m %s\n' "$*"; }
c_info() { printf '\033[36m[..]\033[0m %s\n' "$*"; }
c_warn() { printf '\033[33m[!]\033[0m %s\n' "$*"; }
die()    { printf '\033[31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "用 root 跑: sudo bash install.sh"

# ---------------------------------------------------------------- 1. docker
c_info "检查 docker"
if ! command -v docker >/dev/null 2>&1; then
  c_info "没装 docker，走官方脚本安装（会拉 ~300MB）"
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || true
docker info >/dev/null 2>&1 || die "docker 守护进程没起来，先手动 systemctl start docker"
if ! docker compose version >/dev/null 2>&1; then
  c_info "装 compose 插件"
  apt-get update -qq && apt-get install -y -qq docker-compose-plugin
fi
c_ok "docker $(docker --version | awk '{print $3}' | tr -d ,)"

# ---------------------------------------------------------------- 2. 端口
if [ "$BIND_IP" = "auto" ]; then
  if command -v tailscale >/dev/null 2>&1 && TS_IP=$(tailscale ip -4 2>/dev/null | head -n1) && [ -n "$TS_IP" ]; then
    BIND_IP=$TS_IP
    c_ok "绑定 tailscale IP: $BIND_IP"
  else
    BIND_IP=0.0.0.0
    c_warn "没有 tailscale，绑 0.0.0.0 —— 记得用防火墙挡住 $QL_PORT"
  fi
fi

if ss -ltn 2>/dev/null | grep -q ":${QL_PORT} "; then
  # 自己的容器占着就不算冲突，否则重跑 install.sh 永远起不来
  if docker ps --filter "name=^ql$" --filter "publish=${QL_PORT}" --format '{{.Names}}' 2>/dev/null | grep -q '^ql$'; then
    c_info "端口 $QL_PORT 已被本项目的 ql 容器占用，继续（幂等重跑）"
  else
    die "端口 $QL_PORT 被别的进程占用，换一个: sudo QL_PORT=5710 bash install.sh"
  fi
fi

# ---------------------------------------------------------------- 3. 落盘
c_info "部署到 $APP_DIR"
mkdir -p "$APP_DIR"/{scripts,data,logs}
install -m 0755 "$SRC_DIR/install.sh" "$APP_DIR/install.sh" 2>/dev/null || true
cp -f "$SRC_DIR/docker-compose.yml" "$APP_DIR/docker-compose.yml"

# 脚本目录：本包里的 scripts/ 优先，否则从源码树同级目录找
if [ -d "$SRC_DIR/../scripts" ]; then
  cp -f "$SRC_DIR/../scripts/"*.py "$APP_DIR/scripts/" 2>/dev/null || true
fi
if [ -d "$SRC_DIR/scripts" ]; then
  cp -f "$SRC_DIR/scripts/"*.py "$APP_DIR/scripts/" 2>/dev/null || true
fi
[ -f "$APP_DIR/scripts/ql_bootstrap.py" ] || die "缺 scripts/*.py，确认整包解压后再跑"

# 面板只绑 $BIND_IP，所以宿主机脚本必须打这个地址，不能写 127.0.0.1
if [ "$BIND_IP" = "0.0.0.0" ]; then
  LOCAL_URL="http://127.0.0.1:$QL_PORT"
else
  LOCAL_URL="http://$BIND_IP:$QL_PORT"
fi

# .env 只写一次，别覆盖用户改过的密码
if [ ! -f "$APP_DIR/.env" ]; then
  cat > "$APP_DIR/.env" <<EOF
QL_PORT=$QL_PORT
BIND_IP=$BIND_IP
QL_URL=$LOCAL_URL
QL_USER=admin
QL_PASS=CHANGE_ME_AFTER_FIRST_LOGIN
NOTIFY_TYPE=
WEWORK_WEBHOOK=
EOF
  chmod 600 "$APP_DIR/.env"
  c_ok "生成 .env（权限 600）"
else
  c_info ".env 已存在，保留密码，只同步 BIND_IP/QL_URL"
  grep -q '^BIND_IP=' "$APP_DIR/.env" && sed -i "s|^BIND_IP=.*|BIND_IP=$BIND_IP|" "$APP_DIR/.env"
  if grep -q '^QL_URL=' "$APP_DIR/.env"; then
    sed -i "s|^QL_URL=.*|QL_URL=$LOCAL_URL|" "$APP_DIR/.env"
  else
    echo "QL_URL=$LOCAL_URL" >> "$APP_DIR/.env"
  fi
fi

cd "$APP_DIR"
chmod 700 "$APP_DIR"/ql-data 2>/dev/null || true

# ---------------------------------------------------------------- 4. 起服务
c_info "拉镜像并启动"
docker compose pull -q
docker compose up -d

c_info "等面板就绪（最多 120s，探测 $LOCAL_URL）"
for i in $(seq 1 40); do
  if curl -fsS "$LOCAL_URL/api/system" >/dev/null 2>&1; then
    c_ok "面板已响应（约 $((i * 3))s）"
    break
  fi
  sleep 3
  [ "$i" -eq 40 ] && c_warn "还没响应，docker compose logs -f 看下"
done

# ---------------------------------------------------------------- 4.5 node 依赖
# 青龙自带的"依赖管理"在部分机器上会 0 秒失败（apk/pnpm 拿不到），
# 结果就是任务日志里 Cannot find module 'axios'。这里直接装到持久目录。
c_info "安装 node 依赖（axios/ws/png-js/moment/dotenv/crypto-js/date-fns）"
docker exec ql sh -c '
  npm install -g --prefix /ql/data/dep_cache/node --no-audit --no-fund \
    axios ws png-js moment dotenv crypto-js date-fns >/tmp/dep.log 2>&1 || {
      echo "[!] npm 安装失败，见容器内 /tmp/dep.log"; }
  mkdir -p /ql/data/deps
  cp -r /ql/data/dep_cache/node/lib/node_modules/* /ql/data/deps/ 2>/dev/null || true
  ls /ql/data/deps | tr "\n" " "
'
echo
if docker exec ql sh -c 'test -d /ql/data/deps/axios'; then
  c_ok "依赖就位（/ql/data/deps，容器重建不丢）"
else
  c_warn "依赖没装上，任务会报 Cannot find module；手动跑上面的 npm 命令排查"
fi

# ---------------------------------------------------------------- 5. 收尾
HOST_FOR_URL=$BIND_IP
[ "$BIND_IP" = "0.0.0.0" ] && HOST_FOR_URL=$(hostname -I 2>/dev/null | awk '{print $1}')

cat <<EOF

==================== 部署完成 ====================
面板地址   http://${HOST_FOR_URL}:${QL_PORT}
数据目录   ${APP_DIR}/ql-data   （备份就打包这个）
日志       docker compose logs -f

接下来三步：
  1) 浏览器进面板，设置 admin 密码
  2) 改 ${APP_DIR}/.env 里的 QL_PASS 为同一个密码
  3) sudo bash -c 'set -a; . ${APP_DIR}/.env; set +a; cd ${APP_DIR}; python3 scripts/ql_bootstrap.py --prune'
     然后在面板「订阅管理」点一次运行，等 cron 自己跑

注意：
  * 容器内跑农场类脚本还要 apk 装 chromium/xvfb，那部分不走上面这步。
  * 拉库会自动塞 80+ 条任务（含互助/助力），--prune 会把它们清掉。
==================================================
EOF
