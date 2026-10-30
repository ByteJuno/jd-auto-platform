#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ql_bootstrap.py — 把青龙面板一次性配好：依赖 / 订阅 / 定时任务。

用法:
    python3 scripts/ql_bootstrap.py --dry-run     # 只打印要提交的 payload
    python3 scripts/ql_bootstrap.py               # 真提交
    QL_TOKEN=xxx python3 scripts/ql_bootstrap.py --no-login

版本适配（踩过的坑，别再踩）:
    2.24.x  POST /api/user/login   （老版本是 PUT）
            PUT  /api/user/init    （首次建管理员，已初始化会返回 450）
            POST /api/dependencies 要数组 [{name,type}, ...]
            POST /api/crons        要对象 {name,command,schedule}
            POST /api/subscriptions 要对象，且必须带 schedule_type + alias，
                                   depend 字段已被移除（传了会 400 not allowed）
            GET  /api/crons        返回 {data:{data:[...],total:n}}
    2.17.x  依赖/订阅/任务都是单对象，订阅带 depend

幂等: 依赖/订阅/任务按名字查重，已存在就跳过。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# ---------------------------------------------------------------- 配置区

QL_URL = os.environ.get("QL_URL", "http://127.0.0.1:5700").rstrip("/")
QL_USER = os.environ.get("QL_USER", "admin")
QL_PASS = os.environ.get("QL_PASS", "")

DEP_TYPES = {"nodejs": 1, "python3": 2, "linux": 3}

DEPS = [
    ("python3", "requests"),
    ("python3", "pyexecjs"),
    ("nodejs", "axios"),
    ("nodejs", "ws"),
    ("nodejs", "png-js"),
    ("nodejs", "jsdom"),
    ("nodejs", "moment"),
    ("nodejs", "dotenv"),
    ("nodejs", "crypto-js"),
    ("nodejs", "date-fns"),
    ("linux", "chromium"),
    ("linux", "xvfb"),
    ("linux", "ttf-freefont"),
    ("linux", "font-noto"),
]

SUBS = [
    {
        "name": "faker2-jd",
        "alias": "faker2",
        # faker4 是"日常签到版"，只有 20 个脚本，没有 jd_CheckCK.js；
        # faker2 是完整库，CK 巡检 / 价保 / 店铺签到都在这里。
        "url": "https://github.com/shufflewzc/faker2.git",
        "type": "public-repo",
        "branch": "main",
        "schedule_type": "cron",
        "schedule": "0 3 * * *",
        "whitelist": r"jd_|jx_|gua_|jddj_|jdCookie",
        "blacklist": r"activity|backUp",
        "depend": r"^jd[^_]|USER|function|utils|sendNotify|sign_graphics_validate|ql",
    }
]

# 拉库后 qinglong 会把脚本落在 scripts/<owner>_<repo>_<branch>/
REPO_DIR = "shufflewzc_faker2_main"

# 只留这几条。拉库会自动塞进 80+ 条任务（含互助/助力），那些是封号主因，见 README。
# 全部 command 用 `task <dir>/<file>`，必须带目录，否则 qinglong 找不到文件。
TASKS = [
    ("京东CK检测", f"task {REPO_DIR}/jd_CheckCK.js", "30 7 * * *"),
    ("京东资产统计", f"task {REPO_DIR}/jd_bean_change.js", "40 21 * * *"),
    ("京东店铺签到", f"task {REPO_DIR}/jd_dpqd_sign.js", "8 9 * * *"),
    ("京东升级赚京豆", f"task {REPO_DIR}/jd_bean_home.js", "23 10 * * *"),
    ("京东一键价保", f"task {REPO_DIR}/jd_OnceApply.js", "55 11 * * *"),
]

V24 = False  # 运行期由 /api/system 的 version 决定

# ---------------------------------------------------------------- HTTP


def _req(method: str, path: str, body=None, token: str | None = None,
         dry: bool = False) -> tuple[int, object]:
    url = f"{QL_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    if dry:
        print(f"  [dry] {method} {url}")
        if data:
            shown = data.decode()
            if isinstance(body, dict) and "password" in body:
                shown = json.dumps({**body, "password": "***"}, ensure_ascii=False)
            print(f"        {shown[:300]}")
        return 200, {"code": 200, "dry": True}
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw
    except urllib.error.URLError as e:
        return 0, f"连接失败: {e.reason}"


def _ok(code: int, res, dry: bool = False) -> bool:
    """青龙习惯用 HTTP 200 包错误，真正看 body.code。"""
    if dry:
        return True
    if code != 200:
        return False
    if isinstance(res, dict) and "code" in res:
        return res.get("code") == 200
    return True


def _why(res) -> str:
    if isinstance(res, dict):
        v = res.get("validation")
        if isinstance(v, dict):
            body = v.get("body") or {}
            if body.get("message"):
                return str(body["message"])
        return str(res.get("message") or res.get("error") or "")[:120]
    return str(res)[:120]


def _list_of(res) -> list:
    """GET 列表接口：2.24 是 {data:{data:[...]}}，老版本是 {data:[...]}。"""
    if not isinstance(res, dict):
        return []
    d = res.get("data")
    if isinstance(d, dict):
        d = d.get("data")
    return d if isinstance(d, list) else []


def detect_version():
    global V24
    code, res = _req("GET", "/api/system")
    ver = ""
    if isinstance(res, dict):
        ver = str((res.get("data") or {}).get("version") or "")
    try:
        major, minor = (int(x) for x in ver.split(".")[:2])
    except Exception:  # noqa: BLE001
        major, minor = 2, 17
    V24 = (major, minor) >= (2, 24)
    print(f"      青龙 {ver or '未知'}  ->  {'新 API (2.24+)' if V24 else '兼容 API'}")


def login(dry: bool) -> str | None:
    print("[1/5] 登录")
    if not dry:
        detect_version()
    code, res = _req("POST", "/api/user/login",
                     {"username": QL_USER, "password": QL_PASS}, dry=dry)
    if dry:
        return "DRYTOKEN"
    if _ok(code, res, dry):
        print("      ok")
        return (res.get("data") or {}).get("token")
    if isinstance(res, dict) and res.get("code") == 450:
        print("      面板未初始化 -> PUT /api/user/init")
        c2, r2 = _req("PUT", "/api/user/init",
                      {"username": QL_USER, "password": QL_PASS})
        if _ok(c2, r2):
            c3, r3 = _req("POST", "/api/user/login",
                          {"username": QL_USER, "password": QL_PASS})
            if _ok(c3, r3):
                print("      ok (initialized)")
                return (r3.get("data") or {}).get("token")
    print(f"      !! 失败 {code} {_why(res)}")
    print("      检查 .env 的 QL_PASS 是否与面板一致，QL_URL 是否指向端口实际绑定的地址")
    return None


def ensure_deps(token: str, dry: bool) -> list[str]:
    print("[2/5] 依赖")
    failed = []
    code, existing = _req("GET", "/api/dependencies?page=1&size=200", token=token, dry=dry)
    have = {(d.get("type"), d.get("name")) for d in _list_of(existing)}
    todo = [(k, n) for k, n in DEPS if (DEP_TYPES[k], n) not in have]
    if not todo:
        print("      全部已存在")
        return failed

    if V24:
        payload = [{"name": n, "type": DEP_TYPES[k]} for k, n in todo]
        code, res = _req("POST", "/api/dependencies", payload, token=token, dry=dry)
        ok = _ok(code, res, dry)
        print(f"      {'add ' if ok else 'FAIL'} {len(payload)} 个依赖 -> {code} {'' if ok else _why(res)}")
        if not ok:
            failed += [f"{k}:{n}" for k, n in todo]
    else:
        for k, n in todo:
            code, res = _req("POST", "/api/dependencies",
                             {"name": n, "type": DEP_TYPES[k]}, token=token, dry=dry)
            ok = _ok(code, res, dry)
            print(f"      {'add ' if ok else 'FAIL'} {k}:{n} -> {code} {'' if ok else _why(res)}")
            if not ok:
                failed.append(f"{k}:{n}")

    print("      注: linux 包（chromium 等）装得慢，面板显示『已安装』前别跑农场脚本")
    return failed


def ensure_subs(token: str, dry: bool) -> list[str]:
    print("[3/5] 订阅")
    failed = []
    code, existing = _req("GET", "/api/subscriptions?page=1&size=100", token=token, dry=dry)
    have = {s.get("url") for s in _list_of(existing)}
    for s in SUBS:
        if s["url"] in have:
            print(f"      skip {s['name']}")
            continue
        if V24:
            body = dict(s)
            body.pop("depend", None)          # 2.24 已移除，传了 400
            body.setdefault("schedule_type", "cron")
            body.setdefault("alias", s["name"])
            payload = body
        else:
            payload = [{"name": s["name"], "url": s["url"], "type": s["type"],
                        "branch": s["branch"], "schedule": s["schedule"],
                        "whitelist": s["whitelist"], "blacklist": s["blacklist"],
                        "depend": s["depend"]}]
        code, res = _req("POST", "/api/subscriptions", payload, token=token, dry=dry)
        ok = _ok(code, res, dry)
        print(f"      {'add ' if ok else 'FAIL'} {s['name']} -> {code} {'' if ok else _why(res)}")
        if not ok:
            failed.append(s["name"])
    print("      注: 订阅建好后在面板点一次『运行』把脚本拉下来")
    return failed


def ensure_crons(token: str, dry: bool) -> list[str]:
    print("[4/5] 定时任务")
    failed = []
    code, existing = _req("GET", "/api/crons?page=1&size=200", token=token, dry=dry)
    have = {c.get("name") for c in _list_of(existing)}
    for name, cmd, cron in TASKS:
        if name in have:
            print(f"      skip {name}")
            continue
        body = {"name": name, "command": cmd, "schedule": cron}
        payload = body if V24 else [body]
        code, res = _req("POST", "/api/crons", payload, token=token, dry=dry)
        ok = _ok(code, res, dry)
        print(f"      {'add ' if ok else 'FAIL'} {name} [{cron}] -> {code} {'' if ok else _why(res)}")
        if not ok:
            failed.append(name)
    return failed


def prune_crons(token: str, dry: bool) -> list[str]:
    """删掉拉库自动塞进来的任务，只保留 TASKS 里的。拉完库跑一次。"""
    print("[5/5] 清理自动加的任务")
    keep = {cmd for _n, cmd, _c in TASKS}
    code, existing = _req("GET", "/api/crons?page=1&size=500", token=token, dry=dry)
    victims = [c for c in _list_of(existing) if c.get("command") not in keep]
    if not victims:
        print("      没有多余任务")
        return []
    ids = [c["id"] for c in victims]
    print(f"      删除 {len(ids)} 条: " + ", ".join(str(c.get("name")) for c in victims[:8]) + (" ..." if len(ids) > 8 else ""))
    code, res = _req("DELETE", "/api/crons", ids, token=token, dry=dry)
    ok = _ok(code, res, dry)
    print(f"      {'ok' if ok else 'FAIL'} -> {code} {'' if ok else _why(res)}")
    return [] if ok else ["prune"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-login", action="store_true")
    ap.add_argument("--prune", action="store_true",
                    help="删掉非本清单的定时任务（拉库后会多出 80+ 条互助任务）")
    a = ap.parse_args()
    dry = a.dry_run

    print(f"目标面板 {QL_URL}  dry_run={dry}\n")
    token = os.environ.get("QL_TOKEN") if a.no_login else login(dry)
    if not token:
        return 2

    bad = []
    bad += ensure_deps(token, dry)
    bad += ensure_subs(token, dry)
    bad += ensure_crons(token, dry)
    if a.prune:
        bad += prune_crons(token, dry)

    print("\n===== 结果 =====")
    if bad:
        print("以下需要手动在面板补:")
        for x in bad:
            print(f"  - {x}")
        return 1
    print("全部就绪。下一步: python3 scripts/ck_push.py --file ck.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
