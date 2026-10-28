"""Read-only server diagnostics; never output secrets, paths or recipient IDs."""
import json
import os
from pathlib import Path
import shlex
import sqlite3
import urllib.error
import urllib.request
import sys
import argparse

sys.path.insert(0, "/opt/ikon-feed-monitor")
import monitor


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deliver-current", action="store_true")
    args = parser.parse_args()
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        for line in Path("/etc/ikon-feed-monitor.env").read_text().splitlines():
            key, value = shlex.split(line)[0].split("=", 1)
            os.environ[key] = value
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    for method in ("getMe", "getWebhookInfo"):
        request = urllib.request.Request("https://api.telegram.org/bot" + token + "/" + method)
        try:
            with monitor._telegram_urlopen(request, timeout=20) as response:
                result = json.load(response)["result"]
            if method == "getMe":
                print("bot:", result.get("username"))
            else:
                print("webhook:", bool(result.get("url")), "pending:", result.get("pending_update_count"),
                      "error:", result.get("last_error_message"), "error_at:", result.get("last_error_date"))
        except urllib.error.HTTPError as error:
            print(method, "HTTP", error.code)
    try:
        status = monitor._telegram_state_request({"action": "subscription_status"})
        print("registry:", {key: status.get(key) for key in
                            ("ok", "active_subscriber_count", "registered_count", "checked_at")})
    except urllib.error.HTTPError as error:
        print("registry HTTP", error.code)
    report_path = Path("/var/lib/ikon-feed-monitor/latest.json")
    if report_path.exists():
        report = json.loads(report_path.read_text())
        text = monitor.format_telegram(report)
        print("report:", report["checked_at"], "characters:", len(text))
        for category in report["categories"].values():
            for project, data in category.get("projects", {}).items():
                master = data.get("master", {})
                print("master:", project, master.get("status"), master.get("reason"))
        if args.deliver_current:
            # Explicit operator flag sends the already checked report, not a test message.
            try:
                print("delivery:", monitor.broadcast_telegram(text, report["checked_at"]))
            except urllib.error.HTTPError as error:
                print("delivery HTTP", error.code)
                raise


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Diagnostic failed:", type(error).__name__)
        raise SystemExit(1)
