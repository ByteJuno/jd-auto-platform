#!/usr/bin/env bash
# sync_ck.sh — 把 ck.txt 里的账号同步进 .env 的 JD_COOKIE（按 pt_pin 去重）
#
#   ./sync_ck.sh             # 用同目录 ck.txt
#   ./sync_ck.sh other.txt
set -euo pipefail

APP_DIR=${APP_DIR:-/data/apps/jd-cron}
SRC=${1:-$APP_DIR/ck.txt}
[ -f "$SRC" ] || { echo "[x] 找不到 $SRC"; exit 1; }

python3 - "$SRC" "$APP_DIR/.env" <<'PY'
import re, sys

src, envf = sys.argv[1], sys.argv[2]
PIN, KEY = re.compile(r'pt_pin=([^;]+)'), re.compile(r'pt_key=([^;]+)')

acc = {}
for line in open(src, encoding='utf-8-sig'):
    line = line.strip().strip(',').strip('"')
    if not line or line.startswith('#'):
        continue
    p, k = PIN.search(line), KEY.search(line)
    if not p or not k:
        print('  !! 跳过不完整行:', line[:40])
        continue
    if len(k.group(1)) < 20:
        print('  !! 跳过被截断的 key:', p.group(1))
        continue
    acc[p.group(1)] = f'pt_key={k.group(1)};pt_pin={p.group(1)};'

if not acc:
    print('[x] 没解析到有效 ck')
    sys.exit(2)

lines = open(envf, encoding='utf-8').read().splitlines() if __import__('os').path.exists(envf) else []
out, found = [], False
for ln in lines:
    if ln.startswith('JD_COOKIE='):
        found = True
        old = [x for x in ln[len('JD_COOKIE='):].split('&') if x.strip()]
        merged = dict(acc)
        for ck in old:
            m = PIN.search(ck)
            if m and m.group(1) not in merged:
                merged[m.group(1)] = ck
        out.append('JD_COOKIE=' + '&'.join(merged.values()))
        print(f'[ok] 更新 JD_COOKIE: {len(merged)} 个账号 -> ' + ', '.join(merged))
    else:
        out.append(ln)
if not found:
    out.append('JD_COOKIE=' + '&'.join(acc.values()))
    print(f'[ok] 写入 JD_COOKIE: {len(acc)} 个账号 -> ' + ', '.join(acc))

open(envf, 'w', encoding='utf-8').write('\n'.join(out) + '\n')
PY
chmod 600 "$APP_DIR/.env"
echo "[ok] $APP_DIR/.env 已更新（权限 600）"
