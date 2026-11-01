#!/usr/bin/env python3
"""Independent 11:30 ALIA report; existing monitor cache/registries are read-only."""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import sqlite3
import sys
from zoneinfo import ZoneInfo

import dashboard
from providers import collect_cian, collect_avito
from render import render

MOSCOW = ZoneInfo('Europe/Moscow')


def environment(path):
    env = {}
    for line in Path(path).read_text().splitlines():
        key, sep, value = line.strip().removeprefix('export ').partition('=')
        if sep and not key.startswith('#'):
            parts = shlex.split(value, comments=True)
            if len(parts) == 1:
                env[key] = parts[0]
    return env


def write_json(path, data):
    pending = path.with_suffix('.pending')
    pending.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.chmod(pending, 0o600)
    pending.replace(path)


def subscribers(path):
    with sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True, timeout=10) as db:
        return sorted({str(r[0]) for r in db.execute(
            'SELECT chat_id FROM subscribers WHERE tenant=? AND active=1', ('asterus',))})


def send_once(db, *, key, chat, text, telegram):
    """Persist the claim before sending. An ambiguous timeout requires manual review."""
    prior = db.execute('SELECT status FROM deliveries WHERE run_key=? AND chat=?', (key, chat)).fetchone()
    if prior and prior[0] in ('sent', 'sending', 'unknown', 'blocked'):
        return prior[0]
    db.execute('INSERT OR REPLACE INTO deliveries VALUES (?,?,?,?,?)',
               (key, chat, 'sending', hashlib.sha256(text.encode()).hexdigest(), None))
    db.commit()
    try:
        result = telegram.call('sendMessage', {'chat_id': chat, 'text': text, 'parse_mode': 'HTML',
                                             'disable_web_page_preview': 'true'})
        status, message_id = 'sent', result['message_id']
    except Exception as error:
        code = getattr(error, 'code', None)
        status, message_id = ('blocked' if code == 403 else 'failed' if code else 'unknown'), None
    db.execute('UPDATE deliveries SET status=?,message_id=? WHERE run_key=? AND chat=?',
               (status, message_id, key, chat))
    db.commit()
    return status


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-dir', default='/var/lib/alia-analytics')
    parser.add_argument('--env-file', default='/etc/asterus-feed-monitor.env')
    parser.add_argument('--google-credentials', default='/etc/alia-analytics-google.json')
    parser.add_argument('--shared-cache', default='/var/lib/asterus-feed-monitor/cache/statistics-residential-cian_1.json')
    parser.add_argument('--subscribers', default='/var/lib/asterus-feed-monitor/subscribers.sqlite3')
    parser.add_argument('--transport-dir', default='/opt/advelop-command-intake')
    parser.add_argument('--send', action='store_true')
    parser.add_argument('--owner-only', action='store_true')
    parser.add_argument('--refresh', action='store_true', help='Refresh draft; does not re-send a delivered day')
    args = parser.parse_args()
    os.umask(0o077)
    root = Path(args.state_dir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / 'run.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another analytics run is active')
            return 0
        now = dt.datetime.now(MOSCOW)
        day = now.date() - dt.timedelta(days=1)
        suffix = '-preview' if args.owner_only or not args.send else ''
        path = root / (day.isoformat() + suffix + '.json')
        text_path = root / (day.isoformat() + suffix + '.html')
        env = environment(args.env_file)
        if args.refresh or suffix or not path.exists():
            old_path = root / ((day - dt.timedelta(days=1)).isoformat() + '.json')
            old = json.loads(old_path.read_text()) if old_path.exists() else None
            report = {'date': day.isoformat(), 'observed_at': now.strftime('%d.%m %H:%M МСК'),
                      'dashboard': dashboard.collect(args.google_credentials, day),
                      'cian': collect_cian(env, day, root, args.shared_cache),
                      'avito': collect_avito(env, day)}
            text = render(report, old)
            if len(text) > 4000:
                raise ValueError('Report exceeds one-message limit; nothing sent')
            write_json(path, report)
            text_path.write_text(text)
        else:
            report = json.loads(path.read_text())
            text = text_path.read_text()
        print(json.dumps({'date': day.isoformat(), 'dashboard': report['dashboard']['status'],
                          'cian': report['cian']['status'], 'avito': report['avito']['status'],
                          'characters': len(text)}, ensure_ascii=False))
        if not args.send:
            print(text)
            return 0
        sys.path.insert(0, args.transport_dir)
        from transport import Telegram
        telegram = Telegram(env['TELEGRAM_BOT_TOKEN'])
        if telegram.call('getMe').get('username') != 'advelop_asterus_bot':
            raise ValueError('Bot identity mismatch; nothing sent')
        chats = [env['TELEGRAM_CHAT_ID']] if args.owner_only else subscribers(args.subscribers)
        with sqlite3.connect(root / 'delivery.sqlite3', timeout=10) as outbox:
            outbox.execute('CREATE TABLE IF NOT EXISTS deliveries '
                           '(run_key TEXT,chat TEXT,status TEXT,body_hash TEXT,message_id INTEGER,'
                           'PRIMARY KEY(run_key,chat))')
            key = day.isoformat() + ('-preview' if args.owner_only else '')
            results = {}
            for chat in chats:
                if not args.owner_only and chat not in subscribers(args.subscribers):
                    continue  # Honor /stop even during a long dispatch.
                outcome = send_once(outbox, key=key, chat=chat, text=text, telegram=telegram)
                results[outcome] = results.get(outcome, 0) + 1
            print(json.dumps({'delivery': results}))
        return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        # Never print traceback/URL containing credentials.
        print('Analytics failed: ' + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
