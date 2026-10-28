"""Operator-only setup/status; never run from the monitor schedule.

Read the exact IKON Lockbox entry into memory, seed an existing report and
configure Telegram's webhook. No feed downloads and no plaintext secret files.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

import monitor


EXPECTED_BOT = "advelop_ikon_bot"
EXPECTED_BOT_ID = 8628586333
ENDPOINT = "https://functions.yandexcloud.net/d4e7q4tsbnf8q795rj2h"
GATEWAY_ENDPOINT = "https://d5dqg7mhncqkops7na55.0ly8ed4d.apigw.yandexcloud.net/telegram"
WEBHOOK_ENDPOINT = "https://ikon-telegram-relay.avillius.chatgpt.site/telegram"
SECRET_ID = "e6q7f76acilc6e8c17em"


def telegram_call(token: str, method: str, payload: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload or {}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError("Telegram did not confirm operation")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yc", default="yc")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if not args.status and args.report is None:
        parser.error("--report is required for setup")
    result = subprocess.run(
        [args.yc, "lockbox", "payload", "get", "--id", SECRET_ID, "--key", "telegram_bot_token"],
        capture_output=True, text=True, timeout=45, check=True,
    )
    token = result.stdout.strip()
    if not token.startswith(str(EXPECTED_BOT_ID) + ":"):
        raise RuntimeError("Unexpected bot token")
    me = telegram_call(token, "getMe")["result"]
    if me.get("id") != EXPECTED_BOT_ID or me.get("username") != EXPECTED_BOT:
        raise RuntimeError("Unexpected bot identity")
    previous = telegram_call(token, "getWebhookInfo")["result"]
    if previous.get("url") not in (None, "", ENDPOINT, GATEWAY_ENDPOINT, WEBHOOK_ENDPOINT):
        raise RuntimeError("Different webhook already configured; inspect before replacing")
    if not args.status:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        message = monitor.format_telegram(report)
        os.environ["TELEGRAM_BOT_TOKEN"] = token
        os.environ["TELEGRAM_LATEST_MESSAGE_URL"] = ENDPOINT
        monitor.sync_latest_message(message, report["checked_at"])
        print(json.dumps({"report_seeded": True, "checked_at": report["checked_at"],
                          "message_sha256": hashlib.sha256(message.encode()).hexdigest()}))
        telegram_call(token, "setWebhook", {
            "url": WEBHOOK_ENDPOINT, "secret_token": hashlib.sha256(token.encode()).hexdigest(),
            "max_connections": 1, "allowed_updates": ["message"], "drop_pending_updates": False,
        })
    info = telegram_call(token, "getWebhookInfo")["result"]
    print(json.dumps({"bot": me["username"], "webhook": info.get("url"),
                      "pending_updates": info.get("pending_update_count"),
                      "last_error_date": info.get("last_error_date"),
                      "last_error_message": info.get("last_error_message"),
                      "ip_address": info.get("ip_address"),
                      "max_connections": info.get("max_connections")}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Do not print exception URLs or subprocess stdout: they can contain tokens.
        code = getattr(error, "code", None)
        print(f"Webhook setup failed: {type(error).__name__}; HTTP status={code}", file=sys.stderr)
        raise SystemExit(1)
