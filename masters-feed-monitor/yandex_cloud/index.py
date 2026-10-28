"""Yandex Cloud Functions entry point.

The Object Storage bucket is mounted read/write at ``/function/storage/state``.
Only the alert state and the latest JSON report are persisted there. Source
feeds are downloaded to ``/tmp`` and discarded after the invocation.
"""

from __future__ import annotations

import json
import base64
import hashlib
import hmac
import os
import re
import shutil
import socket
import ssl
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import monitor


CACHE_DIR = Path(os.environ.get("MONITOR_CACHE_DIR", "/tmp/masters-feed-monitor"))
STATE_DIR = Path(os.environ.get("MONITOR_STATE_DIR", "/function/storage/state"))
NOTIFY_AFTER = int(os.environ.get("MONITOR_NOTIFY_AFTER", "2"))
LATEST_TELEGRAM_STATE_NAME = "latest-telegram.json"
START_REPLY_THROTTLE_NAME = "telegram-start-throttle.json"
SUBSCRIBERS_DIR_NAME = "telegram-subscribers"
START_REPLY_THROTTLE_SECONDS = 60
MAX_SUBSCRIBERS = 100


def _is_digest_event(event: Any) -> bool:
    """Return True for the separate daily status trigger."""
    if not isinstance(event, dict):
        return False
    if event.get("mode") == "digest":
        return True
    for message in event.get("messages", []):
        payload = message.get("details", {}).get("payload")
        if not payload:
            continue
        try:
            decoded = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            decoded = {"mode": payload}
        if isinstance(decoded, dict) and decoded.get("mode") == "digest":
            return True
    return False


def _event_mode(event: Any) -> str:
    if not isinstance(event, dict):
        return ""
    if isinstance(event.get("mode"), str):
        return event["mode"]
    for message in event.get("messages", []):
        payload = message.get("details", {}).get("payload")
        if not payload:
            continue
        try:
            decoded = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            decoded = {"mode": payload}
        if isinstance(decoded, dict) and isinstance(decoded.get("mode"), str):
            return decoded["mode"]
    return ""


def _network_diagnostics() -> dict[str, Any]:
    """Check DNS, TCP and TLS without using or exposing the bot token."""
    result: dict[str, Any] = {}
    for host in ("api.telegram.org", "cg-projects.ru", "newfeed.6feeds.ru"):
        host_checks: list[dict[str, Any]] = []
        try:
            addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError as exc:
            result[host] = {"dns_error": type(exc).__name__}
            continue
        seen: set[tuple[Any, ...]] = set()
        for family, socktype, proto, _, sockaddr in addresses:
            address_key = (family, *sockaddr)
            if address_key in seen or len(host_checks) >= 6:
                continue
            seen.add(address_key)
            check = {
                "family": "ipv6" if family == socket.AF_INET6 else "ipv4",
                "address": sockaddr[0],
            }
            raw_socket = socket.socket(family, socktype, proto)
            raw_socket.settimeout(5)
            try:
                raw_socket.connect(sockaddr)
                context = ssl.create_default_context()
                with context.wrap_socket(raw_socket, server_hostname=host) as tls_socket:
                    check["tls"] = tls_socket.version()
                    check["ok"] = True
            except OSError as exc:
                check["ok"] = False
                check["error"] = type(exc).__name__
                raw_socket.close()
            host_checks.append(check)
        result[host] = host_checks
    return result


def _copy_if_exists(source: Path, destination: Path) -> None:
    if source.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def _json_response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps(payload, ensure_ascii=False),
    }


def _telegram_webhook_reply(chat_id: str, message: str) -> dict[str, Any]:
    """Ask Telegram to execute sendMessage from the webhook response itself."""
    return _json_response(
        200,
        {
            "method": "sendMessage",
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": True,
        },
    )


def _claim_start_reply(chat_id: str) -> bool:
    """Allow one cached reply per private chat during a short retry window."""
    state_path = STATE_DIR / START_REPLY_THROTTLE_NAME
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    replies = state.get("chat_replies") if isinstance(state, dict) else None
    if not isinstance(replies, dict):
        replies = {}
    now = int(time.time())
    recent = {
        str(key): int(value)
        for key, value in replies.items()
        if isinstance(value, (int, float))
        and now - int(value) < START_REPLY_THROTTLE_SECONDS
    }
    if chat_id in recent:
        return False
    recent[chat_id] = now
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"chat_replies": recent}, ensure_ascii=False), encoding="utf-8"
    )
    return True


def _private_command(payload: dict[str, Any]) -> tuple[str, str, int] | None:
    message = payload.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    text = str(message.get("text") or "").strip()
    update_id = payload.get("update_id")
    if not (
        isinstance(chat, dict)
        and chat.get("type") == "private"
        and isinstance(update_id, int)
        and not isinstance(update_id, bool)
    ):
        return None
    chat_id = str(chat.get("id") or "")
    if not chat_id.isdigit() or len(chat_id) > 20 or int(chat_id) <= 0:
        return None
    match = re.match(r"^/(start|stop)(?:@\w+)?(?:\s|$)", text, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).lower(), chat_id, update_id


def _load_subscribers() -> dict[str, dict[str, Any]]:
    directory = STATE_DIR / SUBSCRIBERS_DIR_NAME
    clean: dict[str, dict[str, Any]] = {}
    for path in directory.glob("*.json"):
        chat_id = path.stem
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise RuntimeError("Telegram subscriber registry is damaged") from error
        if not chat_id.isdigit() or not isinstance(record, dict):
            raise RuntimeError("Telegram subscriber registry is damaged")
        clean[chat_id] = {
            "active": bool(record.get("active")),
            "last_update_id": int(record.get("last_update_id", -1)),
            "updated_at": int(record.get("updated_at") or 0),
        }
    owner = str(os.environ.get("TELEGRAM_CHAT_ID") or "")
    if owner.isdigit() and owner not in clean:
        clean[owner] = {"active": True, "last_update_id": -1, "updated_at": 0}
    return clean


def _save_subscription(chat_id: str, record: dict[str, Any]) -> None:
    directory = STATE_DIR / SUBSCRIBERS_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{chat_id}.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )


def _set_subscription(chat_id: str, *, active: bool, update_id: int) -> bool:
    subscribers = _load_subscribers()
    previous = subscribers.get(chat_id)
    # Telegram can randomize update IDs after a week without updates.
    if (previous and time.time() - previous.get("updated_at", 0) < 7 * 86400
            and update_id <= previous.get("last_update_id", -1)):
        return bool(previous.get("active"))
    if active and chat_id not in subscribers and len(subscribers) >= MAX_SUBSCRIBERS:
        raise RuntimeError("Telegram subscriber limit reached")
    record = {
        "active": active,
        "last_update_id": update_id,
        "updated_at": int(time.time()),
    }
    _save_subscription(chat_id, record)
    return active


def _active_subscribers() -> list[str]:
    return sorted(
        chat_id for chat_id, record in _load_subscribers().items() if record.get("active")
    )


def _http_payload(event: dict[str, Any]) -> dict[str, Any]:
    body = event.get("body")
    if not isinstance(body, str):
        return {}
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return {}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _header(event: dict[str, Any], name: str) -> str:
    headers = event.get("headers")
    if not isinstance(headers, dict):
        return ""
    target = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == target:
            return str(value)
    return ""


def _webhook_secret(token: str | None = None) -> str:
    value = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _telegram_api(method: str, parameters: dict[str, str]) -> dict[str, Any]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=urllib.parse.urlencode(parameters).encode("utf-8"),
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise RuntimeError(f"Telegram {method} failed")
    return payload


def _configure_webhook(url: str) -> dict[str, Any]:
    return _telegram_api(
        "setWebhook",
        {
            "url": url,
            "secret_token": _webhook_secret(),
            "allowed_updates": json.dumps(["message"]),
            "drop_pending_updates": "false",
        },
    )


def _handle_http(event: dict[str, Any]) -> dict[str, Any]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return _json_response(500, {"ok": False, "error": "bot token missing"})
    payload = _http_payload(event)
    latest_state = STATE_DIR / LATEST_TELEGRAM_STATE_NAME

    if payload.get("action") == "subscription_status":
        if not hmac.compare_digest(_header(event, "X-Masters-Bot-Token"), token):
            return _json_response(403, {"ok": False})
        return _json_response(200, {
            "ok": True, "active_subscriber_count": len(_active_subscribers()),
            "registered_count": len(_load_subscribers()),
            "checked_at": monitor._read_json_object(latest_state).get("latest_checked_at"),
        })

    if payload.get("action") == "sync_latest":
        if not hmac.compare_digest(_header(event, "X-Masters-Bot-Token"), token):
            return _json_response(403, {"ok": False})
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip():
            return _json_response(400, {"ok": False, "error": "message missing"})
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        monitor.save_latest_telegram_message(
            latest_state, message, payload.get("checked_at")
        )
        print("HTTP sync_latest stored=True")
        subscribers = _active_subscribers()
        return _json_response(
            200,
            {"ok": True, "stored": True, "recipients": subscribers,
             "subscriber_count": len(subscribers)},
        )

    received_secret = _header(event, "X-Telegram-Bot-Api-Secret-Token")
    if not hmac.compare_digest(received_secret, _webhook_secret(token)):
        print("HTTP telegram secret_ok=False")
        return _json_response(403, {"ok": False})
    command = _private_command(payload)
    if command is None:
        return _json_response(200, {"ok": True, "sent": False})
    name, chat_id, update_id = command
    active = _set_subscription(chat_id, active=name == "start", update_id=update_id)
    if name == "stop" or not active:
        return _telegram_webhook_reply(
            chat_id, "Автоматические обновления отключены. Чтобы подключить их снова, нажмите /start."
        )
    reply = monitor.telegram_start_payload(payload, latest_state)
    print(
        "HTTP telegram secret_ok=True has_message=%s start_reply=%s"
        % (isinstance(payload.get("message"), dict), reply is not None)
    )
    if reply is None:
        if not _claim_start_reply(chat_id):
            return _json_response(200, {"ok": True, "sent": False})
        return _telegram_webhook_reply(
            chat_id, "✅ Автоматические обновления подключены. Первый отчёт придёт после ближайшей проверки."
        )
    if not _claim_start_reply(reply["chat_id"]):
        print("HTTP telegram start_reply=False reason=throttled")
        return _json_response(200, {"ok": True, "sent": False})
    confirmation = "\n\n✅ Автоматические обновления подключены."
    message = reply["text"]
    if len(message) + len(confirmation) <= 4096:
        message += confirmation
    return _telegram_webhook_reply(reply["chat_id"], message)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Run one comparison and optionally deliver a Telegram notification."""
    if isinstance(event, dict) and event.get("httpMethod"):
        return _handle_http(event)

    if _event_mode(event) == "configure_webhook":
        url = event.get("url") if isinstance(event, dict) else None
        if not isinstance(url, str) or not url.startswith("https://"):
            return _json_response(400, {"ok": False, "error": "HTTPS url required"})
        _configure_webhook(url)
        return _json_response(200, {"ok": True, "configured": True})

    if _event_mode(event) == "diagnose":
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json; charset=utf-8"},
            "body": json.dumps(_network_diagnostics(), ensure_ascii=False),
        }

    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not os.environ.get(
        "TELEGRAM_CHAT_ID"
    ):
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")

    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    monitor.download_sources(CACHE_DIR)
    report = monitor.analyze_directory(CACHE_DIR)
    message = monitor.format_telegram(report)

    # Decide against a temporary copy. If Telegram delivery fails, the durable
    # state is not advanced, so the trigger retry can send the same alert again.
    durable_state = STATE_DIR / "alert-state.json"
    temporary_state = CACHE_DIR / "alert-state.json"
    _copy_if_exists(durable_state, temporary_state)
    alert_due = monitor.should_notify(report, temporary_state, NOTIFY_AFTER)
    digest_due = _is_digest_event(event)
    sent = alert_due or digest_due
    if sent:
        subscribers = _active_subscribers()
        failures = 0
        for position, chat_id in enumerate(subscribers):
            try:
                monitor.send_telegram(message, chat_id=chat_id)
            except (OSError, RuntimeError, ValueError):
                failures += 1
            if position + 1 < len(subscribers):
                time.sleep(0.05)
        if failures:
            raise RuntimeError(f"Telegram broadcast failed for {failures} subscriber(s)")
        monitor.save_latest_telegram_message(
            STATE_DIR / LATEST_TELEGRAM_STATE_NAME,
            message,
            report.get("checked_at"),
        )

    _copy_if_exists(temporary_state, durable_state)
    (STATE_DIR / "latest-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    body = {
        "ok": True,
        "checked_at": report["checked_at"],
        "telegram_sent": sent,
        "telegram_recipient_count": len(subscribers) if sent else 0,
        "reason": "alert" if alert_due else "digest" if digest_due else "suppressed",
        "master_count": report["master"]["lot_count"],
        "site_count": report["site"]["lot_count"],
        "feed_count": report["feeds"]["cian"]["lot_count"],
    }
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps(body, ensure_ascii=False),
    }
