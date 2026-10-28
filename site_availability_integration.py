"""Current public-site availability in feed reports; no history or new state files."""
from __future__ import annotations

import datetime as dt
import logging
import json
from pathlib import Path
import urllib.parse
from zoneinfo import ZoneInfo

from site_availability import CheckResult, check_sites

logger = logging.getLogger(__name__)
MOSCOW = ZoneInfo('Europe/Moscow')
# Keep this independent from XML/API timeouts. In particular, MASTERS has a
# three-minute Actions job. The imported module retains its original 30/3/30
# defaults for standalone callers; the scheduled reports use 8/3/2 explicitly.
REPORT_TIMEOUT_SECONDS = 8
REPORT_ATTEMPTS = 3
REPORT_RETRY_DELAY_SECONDS = 2


def read_existing_report(path) -> dict:
    if path is None or not Path(path).is_file():
        raise ValueError('A saved report is required for a site-only refresh')
    report = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(report, dict) or not report.get('checked_at') or not (
            isinstance(report.get('categories'), dict) or isinstance(report.get('feeds'), dict)):
        raise ValueError('Saved feed report is invalid')
    return report


def build_public_sites(client: str, projects: dict, websites: dict) -> list[dict]:
    """Use only explicitly configured public websites, never XML/API/card URLs."""
    sites = []
    for key, url in websites.items():
        if not url:
            continue
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Invalid public website configuration')
        sites.append({'client': client, 'project': projects[key], 'project_key': key, 'url': url})
    return sites


def enrich_report_with_availability(report: dict, sites: list[dict], *, offline: bool = False,
                                    timeout: float = REPORT_TIMEOUT_SECONDS,
                                    attempts: int = REPORT_ATTEMPTS,
                                    retry_delay: float = REPORT_RETRY_DELAY_SECONDS) -> dict:
    """One separate phase after feed analysis. Never change counts or feed status.

    Offline report rendering and /start never perform these requests. Live
    observations belong only to the current report, not a separate cache/DB.
    """
    observations = {}
    checked_at = dt.datetime.now(MOSCOW).isoformat(timespec='seconds')
    for site in sites:
        observations[site['project_key']] = {
            'url': site['url'], 'status': 'not_checked', 'status_code': None,
            'available': None, 'reason': 'Проверка доступности не выполнялась',
            'checked_at': None,
        }
    report['site_availability'] = observations
    if offline:
        return observations

    def record(site: dict, result: CheckResult):
        observations[site['project_key']] = {
            'url': site['url'], 'status': 'available' if result.available else 'unavailable',
            'status_code': result.status_code, 'available': result.available,
            'reason': result.reason, 'checked_at': dt.datetime.now(MOSCOW).isoformat(timespec='seconds'),
        }

    try:
        check_sites(sites, timeout=timeout, attempts=attempts,
                    retry_delay=retry_delay, on_result=record)
    except Exception as error:
        # Availability is auxiliary. A defect in this phase must not prevent
        # delivery of the original lot/feed report or turn missing data into 0.
        logger.warning('Availability phase incomplete: %s', type(error).__name__)
        for value in observations.values():
            if value['status'] == 'not_checked':
                value.update(reason='Проверку доступности выполнить не удалось', checked_at=checked_at)
    return observations


def check_report_sites(report: dict, client: str, projects: dict, websites: dict,
                       *, offline: bool = False) -> dict:
    try:
        sites = build_public_sites(client, projects, websites)
    except (ValueError, TypeError, KeyError):
        report['site_availability'] = {
            key: {'status': 'not_checked', 'available': None, 'status_code': None,
                  'reason': 'Не настроен корректный публичный адрес сайта', 'checked_at': None}
            for key, url in websites.items() if url
        }
        logger.warning('Public website configuration invalid; feed report preserved')
        return report['site_availability']
    return enrich_report_with_availability(report, sites, offline=offline)


def availability_status_line(report: dict, project_key: str, *, compact: bool = False) -> str:
    result = report.get('site_availability', {}).get(project_key, {})
    timestamp = ''
    if result.get('checked_at') and not compact:
        try:
            timestamp = ' · ' + dt.datetime.fromisoformat(result['checked_at']).astimezone(MOSCOW).strftime('%H:%M МСК')
        except (ValueError, TypeError):
            pass
    if result.get('status') == 'available':
        code = result.get('status_code')
        suffix = ' (HTTP ' + str(code) + ')' if code is not None else ''
        return '✅ Сайт отвечает' + suffix + timestamp
    if result.get('status') == 'unavailable':
        reason = result.get('reason') or 'нет ответа'
        if compact:
            reason = reason.split(' — ', 1)[0].rstrip('.')
        return '🔴 Сайт недоступен: ' + reason + timestamp
    return '⚪ Доступность сайта не проверена'
