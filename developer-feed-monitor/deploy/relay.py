#!/usr/bin/env python3
"""Isolated Telegram subscriber relay for DAR.

The service intentionally uses only the Python standard library. The VPS
monitor stores the latest digest through an authenticated, unguessable URL.
Telegram posts /start and /stop updates to the same URL.  Subscriber state is
kept in SQLite on the VPS.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable


MAX_BODY_BYTES = 32 * 1024
MAX_MESSAGE_LENGTH = 4096
MAX_SUBSCRIBERS = 100
BOT_TOKEN_RE = re.compile(r"^[1-9][0-9]{5,12}:[A-Za-z0-9_-]{20,}$")
CHAT_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _valid_chat_id(value: Any) -> bool:
    return bool(CHAT_ID_RE.fullmatch(str(value)))


def _checked_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


class RelayStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS credentials (
                    tenant TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL,
                    webhook_configured INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS latest (
                    tenant TEXT PRIMARY KEY,
                    message TEXT NOT NULL,
                    checked_at TEXT,
                    parse_mode TEXT,
                    saved_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS subscribers (
                    tenant TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    active INTEGER NOT NULL,
                    last_update_id INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (tenant, chat_id)
                );
                """
            )

    def token_record(self, tenant: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                "SELECT token_hash, webhook_configured FROM credentials WHERE tenant = ?",
                (tenant,),
            ).fetchone()

    def register_token(self, tenant: str, token_hash: str) -> None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO credentials
                    (tenant, token_hash, webhook_configured, updated_at)
                VALUES (?, ?, 0, ?)
                """,
                (tenant, token_hash, now),
            )

    def mark_webhook_configured(self, tenant: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE credentials SET webhook_configured = 1, updated_at = ? WHERE tenant = ?",
                (int(time.time()), tenant),
            )

    def latest(self, tenant: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT message, checked_at, parse_mode, saved_at FROM latest WHERE tenant = ?",
                (tenant,),
            ).fetchone()
        return dict(row) if row is not None else {}

    def save_latest(
        self, tenant: str, message: str, checked_at: str | None, parse_mode: str | None
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO latest (tenant, message, checked_at, parse_mode, saved_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(tenant) DO UPDATE SET
                    message = excluded.message,
                    checked_at = excluded.checked_at,
                    parse_mode = excluded.parse_mode,
                    saved_at = excluded.saved_at
                """,
                (tenant, message, checked_at, parse_mode, int(time.time())),
            )

    def subscribers(self, tenant: str, *, active_only: bool = False) -> list[dict[str, Any]]:
        query = (
            "SELECT chat_id, active, last_update_id, updated_at FROM subscribers "
            "WHERE tenant = ?"
        )
        if active_only:
            query += " AND active = 1"
        query += " ORDER BY chat_id"
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(query, (tenant,)).fetchall()]

    def subscribe(
        self, tenant: str, chat_id: str, update_id: int, *, owner: bool = False
    ) -> bool:
        now = int(time.time())
        with self._connect() as connection:
            previous = connection.execute(
                """
                SELECT active, last_update_id, updated_at
                FROM subscribers WHERE tenant = ? AND chat_id = ?
                """,
                (tenant, chat_id),
            ).fetchone()
            if owner and previous is not None:
                return bool(previous["active"])
            if (
                previous is not None
                and now - int(previous["updated_at"]) < 7 * 86400
                and update_id <= int(previous["last_update_id"])
            ):
                return bool(previous["active"])
            if previous is None:
                count = connection.execute(
                    "SELECT COUNT(*) FROM subscribers WHERE tenant = ?", (tenant,)
                ).fetchone()[0]
                if count >= MAX_SUBSCRIBERS:
                    raise ValueError("subscriber limit reached")
            connection.execute(
                """
                INSERT INTO subscribers
                    (tenant, chat_id, active, last_update_id, updated_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(tenant, chat_id) DO UPDATE SET
                    active = 1,
                    last_update_id = excluded.last_update_id,
                    updated_at = excluded.updated_at
                """,
                (tenant, chat_id, update_id, now),
            )
        return True

    def unsubscribe(self, tenant: str, chat_id: str, update_id: int) -> None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO subscribers
                    (tenant, chat_id, active, last_update_id, updated_at)
                VALUES (?, ?, 0, ?, ?)
                ON CONFLICT(tenant, chat_id) DO UPDATE SET
                    active = 0,
                    last_update_id = excluded.last_update_id,
                    updated_at = excluded.updated_at
                WHERE excluded.last_update_id > subscribers.last_update_id
                   OR excluded.updated_at - subscribers.updated_at >= ?
                """,
                (tenant, chat_id, update_id, now, 7 * 86400),
            )

    def mark_unreachable(self, tenant: str, chat_id: str, revision: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE subscribers SET active = 0, updated_at = ?
                WHERE tenant = ? AND chat_id = ? AND last_update_id = ?
                """,
                (int(time.time()), tenant, chat_id, revision),
            )
        return cursor.rowcount == 1


class RelayApplication:
    def __init__(
        self,
        store: RelayStore,
        public_base: str,
        path_secrets: dict[str, str],
        telegram_request: Callable[[str, str, dict[str, str]], dict[str, Any]] | None = None,
    ):
        self.store = store
        self.public_base = public_base.rstrip("/")
        self.path_secrets = path_secrets
        self.telegram_request = telegram_request or self._telegram_request

    @staticmethod
    def _telegram_request(token: str, method: str, parameters: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/{method}",
            data=urllib.parse.urlencode(parameters).encode("utf-8"),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise RuntimeError(f"Telegram {method} failed")
        return payload

    def _tenant_from_path(self, path: str) -> str | None:
        parsed = urllib.parse.urlsplit(path).path.rstrip("/")
        prefix = urllib.parse.urlsplit(self.public_base).path.rstrip("/")
        tenants = '|'.join(re.escape(name) for name in self.path_secrets)
        match = re.fullmatch(re.escape(prefix) + r"/v1/(" + tenants + r")/([^/]+)", parsed)
        if not match:
            return None
        tenant, supplied = match.groups()
        expected = self.path_secrets.get(tenant, "")
        return tenant if expected and hmac.compare_digest(supplied, expected) else None

    def _configure_webhook(self, tenant: str, token: str) -> None:
        if os.environ.get("RELAY_DELIVERY_MODE") == "polling":
            self.store.mark_webhook_configured(tenant)
            return
        secret = self.path_secrets[tenant]
        url = f"{self.public_base}/v1/{tenant}/{secret}"
        self.telegram_request(
            token,
            "setWebhook",
            {
                "url": url,
                "secret_token": _token_hash(token),
                "allowed_updates": json.dumps(["message"]),
                "drop_pending_updates": "false",
            },
        )
        self.store.mark_webhook_configured(tenant)

    def _authenticate_monitor(
        self, tenant: str, headers: dict[str, str]
    ) -> tuple[bool, str | None, str | None]:
        header = "x-dar-bot-token"
        token = headers.get(header, "")
        if not BOT_TOKEN_RE.fullmatch(token):
            return False, None, "bot token missing"
        digest = _token_hash(token)
        record = self.store.token_record(tenant)
        if record is None:
            self.store.register_token(tenant, digest)
            record = self.store.token_record(tenant)
        if record is None or not hmac.compare_digest(str(record["token_hash"]), digest):
            return False, None, "bot token mismatch"
        if not bool(record["webhook_configured"]):
            try:
                self._configure_webhook(tenant, token)
            except (OSError, RuntimeError, ValueError) as error:
                print(f"{tenant}: setWebhook failed: {type(error).__name__}", file=sys.stderr)
                return False, None, "webhook configuration failed"
        return True, token, None

    def health(self) -> tuple[int, dict[str, Any]]:
        tenants = {
            name: {"configured": self.store.token_record(name) is not None}
            for name in sorted(self.path_secrets)
        }
        return 200, {"ok": True, "service": "dar-telegram-relay", "tenants": tenants}

    def handle(
        self, path: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        tenant = self._tenant_from_path(path)
        if tenant is None:
            return 404, {"ok": False}
        normalized_headers = {str(key).casefold(): str(value) for key, value in headers.items()}
        action = payload.get("action")
        if action in {"sync_latest", "subscription_status", "mark_unreachable"}:
            authenticated, _, error = self._authenticate_monitor(tenant, normalized_headers)
            if not authenticated:
                return (502 if error == "webhook configuration failed" else 403), {
                    "ok": False,
                    "error": error,
                }
            if action == "sync_latest":
                return self._sync_latest(tenant, payload)
            if action == "subscription_status":
                return self._subscription_status(tenant)
            return self._mark_unreachable(tenant, payload)
        return self._telegram_webhook(tenant, normalized_headers, payload)

    def _sync_latest(self, tenant: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        message = payload.get("message")
        if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE_LENGTH:
            return 400, {"ok": False, "error": "invalid message"}
        parse_mode = payload.get("parse_mode")
        if parse_mode not in (None, "HTML"):
            return 400, {"ok": False, "error": "invalid parse_mode"}
        checked_at = payload.get("checked_at")
        incoming_time = _checked_time(checked_at)
        previous = self.store.latest(tenant)
        previous_time = _checked_time(previous.get("checked_at"))
        if incoming_time is not None and previous_time is not None and incoming_time < previous_time:
            return 200, {"ok": True, "stored": False, "reason": "older_report"}
        owner = payload.get("owner_chat_id")
        if owner is not None and not _valid_chat_id(owner):
            return 400, {"ok": False, "error": "invalid owner"}
        self.store.save_latest(tenant, message, checked_at, parse_mode)
        if owner is not None:
            self.store.subscribe(tenant, str(owner), -1, owner=True)
        records = self.store.subscribers(tenant, active_only=True)
        recipients = [
            {"chat_id": record["chat_id"], "revision": record["last_update_id"]}
            for record in records
        ]
        return 200, {
            "ok": True,
            "stored": True,
            "recipients": recipients,
            "subscriber_count": len(recipients),
        }

    def _subscription_status(self, tenant: str) -> tuple[int, dict[str, Any]]:
        all_records = self.store.subscribers(tenant)
        active = sum(bool(record["active"]) for record in all_records)
        return 200, {
            "ok": True,
            "active_subscriber_count": active,
            "registered_count": len(all_records),
            "checked_at": self.store.latest(tenant).get("checked_at"),
        }

    def _mark_unreachable(
        self, tenant: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        records = payload.get("recipients")
        if not isinstance(records, list) or len(records) > MAX_SUBSCRIBERS:
            return 400, {"ok": False}
        disabled = 0
        for record in records:
            if (
                not isinstance(record, dict)
                or not _valid_chat_id(record.get("chat_id"))
                or type(record.get("revision")) is not int
            ):
                return 400, {"ok": False}
            disabled += self.store.mark_unreachable(
                tenant, str(record["chat_id"]), int(record["revision"])
            )
        return 200, {"ok": True, "disabled": disabled}

    def _telegram_webhook(
        self, tenant: str, headers: dict[str, str], payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        record = self.store.token_record(tenant)
        received = headers.get("x-telegram-bot-api-secret-token", "")
        if record is None or not hmac.compare_digest(received, str(record["token_hash"])):
            return 403, {"ok": False}
        message = payload.get("message")
        update_id = payload.get("update_id")
        if not isinstance(message, dict) or type(update_id) is not int or update_id < 0:
            return 200, {"ok": True, "sent": False}
        chat = message.get("chat")
        text = str(message.get("text") or "").strip()
        if not isinstance(chat, dict) or chat.get("type") != "private" or not _valid_chat_id(chat.get("id")):
            return 200, {"ok": True, "sent": False}
        command = re.match(r"^/(start|stop)(?:@\w+)?(?:\s|$)", text, re.IGNORECASE)
        if command is None:
            return 200, {"ok": True, "sent": False}
        chat_id = str(chat["id"])
        if command.group(1).casefold() == "stop":
            self.store.unsubscribe(tenant, chat_id, update_id)
            return 200, {
                "method": "sendMessage",
                "chat_id": chat_id,
                "text": "Автоматические обновления отключены. Чтобы подключить их снова, нажмите /start.",
                "disable_web_page_preview": True,
            }
        self.store.subscribe(tenant, chat_id, update_id)
        latest = self.store.latest(tenant)
        if latest:
            confirmation = "\n\n✅ Автоматические обновления подключены."
            reply = str(latest["message"])
            if len(reply) + len(confirmation) <= MAX_MESSAGE_LENGTH:
                reply += confirmation
        else:
            reply = "✅ Автоматические обновления подключены. Первый отчёт придёт после ближайшей проверки."
        response: dict[str, Any] = {
            "method": "sendMessage",
            "chat_id": chat_id,
            "text": reply,
            "disable_web_page_preview": True,
        }
        if latest.get("parse_mode") == "HTML":
            response["parse_mode"] = "HTML"
        return 200, response


class RelayRequestHandler(BaseHTTPRequestHandler):
    app: RelayApplication

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if urllib.parse.urlsplit(self.path).path.rstrip("/").endswith("/health"):
            self._send(*self.app.health())
        else:
            self._send(404, {"ok": False})

    def do_POST(self) -> None:  # noqa: N802
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send(400, {"ok": False, "error": "invalid content length"})
            return
        if content_length <= 0 or content_length > MAX_BODY_BYTES:
            self._send(413, {"ok": False, "error": "invalid body size"})
            return
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"ok": False, "error": "invalid json"})
            return
        if not isinstance(payload, dict):
            self._send(400, {"ok": False, "error": "invalid payload"})
            return
        try:
            status, response = self.app.handle(self.path, dict(self.headers.items()), payload)
        except (OSError, RuntimeError, sqlite3.Error, ValueError) as error:
            print(f"request failed: {type(error).__name__}", file=sys.stderr)
            status, response = 500, {"ok": False, "error": "internal error"}
        self._send(status, response)

    def log_message(self, format: str, *args: Any) -> None:
        # Never print the secret path. Nginx logs only the normalized URI prefix.
        print(f"relay http status={args[1] if len(args) > 1 else '-'}", file=sys.stderr)


def main() -> None:
    database = Path(os.environ.get("RELAY_DATABASE", "/var/lib/dar-telegram-relay/relay.sqlite3"))
    public_base = os.environ.get("RELAY_PUBLIC_BASE", "").rstrip("/")
    secrets = {
        "dar": os.environ.get("DAR_PATH_SECRET", ""),
    }
    if not public_base.startswith("https://") or any(len(value) < 32 for value in secrets.values()):
        raise SystemExit("RELAY_PUBLIC_BASE and 32+ character tenant path secrets are required")
    host = os.environ.get("RELAY_BIND_HOST", "127.0.0.1")
    port = int(os.environ.get("RELAY_BIND_PORT", "8766"))
    RelayRequestHandler.app = RelayApplication(RelayStore(database), public_base, secrets)
    server = ThreadingHTTPServer((host, port), RelayRequestHandler)
    print(f"dar-telegram-relay listening on {host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
