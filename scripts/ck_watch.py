#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ck_watch.py — CK 存活巡检。京东 ck 大约 7 天过期，过期不报警 = 白跑一周。

用法:
    python scripts/ck_watch.py --file ck.txt
    python scripts/ck_watch.py --from-ql          # 直接从青龙拉 JD_COOKIE 检查

判定: 打 me-api 的 GetJDUserInfoUnion
    retcode 0    -> 有效
    retcode 1001 -> ck 失效/需重新登录
    其它/网络错  -> 存疑，不下结论（可能是风控打脸，不是 ck 死了）

限速: 账号间 2~5 秒随机延迟，单线程。别改成并发，这是被封的最快方式。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

QL_URL = os.environ.get("QL_URL", "http://127.0.0.1:5700").rstrip("/")
QL_USER = os.environ.get("QL_USER", "admin")
QL_PASS = os.environ.get("QL_PASS", "")

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
      "jdapp;iPhone;13.2.0;17.4;9a1f6c;A;")
CHECK_URL = "https://me-api.jd.com/user_new/info/GetJDUserInfoUnion"

PIN_RE = re.compile(r"pt_pin=([^;]+)")
KEY_RE = re.compile(r"pt_key=([^;]+)")


def _http_json(url, body=None, cookie=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data,
                                 method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", UA)
    if cookie:
        req.add_header("Cookie", cookie)
        req.add_header("Referer", "https://home.m.jd.com/")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200]
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


def ql_login():
    code, res = _http_json(f"{QL_URL}/api/user/login",
                           {"username": QL_USER, "password": QL_PASS})
    if code == 200 and isinstance(res, dict) and res.get("code") == 200:
        return res.get("data", {}).get("token")
    return None


def from_ql() -> list[tuple[str, str]]:
    token = ql_login()
    if not token:
        print("青龙登录失败，检查 .env")
        return []
    req = urllib.request.Request(
        f"{QL_URL}/api/envs?searchValue=JD_COOKIE&page=1&size=200")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            res = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        print(f"拉环境变量失败: {e}")
        return []
    out = []
    for e in res.get("data") or []:
        if e.get("name") != "JD_COOKIE":
            continue
        v = e.get("value") or ""
        p = PIN_RE.search(v)
        if p:
            out.append((p.group(1), v))
    return out


def from_file(path: str) -> list[tuple[str, str]]:
    out = {}
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            p, k = PIN_RE.search(line), KEY_RE.search(line)
            if p and k and len(k.group(1)) >= 20:
                out[p.group(1)] = f"pt_key={k.group(1)};pt_pin={p.group(1)};"
    return list(out.items())


def check_one(cookie: str) -> tuple[str, str]:
    code, res = _http_json(CHECK_URL, cookie=cookie)
    if code != 200 or not isinstance(res, dict):
        return "unknown", f"HTTP {code} {str(res)[:80]}"
    rc = str(res.get("retcode"))
    if rc == "0":
        nick = (res.get("data") or {}).get("userInfo", {}).get("nickName") or "?"
        return "valid", f"retcode=0 nick={nick}"
    if rc == "1001":
        return "expired", "retcode=1001 需重新扫码"
    return "unknown", f"retcode={rc} msg={res.get('msg')}"


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--file")
    g.add_argument("--from-ql", action="store_true")
    ap.add_argument("--no-notify", action="store_true")
    a = ap.parse_args()

    accounts = from_ql() if a.from_ql else from_file(a.file)
    if not accounts:
        print("没有账号")
        return 2

    rows, expired = [], []
    for i, (pin, cookie) in enumerate(accounts, 1):
        state, detail = check_one(cookie)
        rows.append({"pin": pin, "state": state, "detail": detail})
        print(f"[{i}/{len(accounts)}] {pin:<24} {state:<8} {detail}")
        if state == "expired":
            expired.append(pin)
        if i < len(accounts):
            time.sleep(random.uniform(2.0, 5.0))

    os.makedirs("logs", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = f"logs/ck_watch_{ts}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"\n报告: {path}")

    if expired and not a.no_notify:
        try:
            sys.path.insert(0, os.path.dirname(__file__))
            from notify import send
            send("京东 CK 失效",
                 "需重新扫码:\n" + "\n".join(f"- {p}" for p in expired))
            print("已推送告警")
        except Exception as e:  # noqa: BLE001
            print(f"告警推送失败: {e}")
    return 1 if expired else 0


if __name__ == "__main__":
    sys.exit(main())
