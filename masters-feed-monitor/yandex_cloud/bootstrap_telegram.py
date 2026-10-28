#!/usr/bin/env python3
"""Safely collect a Telegram bot token and store it in Yandex Lockbox.

The token is entered in a native macOS hidden-input dialog. It is never
printed, written to disk, or passed as a command-line argument.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parents[1]
YC = PROJECT_DIR / ".tools" / "yandex-cloud" / "bin" / "yc"
DEPLOY_ENV = Path(__file__).with_name("deploy.env")
SECRET_NAME = "masters-feed-monitor-telegram"
TOKEN_RE = re.compile(r"\d{7,16}:[A-Za-z0-9_-]{30,}")


class BootstrapError(RuntimeError):
    pass


def _dialog(script: str) -> str:
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise BootstrapError("Операция отменена в системном окне") from exc
    return result.stdout.strip()


def prompt_token() -> str:
    script = '''
set answer to display dialog "Вставьте НОВЫЙ токен Telegram-бота. Он будет сразу сохранён в Yandex Lockbox." default answer "" with hidden answer buttons {"Отмена", "Сохранить"} default button "Сохранить" cancel button "Отмена" with title "MASTERS Feed Monitor"
return text returned of answer
'''
    dialog_result = _dialog(script)
    match = TOKEN_RE.search(dialog_result)
    if not match:
        raise BootstrapError("Строка не похожа на токен Telegram Bot API")
    return match.group(0)


def _telegram(token: str, method: str, params: dict[str, Any] | None = None) -> Any:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params or {}).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise BootstrapError(f"Telegram отклонил токен, HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise BootstrapError("Не удалось безопасно проверить токен через Telegram") from exc
    if not payload.get("ok"):
        raise BootstrapError(str(payload.get("description") or "Telegram API error"))
    return payload.get("result")


def recent_chats(updates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    chats: dict[str, dict[str, Any]] = {}
    for update in updates:
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            continue
        label = (
            chat.get("title")
            or " ".join(part for part in (chat.get("first_name"), chat.get("last_name")) if part)
            or chat.get("username")
            or str(chat_id)
        )
        chats[str(chat_id)] = {"id": str(chat_id), "label": str(label)}
    return list(chats.values())


def wait_for_chat(token: str) -> dict[str, Any]:
    for attempt in range(3):
        chats = recent_chats(_telegram(token, "getUpdates", {"limit": 100, "timeout": 0}))
        if chats:
            return chats[-1]
        if attempt < 2:
            _dialog('''
display dialog "Откройте созданного бота в Telegram, нажмите «Запустить» или отправьте /start. Затем вернитесь сюда." buttons {"Отмена", "Проверить снова"} default button "Проверить снова" cancel button "Отмена" with title "Нет сообщений от бота"
return "continue"
''')
    raise BootstrapError("Бот не получил /start, поэтому chat_id пока неизвестен")


def _yc(*args: str, input_text: str | None = None, allow_failure: bool = False) -> str:
    if not YC.exists():
        raise BootstrapError("Yandex Cloud CLI не найден в папке проекта")
    result = subprocess.run(
        [str(YC), *args],
        input=input_text,
        capture_output=True,
        text=True,
    )
    if result.returncode and not allow_failure:
        raise BootstrapError("Команда Yandex Cloud завершилась ошибкой")
    return result.stdout


def save_secret(token: str) -> tuple[str, str]:
    secret_payload = json.dumps(
        [{"key": "telegram_bot_token", "text_value": token}],
        ensure_ascii=False,
    )
    existing = _yc(
        "lockbox", "secret", "get", "--name", SECRET_NAME, "--format", "json",
        allow_failure=True,
    )
    if existing.strip():
        secret = json.loads(existing)
        version_raw = _yc(
            "lockbox", "secret", "add-version", secret["id"],
            "--payload", "-", "--format", "json", input_text=secret_payload,
        )
        version = json.loads(version_raw)
        return secret["id"], version["id"]

    secret_raw = _yc(
        "lockbox", "secret", "create",
        "--name", SECRET_NAME,
        "--description", "Telegram token for MASTERS feed monitor",
        "--payload", "-",
        "--format", "json",
        input_text=secret_payload,
    )
    secret = json.loads(secret_raw)
    return secret["id"], secret["current_version"]["id"]


def write_deploy_env(secret_id: str, version_id: str, chat_id: str) -> None:
    folder_id = _yc("config", "get", "folder-id").strip()
    if not folder_id:
        raise BootstrapError("В профиле yc не выбран каталог")
    values = {
        "YC_FOLDER_ID": folder_id,
        "YC_STATE_BUCKET": f"masters-feed-monitor-{folder_id}",
        "YC_TELEGRAM_SECRET_ID": secret_id,
        "YC_TELEGRAM_SECRET_VERSION_ID": version_id,
        "TELEGRAM_CHAT_ID": chat_id,
    }
    DEPLOY_ENV.write_text(
        "".join(f"export {key}='{value}'\n" for key, value in values.items()),
        encoding="utf-8",
    )


def main() -> int:
    try:
        token = prompt_token()
        bot = _telegram(token, "getMe")
        chat = wait_for_chat(token)
        secret_id, version_id = save_secret(token)
        write_deploy_env(secret_id, version_id, chat["id"])
    except BootstrapError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    finally:
        if "token" in locals():
            token = ""  # best-effort release; the token was never persisted locally

    print(f"Готово: @{bot.get('username', 'bot')}, чат «{chat['label']}».")
    print("Токен сохранён в Yandex Lockbox; deploy.env содержит только несекретные ID.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
