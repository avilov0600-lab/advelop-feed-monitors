"""Root-only recipient diagnostics and targeted retries. Never posts diagnostic names."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shlex
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), '/opt', '/opt/advelop-command-intake']
from telegram_delivery import Ledger, deliver, recipient_ref, send_once

TENANTS = ('masters','dar','ikon','asterus','rodina','plato','aston','summa',
           'donstroy','kortros','afi','apsis','sovcombank')


def paths(tenant):
    if tenant == 'masters':
        folder = Path('/var/lib/advelop-telegram-relay')
        return folder / 'delivery.sqlite3', folder / 'relay.sqlite3'
    state = Path('/var/lib/' + tenant + '-feed-monitor')
    registry = (Path('/var/lib/advelop-telegram-relay/relay.sqlite3') if tenant == 'ikon' else
                Path('/var/lib/dar-telegram-relay/relay.sqlite3') if tenant == 'dar' else
                state / 'subscribers.sqlite3')
    return state / 'delivery.sqlite3', registry


def token_for(tenant):
    if tenant == 'masters':
        return Path('/var/lib/advelop-telegram-relay/intake-tokens/masters.token').read_text().strip()
    for line in Path('/etc/' + tenant + '-feed-monitor.env').read_text().splitlines():
        if line.startswith('TELEGRAM_BOT_TOKEN='):
            return shlex.split(line.split('=', 1)[1], comments=False)[0]
    raise RuntimeError('Bot credential unavailable')


def active_recipients(registry, tenant):
    with closing(sqlite3.connect(registry.as_uri() + '?mode=ro', uri=True)) as con:
        rows = con.execute('SELECT chat_id FROM subscribers WHERE tenant=? AND active=1', (tenant,)).fetchall()
        latest = con.execute('SELECT checked_at FROM latest WHERE tenant=?', (tenant,)).fetchone()
    return {str(row[0]) for row in rows}, latest[0] if latest else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tenant', choices=TENANTS)
    parser.add_argument('action', choices=('status', 'retry'))
    parser.add_argument('--report', help='Exact report key; defaults to latest for status')
    parser.add_argument('--recipient', help='Exact private recipient reference from the journal')
    parser.add_argument('--names', action='store_true', help='Resolve names for this private console only')
    parser.add_argument('--send', action='store_true', help='Perform the single-recipient retry')
    parser.add_argument('--confirmed-missing', action='store_true',
                        help='Operator confirms the recipient did not receive an ambiguous attempt')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Only the server administrator can read recipient diagnostics')
    ledger_path, registry = paths(args.tenant)
    if not ledger_path.exists():
        print('No delivery records yet; previous broadcasts cannot be reconstructed.')
        return
    ledger = Ledger(ledger_path)
    report, rows = ledger.status(args.tenant, args.report)
    if report is None:
        parser.error('Report not found')
    active, latest = active_recipients(registry, args.tenant)
    if args.recipient:
        rows = [row for row in rows if recipient_ref(row['chat_id']) == args.recipient]
        if len(rows) != 1:
            parser.error('Recipient reference is missing or ambiguous')
    import transport
    token = token_for(args.tenant) if args.names or args.send else None
    for row in rows:
        item = {k: row[k] for k in ('state', 'code', 'reason', 'attempts', 'updated_at')}
        item.update(recipient_ref=recipient_ref(row['chat_id']), active=row['chat_id'] in active,
                    report_key=report['report_key'], checked_at=report['checked_at'])
        if args.names:
            try:
                request = urllib.request.Request('https://api.telegram.org/bot' + token + '/getChat',
                    data=urllib.parse.urlencode({'chat_id': row['chat_id']}).encode(), method='POST')
                with transport.urlopen(request, timeout=10) as response:
                    person = json.load(response).get('result', {})
                item.update(username=person.get('username'),
                            name=' '.join(person.get(k, '') for k in ('first_name', 'last_name')).strip())
            except Exception:
                item['name_lookup'] = 'unavailable'
        print(json.dumps(item, ensure_ascii=False))
    if args.action == 'status' or not args.send:
        return
    if not args.report or not args.recipient or len(rows) != 1:
        parser.error('Retry requires an exact --report and --recipient; broadcasts are not supported')
    row = rows[0]
    if row['chat_id'] not in active or latest != report['checked_at']:
        parser.error('Recipient inactive or report superseded; nothing sent')
    if row['state'] == 'accepted':
        parser.error('Telegram already accepted this report; nothing sent')
    if row['state'] in ('unconfirmed', 'attempting'):
        if not args.confirmed_missing or time.time() - row['updated_at'] < 600:
            parser.error('Ambiguous attempt: recipient confirmation and a ten-minute quiet period are required')
        with ledger.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            changed = con.execute('''UPDATE deliveries SET state='rejected',reason='operator_confirmed_missing',
                next_attempt=0 WHERE tenant=? AND report_key=? AND chat_id=? AND state=? AND claim=?''',
                (args.tenant, args.report, row['chat_id'], row['state'], row['claim'])).rowcount
            if changed != 1:
                parser.error('Receipt changed during review; nothing sent')
            con.execute('INSERT INTO delivery_events VALUES(?,?,?,?,?,?,?)',
                        (args.tenant, args.report, row['chat_id'], 'rejected', time.time(), None,
                         'operator_confirmed_missing'))
    def send(chat_id):
        current, current_latest = active_recipients(registry, args.tenant)
        if chat_id not in current or current_latest != report['checked_at']:
            raise RuntimeError('Subscription or report changed')
        return send_once(token, chat_id, report['message'], report['parse_mode'], opener=transport.urlopen)
    result = deliver(ledger, args.tenant, report['message'], report['checked_at'],
                     [row['chat_id']], send, report['parse_mode'])
    if result['rejected'] or result['unconfirmed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
