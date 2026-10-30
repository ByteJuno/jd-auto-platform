#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notify.py — 一个函数搞定多渠道通知，只用标准库。

    from notify import send
    send("标题", "正文")

按 .env 的 NOTIFY_TYPE 走: wework | telegram | bark | serverchan
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request


def _post(url: str, body: dict, timeout: int = 12) -> str:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return f"{r.status} {r.read().decode('utf-8', 'replace')[:120]}"
    except Exception as e:  # noqa: BLE001
        return f"ERR {type(e).__name__}: {e}"


def _get(url: str, timeout: int = 12) -> str:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return f"{r.status} {r.read().decode('utf-8', 'replace')[:120]}"
    except Exception as e:  # noqa: BLE001
        return f"ERR {type(e).__name__}: {e}"


def send(title: str, content: str) -> str:
    kind = os.environ.get("NOTIFY_TYPE", "").strip().lower()
    text = f"{title}\n{content}"

    if kind == "wework":
        hook = os.environ.get("WEWORK_WEBHOOK", "")
        if not hook:
            return "skip: 未配置 WEWORK_WEBHOOK"
        return _post(hook, {"msgtype": "text", "text": {"content": text}})

    if kind == "telegram":
        tok = os.environ.get("TG_BOT_TOKEN", "")
        chat = os.environ.get("TG_CHAT_ID", "")
        if not tok or not chat:
            return "skip: 未配置 TG_BOT_TOKEN / TG_CHAT_ID"
        return _post(f"https://api.telegram.org/bot{tok}/sendMessage",
                     {"chat_id": chat, "text": text})

    if kind == "bark":
        base = os.environ.get("BARK_URL", "").rstrip("/")
        if not base:
            return "skip: 未配置 BARK_URL"
        q = urllib.parse.urlencode({"title": title, "body": content})
        return _get(f"{base}/{urllib.parse.quote(title)}?{q}")

    if kind == "serverchan":
        key = os.environ.get("SC_KEY", "")
        if not key:
            return "skip: 未配置 SC_KEY"
        return _post(f"https://sctapi.ftqq.com/{key}.send",
                     {"title": title, "desp": content})

    return f"skip: NOTIFY_TYPE={kind!r} 未识别"


if __name__ == "__main__":
    import sys
    print(send(sys.argv[1] if len(sys.argv) > 1 else "test",
               sys.argv[2] if len(sys.argv) > 2 else "hello"))
