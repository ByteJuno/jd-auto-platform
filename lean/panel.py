#!/usr/bin/env python3
"""panel.py — 一个页面的看板 + 扫码加账号。只用标准库。

能干的事：
  1. 列出所有 jd-* 任务：下次跑点、上次结果、耗时
  2. 点一下立刻跑（systemctl start --no-block）
  3. 看某个任务的日志尾巴
  4. 扫码添加京东账号：起一个无头 chromium 渲染京东登录页的二维码，
     手机扫完自动把 pt_key/pt_pin 写进 .env（不碰密码）
  5. 手动粘贴 cookie 兜底

环境变量（systemd 单元注入）：
  APP_DIR / PANEL_BIND / PANEL_PORT / PANEL_USER / PANEL_PASS
"""
import base64
import hmac
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

APP_DIR = os.environ.get("APP_DIR", "/data/apps/jd-cron")
BIND = os.environ.get("PANEL_BIND", "127.0.0.1")
PORT = int(os.environ.get("PANEL_PORT", "8088"))
USER = os.environ.get("PANEL_USER", "admin")
PASS = os.environ.get("PANEL_PASS", "")
LOGS = os.path.join(APP_DIR, "logs")
SESS_ROOT = os.path.join(APP_DIR, "ck_sessions")
ENV_FILE = os.path.join(APP_DIR, ".env")

if not PASS:
    raise SystemExit("[x] PANEL_PASS 没设置，拒绝以空密码启动")


# ---------------------------------------------------------------- 任务状态

def tasks():
    try:
        with open(os.path.join(APP_DIR, "tasks.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def read_env():
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def ck_accounts():
    for line in read_env():
        if line.startswith("JD_COOKIE="):
            return re.findall(r"pt_pin=([^;&]+)", line)
    return []


def newest_log(script):
    name = os.path.basename(script).rsplit(".", 1)[0]
    best, best_m = None, 0.0
    try:
        for fn in os.listdir(LOGS):
            if fn.startswith(name + "-") and fn.endswith(".log"):
                m = os.path.getmtime(os.path.join(LOGS, fn))
                if m > best_m:
                    best, best_m = fn, m
    except FileNotFoundError:
        pass
    return best


def tail(path, lines=200):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 96 * 1024))
            data = f.read().decode("utf-8", "replace").splitlines()
        return "\n".join(data[-lines:])
    except Exception as e:
        return f"(读不到日志: {e})"


def parse_run(path):
    info = {"start": None, "end": None, "rc": None, "cost": None}
    if not path:
        return info
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"===== (\S+ \S+) start", line)
                if m:
                    info["start"] = m.group(1)
                m = re.match(r"===== (\S+ \S+) end rc=(\d+)", line)
                if m:
                    info["end"], info["rc"] = m.group(1), int(m.group(2))
    except Exception:
        return info
    if info["start"] and info["end"]:
        try:
            a = datetime.strptime(info["start"], "%Y-%m-%d %H:%M:%S")
            b = datetime.strptime(info["end"], "%Y-%m-%d %H:%M:%S")
            info["cost"] = int((b - a).total_seconds())
        except Exception:
            pass
    return info


def timer_next(unit):
    try:
        out = subprocess.run(["systemctl", "list-timers", unit, "--no-pager", "--no-legend"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        parts = out.split("\n")[0].split()
        return " ".join(parts[:3]) if parts else "-"
    except Exception:
        return "-"


def state():
    rows = []
    for t in tasks():
        lg = newest_log(t["script"])
        full = os.path.join(LOGS, lg) if lg else None
        run = parse_run(full)
        rows.append({**t, "log": lg, "last_start": run.get("start"),
                     "rc": run.get("rc"), "cost": run.get("cost"),
                     "next": timer_next(t["timer"])})
    return {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "accounts": ck_accounts(), "tasks": rows}


def run_task(tid):
    t = next((x for x in tasks() if x["id"] == tid), None)
    if not t:
        return False, "没有这个任务"
    try:
        r = subprocess.run(["sudo", "-n", "systemctl", "start", "--no-block", t["unit"]],
                           capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return False, (r.stderr or r.stdout).strip()[:300]
        return True, f"{t['name']} 已排队执行"
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------- CK 写入

def merge_ck(pt_pin, pt_key):
    """按 pt_pin 去重写回 .env。返回现在总共几个账号。"""
    if not pt_pin or not pt_key or len(pt_key) < 20:
        raise ValueError("ck 不完整或被截断")
    ck = f"pt_key={pt_key};pt_pin={pt_pin};"
    lines = read_env()
    out, found, total = [], False, 0
    for ln in lines:
        if ln.startswith("JD_COOKIE="):
            found = True
            merged = {}
            for item in [x for x in ln[len("JD_COOKIE="):].split("&") if x.strip()]:
                m = re.search(r"pt_pin=([^;&]+)", item)
                if m:
                    merged[m.group(1)] = item
            merged[pt_pin] = ck
            total = len(merged)
            out.append("JD_COOKIE=" + "&".join(merged.values()))
        else:
            out.append(ln)
    if not found:
        total = 1
        out.append("JD_COOKIE=" + ck)
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.chmod(ENV_FILE, 0o600)
    return total


def parse_cookie_blob(text):
    """从粘贴的一坨里挑出 pt_key / pt_pin。"""
    pk = re.search(r"pt_key=([^;,\s]+)", text)
    pp = re.search(r"pt_pin=([^;&,\s]+)", text)
    if not pk or not pp:
        raise ValueError("没找到 pt_key / pt_pin")
    return pp.group(1), pk.group(1)


# ---------------------------------------------------------------- 扫码会话

_active = {}   # sid -> 起始时间戳，避免用户连点造成多次登录


def start_qr():
    now = time.time()
    # 已经有一个还在等扫的会话就复用它，别连扫（连扫最容易触发登录保护）。
    # 判断依据是它自己的状态：还活着（starting/qr）且刚更新过，才复用。
    for sid in list(_active):
        st = qr_status(sid)
        age = now - (st.get("ts", 0) / 1000.0 if st.get("ts") else now)
        if st.get("state") in ("starting", "qr") and age < 15:
            return sid
        _active.pop(sid, None)
    os.makedirs(SESS_ROOT, exist_ok=True)
    # 上一个会话可能卡死（浏览器 profile 被占着），先清干净再起新的
    subprocess.run(["pkill", "-f", "tools/ck_qr.js"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        os.unlink(os.path.join(SESS_ROOT, ".ck_qr.lock"))
    except OSError:
        pass
    sid = uuid.uuid4().hex[:12]
    d = os.path.join(SESS_ROOT, sid)
    os.makedirs(d, exist_ok=True)
    node = shutil.which("node") or "/usr/local/bin/node"
    subprocess.Popen([node, os.path.join(APP_DIR, "tools", "ck_qr.js"), d],
                     cwd=APP_DIR, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    _active[sid] = now
    return sid


def qr_status(sid):
    d = os.path.join(SESS_ROOT, sid)
    st = {"state": "starting"}
    try:
        with open(os.path.join(d, "status.json"), encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        pass
    if st.get("state") == "ok":
        try:
            with open(os.path.join(d, "ck.json"), encoding="utf-8") as f:
                ck = json.load(f)
            n = merge_ck(ck["pt_pin"], ck["pt_key"])
            st = {"state": "ok", "msg": f"已添加 {ck['pt_pin']}，共 {n} 个账号",
                  "pin": ck["pt_pin"]}
            shutil.rmtree(d, ignore_errors=True)
        except Exception as e:
            st = {"state": "error", "msg": f"写入失败: {e}"}
    return st


# ---------------------------------------------------------------- 页面

PAGE = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="color-scheme" content="dark light">
<title>JD 任务看板</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
/* 深色是默认，浅色跟随系统（prefers-color-scheme）。所有颜色都走变量。 */
:root{color-scheme:dark light;
      --bg:#0f1115;--card:#171a21;--line:#2a2f3a;--txt:#e6e9ef;--dim:#8b93a7;
      --ok:#3ddc97;--bad:#ff5d5d;--acc:#5b9dff;
      --btn:#232936;--code-bg:#0b0d11;--code-txt:#c9d1e0;--backdrop:#000a;--qrbg:#fff}
@media (prefers-color-scheme:light){
  :root{--bg:#f5f6f8;--card:#ffffff;--line:#e1e5ec;--txt:#1b2129;--dim:#5d6674;
        --ok:#0e8f5b;--bad:#d22f2f;--acc:#1f6feb;
        --btn:#eef1f6;--code-bg:#f4f6f9;--code-txt:#242c37;--backdrop:#0003;--qrbg:#fff}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);
     font:14px/1.5 -apple-system,"Segoe UI",Roboto,"Noto Sans SC",sans-serif;
     -webkit-font-smoothing:antialiased}
header{display:flex;align-items:center;gap:12px;padding:16px 22px;border-bottom:1px solid var(--line);flex-wrap:wrap}
h1{font-size:16px;margin:0;font-weight:600}
.pill{font-size:12px;color:var(--dim);border:1px solid var(--line);border-radius:99px;padding:2px 10px}
.pill b{color:var(--txt)}
main{padding:18px 22px;max-width:1100px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:11px 14px;text-align:left;border-bottom:1px solid var(--line);vertical-align:middle}
th{font-size:12px;color:var(--dim);font-weight:500;letter-spacing:.5px}
tr:last-child td{border-bottom:none}
td.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:var(--dim)}
.ok{color:var(--ok)}.bad{color:var(--bad)}
button{background:var(--btn);color:var(--txt);border:1px solid var(--line);border-radius:7px;
       padding:6px 12px;cursor:pointer;font-size:13px;font-family:inherit}
button:hover{border-color:var(--acc);color:var(--acc)}
button:disabled{opacity:.45;cursor:default}
pre{background:var(--code-bg);border:1px solid var(--line);border-radius:10px;padding:14px;
    max-height:52vh;overflow:auto;font:12px/1.55 ui-monospace,Consolas,monospace;color:var(--code-txt);white-space:pre-wrap}
dialog{background:var(--card);color:var(--txt);border:1px solid var(--line);border-radius:12px;
       width:min(560px,94vw);padding:18px}
dialog::backdrop{background:var(--backdrop)}
.row{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;gap:10px}
.muted{color:var(--dim);font-size:12px}
#qrimg{width:180px;height:180px;background:var(--qrbg);border-radius:8px;image-rendering:pixelated;display:block;margin:10px auto}
textarea{width:100%;height:110px;background:var(--code-bg);color:var(--code-txt);border:1px solid var(--line);
         border-radius:8px;padding:10px;font:12px/1.5 ui-monospace,Consolas,monospace;resize:vertical}
#ckMsg{text-align:center;font-size:13px;margin-top:6px}
</style></head><body>
<header>
  <h1>JD 任务看板</h1>
  <span class="pill">账号 <b id="acc">-</b></span>
  <span class="pill">刷新 <b id="ts">-</b></span>
  <span class="pill" id="msg"></span>
  <span style="margin-left:auto">
    <button onclick="openCk()">添加账号（扫码）</button>
  </span>
</header>
<main>
  <table>
    <thead><tr><th>任务</th><th>计划</th><th>下次</th><th>上次</th><th>结果</th><th></th></tr></thead>
    <tbody id="rows"><tr><td colspan="6" class="muted">加载中…</td></tr></tbody>
  </table>
  <p class="muted">点“跑一次”立刻执行；日志按钮看最近 200 行。页面每 20 秒自刷。</p>
</main>

<dialog id="dlg">
  <div class="row"><b id="dlgTitle">日志</b>
    <span><button onclick="loadLog()">刷新</button><button onclick="dlg.close()">关闭</button></span></div>
  <pre id="dlgBody">…</pre>
</dialog>

<dialog id="ckdlg">
  <div class="row"><b>扫码添加京东账号</b><button onclick="closeCk()">关闭</button></div>
  <p class="muted" style="margin:0 0 6px">手机京东 APP → 左上角「扫一扫」→ 确认登录。扫完自动写入，不碰你的密码。<br>
     二维码约 2 分钟失效，这里会自动换新码，不用关掉重开。</p>
  <img id="qrimg" alt="二维码加载中">
  <div id="ckMsg" class="muted">正在拉起浏览器…</div>
  <details style="margin-top:12px">
    <summary class="muted" style="cursor:pointer">扫不出来？手动粘贴 cookie</summary>
    <textarea id="ckPaste" placeholder="pt_key=AAJ...;pt_pin=jd_xxx;   （从浏览器 F12 → Application → Cookies → jd.com 复制）"></textarea>
    <div style="text-align:right;margin-top:8px"><button onclick="savePaste()">保存</button></div>
  </details>
</dialog>

<script>
const dlg = document.getElementById('dlg');
const ckdlg = document.getElementById('ckdlg');
let cur = null, sid = null, ckTimer = null;

const esc = s => (s==null?'':String(s)).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function resultCell(t){
  if(t.rc===null||t.rc===undefined) return '<span class="muted">未运行</span>';
  return `<span class="${t.rc===0?'ok':'bad'}">${t.rc===0?'成功':'失败'} rc=${t.rc}</span>`;
}

async function refresh(){
  const r = await fetch('/api/state'); if(!r.ok) return;
  const d = await r.json();
  const acc = d.accounts || [];
  document.getElementById('acc').textContent = acc.length ? acc.join(', ') : '0';
  document.getElementById('ts').textContent = d.ts.slice(11);
  document.getElementById('rows').innerHTML = (d.tasks||[]).map(t=>`
    <tr>
      <td>${esc(t.name)}<div class="mono">${esc(t.script)}</div></td>
      <td class="mono">${esc(t.cal)}</td>
      <td class="mono">${esc(t.next)}</td>
      <td class="mono">${esc(t.last_start||'—')}${t.cost!==null&&t.cost!==undefined?`<div class="muted">${t.cost}s</div>`:''}</td>
      <td>${resultCell(t)}</td>
      <td style="text-align:right;white-space:nowrap">
        <button onclick="run('${t.id}',this)">跑一次</button>
        <button onclick="openLog('${t.id}','${esc(t.name)}')">日志</button>
      </td>
    </tr>`).join('') || '<tr><td colspan="6" class="muted">没有任务</td></tr>';
}

async function run(id, btn){
  btn.disabled = true; btn.textContent='已提交';
  const r = await fetch('/api/run?id='+id, {method:'POST'});
  const d = await r.json();
  document.getElementById('msg').innerHTML = d.ok
    ? `<span class="ok">${esc(d.msg)}</span>` : `<span class="bad">${esc(d.msg)}</span>`;
  setTimeout(()=>{btn.disabled=false; btn.textContent='跑一次'; refresh();}, 4000);
}

function openLog(id, name){
  cur = id; document.getElementById('dlgTitle').textContent = '日志 · ' + name;
  document.getElementById('dlgBody').textContent = '加载中…';
  dlg.showModal(); loadLog();
}

async function loadLog(){
  if(!cur) return;
  const r = await fetch('/api/log?id='+cur+'&lines=200');
  const el = document.getElementById('dlgBody');
  el.textContent = await r.text(); el.scrollTop = 1e9;
}

async function openCk(){
  document.getElementById('ckMsg').textContent = '正在拉起浏览器…';
  document.getElementById('qrimg').removeAttribute('src');
  ckdlg.showModal();
  const r = await fetch('/api/ck/start', {method:'POST'});
  const d = await r.json();
  if(!d.ok){ document.getElementById('ckMsg').textContent = d.msg; return; }
  sid = d.sid; pollCk();
}

function closeCk(){ clearTimeout(ckTimer); ckdlg.close(); refresh(); }

const QR_REFRESH_MS = 70000;   // 和服务端 QR_REFRESH_SECONDS 默认值保持一致

async function pollCk(){
  if(!sid) return;
  const r = await fetch('/api/ck/status?sid='+sid);
  const d = await r.json();
  const msg = document.getElementById('ckMsg');
  const img = document.getElementById('qrimg');
  if(d.state === 'qr'){
    const v = String(d.v || 0);
    if(img.dataset.v !== v){                 // 只有换新码时才重新拉图，避免每 1.5 秒重下一遍
      img.dataset.v = v;
      img.src = '/api/ck/qr?sid='+sid+'&v='+v+'&t='+Date.now();
    }
    const left = d.ts ? Math.max(0, Math.round((d.ts + QR_REFRESH_MS - Date.now())/1000)) : 0;
    msg.innerHTML = '<span class="ok">第 '+esc(v)+' 张，等扫码…</span>（约 '+left+' 秒后自动换新码）';
  } else if(d.state === 'ok'){
    msg.innerHTML = '<span class="ok">'+esc(d.msg||'已写入')+'</span>';
    refresh(); return;
  } else if(d.state === 'expired' || d.state === 'error'){
    msg.innerHTML = '<span class="bad">'+(d.state==='expired'?'这张超时了':'出错了：'+esc(d.msg||''))+
                    '</span> <button onclick="openCk()">换一张</button>';
    return;
  } else {
    msg.textContent = '正在拉起浏览器…';
  }
  ckTimer = setTimeout(pollCk, 1500);
}

async function savePaste(){
  const txt = document.getElementById('ckPaste').value.trim();
  const r = await fetch('/api/ck/manual', {method:'POST', body: txt});
  const d = await r.json();
  document.getElementById('ckMsg').innerHTML = d.ok
    ? '<span class="ok">'+esc(d.msg)+'</span>' : '<span class="bad">'+esc(d.msg)+'</span>';
  if(d.ok) refresh();
}

refresh(); setInterval(refresh, 20000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    server_version = "jd-panel/2"

    def log_message(self, *a):
        pass

    def _auth(self):
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            u, _, p = base64.b64decode(hdr[6:]).decode("utf-8").partition(":")
        except Exception:
            return False
        return hmac.compare_digest(u, USER) and hmac.compare_digest(p, PASS)

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def _guard(self):
        if self._auth():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="jd"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if u.path == "/api/state":
            return self._send(200, json.dumps(state(), ensure_ascii=False))
        if u.path == "/api/log":
            t = next((x for x in tasks() if x["id"] == q.get("id", [""])[0]), None)
            if not t:
                return self._send(404, "没有这个任务", "text/plain; charset=utf-8")
            lg = newest_log(t["script"])
            n = int(q.get("lines", ["200"])[0])
            return self._send(200, tail(os.path.join(LOGS, lg), n) if lg else "(还没跑过)",
                              "text/plain; charset=utf-8")
        if u.path == "/api/ck/qr":
            sid = q.get("sid", [""])[0]
            if not re.fullmatch(r"[0-9a-f]{6,32}", sid or ""):
                return self._send(400, "bad sid", "text/plain")
            path = os.path.join(SESS_ROOT, sid, "qr.png")
            try:
                with open(path, "rb") as f:
                    return self._send(200, f.read(), "image/png")
            except FileNotFoundError:
                return self._send(404, "qr not ready", "text/plain")
        if u.path == "/api/ck/status":
            sid = q.get("sid", [""])[0]
            if not re.fullmatch(r"[0-9a-f]{6,32}", sid or ""):
                return self._send(400, json.dumps({"state": "error", "msg": "bad sid"}))
            st = qr_status(sid)
            # 会话结束了就把它从复用表里摘掉，用户再点一次能拉起新浏览器
            if st.get("state") in ("ok", "expired", "error"):
                _active.pop(sid, None)
            return self._send(200, json.dumps(st, ensure_ascii=False))
        return self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        if u.path == "/api/run":
            tid = parse_qs(u.query).get("id", [""])[0]
            ok, msg = run_task(tid)
            return self._send(200 if ok else 500,
                              json.dumps({"ok": ok, "msg": msg}, ensure_ascii=False))
        if u.path == "/api/ck/start":
            try:
                return self._send(200, json.dumps({"ok": True, "sid": start_qr()}))
            except Exception as e:
                return self._send(500, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
        if u.path == "/api/ck/manual":
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
            try:
                pin, key = parse_cookie_blob(body)
                total = merge_ck(pin, key)
                return self._send(200, json.dumps(
                    {"ok": True, "msg": f"已添加 {pin}，共 {total} 个账号"}, ensure_ascii=False))
            except Exception as e:
                return self._send(400, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
        return self._send(404, "not found", "text/plain; charset=utf-8")


if __name__ == "__main__":
    os.makedirs(LOGS, exist_ok=True)
    srv = ThreadingHTTPServer((BIND, PORT), H)
    print(f"[jd-panel] http://{BIND}:{PORT}  user={USER} app={APP_DIR}", flush=True)
    srv.serve_forever()
