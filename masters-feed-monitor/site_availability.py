"""Проверка доступности сайтов без базы и без истории.

Модуль принимает уже известный список сайтов, проверяет каждый адрес и
собирает одно текущее уведомление о тех, которые сейчас не открываются.
Прошлые статусы не читаются и не сохраняются. Отправку выполняет вызывающий
код: так модуль можно подключить к уже существующему Telegram-боту.
"""

from __future__ import annotations

import logging
import http.client
import math
import socket
import ssl
import time
import urllib.error
import urllib.request
from typing import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

USER_AGENT = "websites-availability-monitor/1.0"
MAX_MESSAGE_LENGTH = 4000
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_ATTEMPTS = 3
DEFAULT_RETRY_DELAY_SECONDS = 30


@dataclass(frozen=True)
class CheckResult:
    """Итог проверки одного адреса.

    status_code — последний реальный HTTP-код. None означает, что ни одна
    попытка не получила HTTP-ответ. available истинно только для кода 200–399.
    reason заполнен у недоступного сайта и попадает в уведомление.
    """

    status_code: int | None
    available: bool
    reason: str | None = None


def _validate_settings(timeout: float, attempts: int, retry_delay: float) -> None:
    if (
        not math.isfinite(timeout)
        or timeout <= 0
        or type(attempts) is not int
        or attempts < 1
        or not math.isfinite(retry_delay)
        or retry_delay < 0
    ):
        raise ValueError(
            "Таймаут и число попыток должны быть положительными, пауза — конечной и неотрицательной"
        )


def is_available(status_code: int | None) -> bool:
    """Доступен ответ HTTP 200–399. Кода нет — сайт для проверки недоступен."""
    return status_code is not None and 200 <= status_code < 400


def _describe_http_status(status_code: int | None) -> str:
    if status_code is None:
        return "Сервер не вернул HTTP-код ответа."
    descriptions = {
        400: "некорректный запрос к серверу",
        401: "для доступа требуется авторизация",
        403: "сервер запретил доступ к странице",
        404: "страница по этому адресу не найдена",
        408: "сервер не дождался запроса за отведённое время",
        410: "страница больше недоступна по этому адресу",
        429: "слишком много запросов; сервер ограничил частоту обращений",
        500: "внутренняя ошибка сервера",
        502: "ошибка связи между серверами сайта; получен некорректный ответ",
        503: "сервер временно не может обработать запрос",
        504: "один из серверов сайта не ответил вовремя",
    }
    if 400 <= status_code < 500:
        fallback = "сервер отклонил запрос к странице"
    elif 500 <= status_code < 600:
        fallback = "ошибка на стороне сервера"
    else:
        fallback = "получен неожиданный ответ сервера"
    return f"HTTP {status_code} — {descriptions.get(status_code, fallback)}."


def _describe_network_error(error: BaseException, timeout: float) -> str:
    text = str(error).lower()
    if isinstance(error, ssl.SSLError) or "certificate" in text or "ssl" in text:
        if "certificate has expired" in text:
            return "Ошибка HTTPS — срок действия SSL-сертификата истёк."
        if "hostname mismatch" in text or "doesn't match" in text:
            return "Ошибка HTTPS — SSL-сертификат не соответствует имени сайта."
        if "certificate verify failed" in text or "certificate_verify_failed" in text:
            return "Ошибка HTTPS — не удалось проверить SSL-сертификат сайта."
        return "Ошибка HTTPS — не удалось установить защищённое соединение."
    if isinstance(error, TimeoutError) or "timed out" in text:
        return f"Таймаут запроса — превышено время ожидания ({timeout:g} с)."
    if isinstance(error, socket.gaierror) or any(
        marker in text for marker in ("nameresolution", "getaddrinfo", "name or service not known", "nodename nor servname")
    ):
        return "Ошибка DNS — не удалось определить IP-адрес сайта."
    if "too many redirects" in text or "infinite loop" in text:
        return "Ошибка перенаправлений — слишком много переходов между адресами."
    return "Ошибка соединения — соединение с сайтом не установлено или прервано."


def _redirect_loop(error: urllib.error.HTTPError) -> bool:
    text = f"{error.reason} {error}".lower()
    return "infinite loop" in text or "too many redirects" in text


def check_url(
    url: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    attempts: int = DEFAULT_ATTEMPTS,
    retry_delay: float = DEFAULT_RETRY_DELAY_SECONDS,
    user_agent: str = USER_AGENT,
) -> CheckResult:
    """Проверить адрес методом GET, не сохраняя тело страницы.

    Переходы по редиректам выполняются, сертификат TLS проверяется.
    Успешный код 200–399 возвращается сразу. Любой другой HTTP-код и любая
    сетевая ошибка повторяются до исчерпания попыток. Между попытками — пауза,
    после последней попытки паузы нет.
    """
    _validate_settings(timeout, attempts, retry_delay)
    last_status = None
    last_reason = None
    last_was_network = False

    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, headers={"User-Agent": user_agent}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                last_status = getattr(response, "status", None)
            last_was_network = False
            if is_available(last_status):
                return CheckResult(last_status, True)
            last_reason = _describe_http_status(last_status)
        except urllib.error.HTTPError as error:
            error.close()
            if _redirect_loop(error):
                last_status = error.code
                last_was_network = True
                last_reason = _describe_network_error(error, timeout)
            else:
                last_status = error.code
                last_was_network = False
                if is_available(last_status):
                    return CheckResult(last_status, True)
                last_reason = _describe_http_status(last_status)
        except (urllib.error.URLError, TimeoutError, OSError, ssl.SSLError, http.client.HTTPException) as error:
            cause = error.reason if isinstance(error, urllib.error.URLError) and error.reason is not None else error
            last_was_network = True
            last_reason = _describe_network_error(cause, timeout)

        logger.warning("Проверка %s, попытка %s/%s: %s", url, attempt, attempts, last_reason)
        if attempt < attempts and retry_delay:
            time.sleep(retry_delay)

    if last_was_network and last_status is not None:
        last_reason = f"{last_reason} Последний полученный ответ: {_describe_http_status(last_status)}"
    return CheckResult(last_status, False, last_reason or "Ошибка запроса — не удалось получить HTTP-ответ от сайта.")


def check_sites(
    sites: list[dict],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    attempts: int = DEFAULT_ATTEMPTS,
    retry_delay: float = DEFAULT_RETRY_DELAY_SECONDS,
    user_agent: str = USER_AGENT,
    on_result: Callable[[dict, CheckResult], None] | None = None,
) -> list[dict]:
    """Проверить сайты по порядку и вернуть только недоступные сейчас.

    Каждый элемент sites содержит client, project и url. В результат попадают
    те же поля плюс reason. Доступные сайты в список не входят: без истории
    отдельное сообщение о восстановлении не формируется.
    """
    unavailable = []
    results_by_url: dict[str, CheckResult] = {}
    for site in sites:
        client, project, url = site["client"], site["project"], site["url"]
        if url not in results_by_url:
            results_by_url[url] = check_url(url, timeout, attempts, retry_delay, user_agent)
        result = results_by_url[url]
        if on_result is not None:
            on_result(site, result)
        if result.available:
            continue
        unavailable.append(
            {"client": client, "project": project, "url": url, "reason": result.reason}
        )
    return unavailable


def _site_block(site: dict) -> str:
    block = f"🌐 {site['client']} — {site['project']}\n🔗 {site['url']}"
    if site.get("reason"):
        block += f"\n⚠️ {site['reason']}"
    return block


def format_unavailable_messages(sites: list[dict]) -> list[str]:
    """Собрать одно или несколько текущих сообщений о недоступных сайтах.

    Пустой список означает, что отправлять нечего. Длинный список режется так,
    чтобы каждый сайт целиком оставался в одном сообщении.
    """
    if not sites:
        return []
    header = "🔴 Сайты не доступны:"
    messages = []
    current = [header]
    for site in sites:
        block = _site_block(site)
        if _telegram_length(header + "\n\n" + block) > MAX_MESSAGE_LENGTH:
            raise ValueError("Блок одного сайта превышает лимит Telegram")
        candidate = "\n\n".join(current + [block])
        if _telegram_length(candidate) > MAX_MESSAGE_LENGTH and len(current) > 1:
            messages.append("\n\n".join(current))
            current = [header, block]
        else:
            current.append(block)
    if len(current) > 1:
        messages.append("\n\n".join(current))
    return messages


def _telegram_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def run_availability_check(sites: list[dict], send, **options) -> list[str]:
    """Проверить сайты и передать вызывающему коду готовые тексты.

    send(text) вызывается только когда есть недоступные сайты, по одному разу
    на каждое сообщение. Функция ничего не записывает на диск и в базу.
    """
    messages = format_unavailable_messages(check_sites(sites, **options))
    for text in messages:
        send(text)
    return messages
