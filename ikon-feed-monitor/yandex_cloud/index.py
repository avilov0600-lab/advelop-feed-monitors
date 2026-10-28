"""Yandex Cloud Functions HTTP handler for the cached Telegram digest.

Mount a dedicated private Object Storage prefix at /function/storage/state.
GitHub Actions performs monitoring; this endpoint only stores/replays messages.
It never downloads feeds and never calls GitHub.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import monitor


STATE_DIR = Path(os.environ.get("MONITOR_STATE_DIR", "/function/storage/state"))
LATEST_STATE_NAME = "ikon-latest-telegram.json"
THROTTLE_STATE_NAME = "ikon-start-throttle.json"
THROTTLE_SECONDS = 60
SUBSCRIBERS_DIR_NAME = "ikon-subscribers"
MAX_SUBSCRIBERS = 100


def _valid_chat_id(value: Any) -> bool:
    return bool(re.fullmatch(r"[1-9][0-9]{0,19}", str(value)))


def _subscribers() -> dict[str, dict[str, Any]]:
    subscribers = {}
    for path in (STATE_DIR / SUBSCRIBERS_DIR_NAME).glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise RuntimeError("IKON subscriber registry is damaged") from error
        if (not _valid_chat_id(path.stem) or not isinstance(record, dict)
                or type(record.get("active")) is not bool
                or type(record.get("last_update_id")) is not int
                or type(record.get("updated_at")) is not int):
            raise RuntimeError("IKON subscriber registry is damaged")
        subscribers[path.stem] = record
    return subscribers


def _save_subscriber(chat_id: str, record: dict[str, Any]) -> None:
    directory = STATE_DIR / SUBSCRIBERS_DIR_NAME
    directory.mkdir(parents=True, exist_ok=True)
    # Separate objects prevent two different new subscribers overwriting each other.
    (directory / f"{chat_id}.json").write_text(json.dumps(record), encoding="utf-8")


def _subscribe(chat_id: str, update_id: int, *, owner: bool = False) -> None:
    subscribers = _subscribers()
    previous = subscribers.get(chat_id)
    if owner and previous is not None:
        return  # Do not re-enable an owner who has blocked the bot.
    if previous and time.time() - previous.get("updated_at", 0) < 7 * 86400:
        if update_id <= previous.get("last_update_id", -1):
            return
    if previous is None and len(subscribers) >= MAX_SUBSCRIBERS:
        raise RuntimeError("IKON subscriber limit reached")
    _save_subscriber(chat_id, {
        "active": True, "last_update_id": update_id, "updated_at": int(time.time()),
    })


def _recipients() -> list[dict[str, Any]]:
    return [{"chat_id": chat_id, "revision": record.get("last_update_id", -1)}
            for chat_id, record in sorted(_subscribers().items()) if record.get("active")]


def _private_start(payload: dict[str, Any]) -> tuple[str, int] | None:
    message = payload.get("message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat")
    update_id = payload.get("update_id")
    if (not isinstance(chat, dict) or chat.get("type") != "private"
            or not _valid_chat_id(chat.get("id"))
            or type(update_id) is not int or update_id < 0
            or not re.match(r"^/start(?:@advelop_ikon_bot)?(?:\s|$)",
                            str(message.get("text") or ""), re.IGNORECASE)):
        return None
    return str(chat["id"]), update_id


def _response(status: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps(payload, ensure_ascii=False),
    }


def _header(event: dict[str, Any], name: str) -> str:
    headers = event.get("headers")
    if not isinstance(headers, dict):
        return ""
    for key, value in headers.items():
        if str(key).casefold() == name.casefold():
            return str(value)
    return ""


def _payload(event: dict[str, Any]) -> dict[str, Any]:
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


def webhook_secret(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _checked_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _claim_reply(chat_id: str) -> bool:
    state_path = STATE_DIR / THROTTLE_STATE_NAME
    previous = monitor._read_json(state_path)
    replies = previous.get("chat_replies") or {}
    now = int(time.time())
    recent = {
        str(key): int(value)
        for key, value in replies.items()
        if isinstance(value, (int, float)) and now - int(value) < THROTTLE_SECONDS
    }
    if chat_id in recent:
        return False
    recent[chat_id] = now
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"chat_replies": recent}), encoding="utf-8")
    return True


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return _response(500, {"ok": False, "error": "bot token missing"})
    if not isinstance(event, dict) or event.get("httpMethod") != "POST":
        return _response(405, {"ok": False})
    payload = _payload(event)
    latest_path = STATE_DIR / LATEST_STATE_NAME
    if payload.get("action") in ("subscription_status", "mark_unreachable"):
        if not hmac.compare_digest(_header(event, "X-IKON-Bot-Token"), token):
            return _response(403, {"ok": False})
        if payload["action"] == "subscription_status":
            return _response(200, {
                "ok": True, "active_subscriber_count": len(_recipients()),
                "registered_count": len(_subscribers()),
                "checked_at": monitor._read_json(latest_path).get("latest_checked_at"),
            })
        records = payload.get("recipients")
        if (not isinstance(records, list) or len(records) > MAX_SUBSCRIBERS
                or any(not isinstance(record, dict) or not _valid_chat_id(record.get("chat_id"))
                       or type(record.get("revision")) is not int for record in records)):
            return _response(400, {"ok": False})
        subscribers = _subscribers()
        disabled = 0
        for record in records:
            chat_id = str(record["chat_id"])
            previous = subscribers.get(chat_id)
            # A delayed delivery error must not disable a newer /start.
            if previous and previous.get("last_update_id", -1) == record["revision"]:
                _save_subscriber(chat_id, {**previous, "active": False})
                disabled += 1
        return _response(200, {"ok": True, "disabled": disabled})
    if payload.get("action") == "sync_latest":
        if not hmac.compare_digest(_header(event, "X-IKON-Bot-Token"), token):
            return _response(403, {"ok": False})
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip() or len(message) > 4096:
            return _response(400, {"ok": False, "error": "invalid message"})
        if payload.get("parse_mode") not in (None, "HTML"):
            return _response(400, {"ok": False, "error": "invalid parse_mode"})
        incoming_time = _checked_time(payload.get("checked_at"))
        if incoming_time is None:
            return _response(400, {"ok": False, "error": "invalid checked_at"})
        owner = payload.get("owner_chat_id")
        if owner is not None and not _valid_chat_id(owner):
            return _response(400, {"ok": False, "error": "invalid owner"})
        previous = monitor._read_json(latest_path)
        previous_time = _checked_time(previous.get("latest_checked_at"))
        if previous_time is not None and incoming_time < previous_time:
            return _response(200, {"ok": True, "stored": False, "reason": "older_report"})
        monitor.save_latest_telegram_message(latest_path, message, payload.get("checked_at"),
                                             parse_mode=payload.get("parse_mode"))
        if owner is not None:
            _subscribe(str(owner), -1, owner=True)
        recipients = _recipients()
        return _response(200, {"ok": True, "stored": True, "recipients": recipients,
                               "subscriber_count": len(recipients)})

    received = _header(event, "X-Telegram-Bot-Api-Secret-Token")
    if not hmac.compare_digest(received, webhook_secret(token)):
        return _response(403, {"ok": False})
    command = _private_start(payload)
    if command is None:
        return _response(200, {"ok": True, "sent": False})
    chat_id, update_id = command
    _subscribe(chat_id, update_id)
    reply = monitor.telegram_start_payload(payload, latest_path)
    if reply is None:
        reply = {"chat_id": chat_id, "text": "Первый отчёт придёт после ближайшей проверки."}
    if not _claim_reply(reply["chat_id"]):
        return _response(200, {"ok": True, "sent": False})
    confirmation = "\n\n✅ Автоматические обновления подключены."
    message = reply["text"]
    if len(message) + len(confirmation) <= 4096:
        message += confirmation
    # Telegram executes this sendMessage itself.  No outbound Telegram request
    # is needed from the Russian cloud function, and no new monitor run occurs.
    return _response(
        200,
        {
            "method": "sendMessage",
            "chat_id": reply["chat_id"],
            "text": message,
            "disable_web_page_preview": True,
            **({"parse_mode": "HTML"} if reply.get("parse_mode") == "HTML" else {}),
        },
    )
