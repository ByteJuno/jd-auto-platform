#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ck_push.py — 把京东 Cookie 推进青龙，按 pt_pin 去重、已存在则更新。

用法:
    python scripts/ck_push.py --file ck.txt --dry-run
    python scripts/ck_push.py --file ck.txt

ck.txt 每行一个账号，格式任选其一:
    pt_key=AAJxxx;pt_pin=jd_abc;
    pt_pin=jd_abc;pt_key=AAJxxx;

设计要点:
  * 只按 pt_pin 判重 —— pt_key 每周会换，按 key 判重会堆出一堆废变量。
  * 不打印完整 key（只显示前 8 位），避免日志泄露即账号泄露。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

QL_URL = os.environ.get("QL_URL", "http://127.0.0.1:5700").rstrip("/")
QL_USER = os.environ.get("QL_USER", "admin")
QL_PASS = os.environ.get("QL_PASS", "")

PIN_RE = re.compile(r"pt_pin=([^;]+)")
KEY_RE = re.compile(r"pt_key=([^;]+)")


def _req(method, path, body=None, token=None, dry=False):
    url = f"{QL_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    if dry:
        slim = dict(body) if isinstance(body, dict) else body
        if isinstance(slim, dict) and "value" in slim:
            slim["value"] = mask(slim["value"])
        print(f"  [dry] {method} {url} {json.dumps(slim, ensure_ascii=False)[:300]}")
        return 200, {"dry": True}
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


def mask(ck: str) -> str:
    m = KEY_RE.search(ck or "")
    if not m:
        return "<bad>"
    k = m.group(1)
    return f"pt_key={k[:8]}...(len={len(k)})"


def parse_file(path: str) -> list[tuple[str, str]]:
    """返回 [(pt_pin, 规范化 ck)]，按 pin 去重，后者覆盖前者。"""
    seen: dict[str, str] = {}
    with open(path, "r", encoding="utf-8-sig") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip().strip(",").strip('"')
            if not line or line.startswith("#"):
                continue
            pin = PIN_RE.search(line)
            key = KEY_RE.search(line)
            if not pin or not key:
                print(f"  !! 第 {ln} 行不完整，跳过: {line[:40]}")
                continue
            cookie = f"pt_key={key.group(1)};pt_pin={pin.group(1)};"
            if len(key.group(1)) < 20:
                print(f"  !! 第 {ln} 行 pt_key 太短，疑似被截断，跳过")
                continue
            seen[pin.group(1)] = cookie
    return list(seen.items())


def login(dry: bool):
    code, res = _req("POST", "/api/user/login",
                     {"username": QL_USER, "password": QL_PASS}, dry=dry)
    if dry:
        return "DRYTOKEN"
    if (code == 200 and isinstance(res, dict) and res.get("code") == 200
            and res.get("data", {}).get("token")):
        return res["data"]["token"]
    print(f"登录失败 code={code} {res}")
    print("提示: 2.24 起登录是 POST /api/user/login；未初始化先 PUT /api/user/init")
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="ck 文本文件")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-login", action="store_true")
    a = ap.parse_args()
    dry = a.dry_run

    accounts = parse_file(a.file)
    if not accounts:
        print("没有解析到有效 ck")
        return 2
    print(f"解析到 {len(accounts)} 个账号: {', '.join(p for p, _ in accounts)}\n")

    token = os.environ.get("QL_TOKEN") if a.no_login else login(dry)
    if not token:
        return 2

    code, res = _req("GET", "/api/envs?searchValue=JD_COOKIE&page=1&size=200",
                     token=token, dry=dry)
    exist: dict[str, dict] = {}
    if not dry and isinstance(res, dict):
        for e in (res.get("data") or []):
            if e.get("name") != "JD_COOKIE":
                continue
            m = PIN_RE.search(e.get("value") or "")
            if m:
                exist[m.group(1)] = e

    add = upd = bad = 0
    for pin, cookie in accounts:
        if pin in exist:
            e = exist[pin]
            code, _ = _req("PUT", "/api/envs",
                           {"id": e.get("id"), "name": "JD_COOKIE",
                            "value": cookie, "remarks": e.get("remarks") or pin},
                           token=token, dry=dry)
            if dry or code == 200:
                upd += 1
                print(f"  upd  {pin}  {mask(cookie)}")
            else:
                bad += 1
                print(f"  FAIL upd {pin} -> {code}")
        else:
            body = {"name": "JD_COOKIE", "value": cookie, "remarks": pin}
            if dry:
                code, _ = _req("POST", "/api/envs", body, token=token, dry=True)
            else:
                code, _ = _req("POST", "/api/envs", [body], token=token)
                if code != 200:
                    code, _ = _req("POST", "/api/envs", body, token=token)
            if dry or code == 200:
                add += 1
                print(f"  add  {pin}  {mask(cookie)}")
            else:
                bad += 1
                print(f"  FAIL add {pin} -> {code}")

    print(f"\n新增 {add} / 更新 {upd} / 失败 {bad}")
    if not dry and (add or upd):
        print("提示: 改完在面板『环境变量』里点一次『启用』，或直接等下次任务跑。")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
