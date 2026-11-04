"""Private, durable report receipts. Unknown sends are never automatically replayed."""
import datetime as dt
from contextlib import contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


REASONS = {'accepted', 'bot_blocked', 'account_deactivated', 'chat_not_found',
           'rate_limited', 'invalid_formatting', 'message_too_long', 'telegram_rejected',
           'invalid_response', 'network', 'unexpected_error', 'operator_confirmed_missing',
           'pre_journal_unknown'}


def recipient_ref(chat_id):
    return hashlib.sha256(str(chat_id).encode()).hexdigest()[:16]


def report_identity(tenant, message, checked_at, parse_mode):
    if not isinstance(checked_at, str):
        raise ValueError('Missing report time')
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', tenant):
        raise ValueError('Invalid tenant')
    when = dt.datetime.fromisoformat(checked_at.replace('Z', '+00:00'))
    if when.tzinfo is None:
        raise ValueError('Report time must include timezone')
    if not isinstance(message, str) or not message.strip() or len(message.encode('utf-16-le')) // 2 > 4096:
        raise ValueError('Invalid message')
    if parse_mode not in (None, 'HTML'):
        raise ValueError('Invalid parse mode')
    stamp = when.astimezone(dt.timezone.utc).isoformat()
    key = hashlib.sha256((tenant + '\0' + stamp).encode()).hexdigest()
    digest = hashlib.sha256(json.dumps([message, parse_mode], ensure_ascii=False).encode()).hexdigest()
    return key, digest


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        os.chmod(self.path, 0o600)
        with self.connect() as con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS reports (
                    tenant TEXT NOT NULL, report_key TEXT NOT NULL, checked_at TEXT NOT NULL,
                    message TEXT NOT NULL, parse_mode TEXT, message_hash TEXT NOT NULL,
                    created_at REAL NOT NULL, PRIMARY KEY(tenant,report_key));
                CREATE TABLE IF NOT EXISTS deliveries (
                    tenant TEXT NOT NULL, report_key TEXT NOT NULL, chat_id TEXT NOT NULL,
                    state TEXT NOT NULL, claim TEXT NOT NULL, attempts INTEGER NOT NULL,
                    updated_at REAL NOT NULL, code INTEGER, reason TEXT, next_attempt REAL NOT NULL,
                    message_id INTEGER, PRIMARY KEY(tenant,report_key,chat_id));
                CREATE TABLE IF NOT EXISTS delivery_events (
                    tenant TEXT NOT NULL, report_key TEXT NOT NULL, chat_id TEXT NOT NULL,
                    state TEXT NOT NULL, happened_at REAL NOT NULL, code INTEGER, reason TEXT);
            ''')

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def claim(self, tenant, message, checked_at, chat_id, parse_mode=None):
        chat_id = str(chat_id)
        if not re.fullmatch(r'[1-9][0-9]{0,19}', chat_id):
            raise ValueError('Invalid private recipient')
        key, digest = report_identity(tenant, message, checked_at, parse_mode)
        now = time.time()
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            report = con.execute('SELECT message_hash FROM reports WHERE tenant=? AND report_key=?',
                                 (tenant, key)).fetchone()
            if report and report['message_hash'] != digest:
                raise ValueError('Report content changed at the same observation time; use the saved report')
            con.execute('INSERT OR IGNORE INTO reports VALUES(?,?,?,?,?,?,?)',
                        (tenant, key, checked_at, message, parse_mode, digest, now))
            row = con.execute('SELECT * FROM deliveries WHERE tenant=? AND report_key=? AND chat_id=?',
                              (tenant, key, chat_id)).fetchone()
            if row and (row['state'] not in {'pending', 'rejected'} or row['next_attempt'] > now):
                return {'ok': True, 'claimed': False, 'state': row['state'], 'report_key': key,
                        'code': row['code'], 'reason': row['reason']}
            claim = uuid.uuid4().hex
            con.execute('''INSERT INTO deliveries VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(tenant,report_key,chat_id) DO UPDATE SET state='attempting',
                claim=excluded.claim,attempts=deliveries.attempts+1,updated_at=excluded.updated_at,
                code=NULL,reason=NULL,next_attempt=0,message_id=NULL''',
                (tenant, key, chat_id, 'attempting', claim, 1, now, None, None, 0, None))
            con.execute('INSERT INTO delivery_events VALUES(?,?,?,?,?,?,?)',
                        (tenant, key, chat_id, 'attempting', now, None, None))
        return {'ok': True, 'claimed': True, 'claim': claim, 'report_key': key}

    def finish(self, tenant, report_key, chat_id, claim, receipt):
        state = receipt.get('state')
        if state not in {'accepted', 'rejected', 'unconfirmed'} or receipt.get('reason') not in REASONS:
            raise ValueError('Invalid receipt')
        code, message_id = receipt.get('code'), receipt.get('message_id')
        if code is not None and (type(code) is not int or not 100 <= code <= 599):
            raise ValueError('Invalid error code')
        if state == 'accepted' and (type(message_id) is not int or message_id <= 0):
            raise ValueError('Message receipt required')
        delay = receipt.get('retry_after', 0)
        if type(delay) is not int or not 0 <= delay <= 86400:
            raise ValueError('Invalid retry delay')
        now = time.time()
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT * FROM deliveries WHERE tenant=? AND report_key=? AND chat_id=?',
                              (tenant, report_key, str(chat_id))).fetchone()
            if not row or row['claim'] != claim:
                raise ValueError('Stale delivery claim')
            if row['state'] != 'attempting':
                if row['state'] == state and row['message_id'] == message_id and row['reason'] == receipt['reason']:
                    return {'ok': True, 'stored': True}
                raise ValueError('Receipt already finalized')
            con.execute('''UPDATE deliveries SET state=?,updated_at=?,code=?,reason=?,next_attempt=?,message_id=?
                WHERE tenant=? AND report_key=? AND chat_id=? AND claim=?''',
                (state, now, code, receipt['reason'], now + delay, message_id,
                 tenant, report_key, str(chat_id), claim))
            con.execute('INSERT INTO delivery_events VALUES(?,?,?,?,?,?,?)',
                        (tenant, report_key, str(chat_id), state, now, code, receipt['reason']))
        return {'ok': True, 'stored': True}

    def status(self, tenant, report_key=None):
        with self.connect() as con:
            report = con.execute('SELECT * FROM reports WHERE tenant=? ' +
                ('AND report_key=?' if report_key else 'ORDER BY created_at DESC LIMIT 1'),
                (tenant, report_key) if report_key else (tenant,)).fetchone()
            if not report:
                return None, []
            rows = con.execute('SELECT * FROM deliveries WHERE tenant=? AND report_key=? ORDER BY chat_id',
                               (tenant, report['report_key'])).fetchall()
        return dict(report), [dict(row) for row in rows]

    def prepare(self, tenant, message, checked_at, recipients, parse_mode=None):
        """Record every intended recipient before the first network send."""
        return self._prepare(tenant, message, checked_at, recipients, parse_mode, 'pending', None)

    def seed_previous(self, tenant, message, checked_at, recipients, parse_mode=None):
        """Existing reports have no historical receipts; never assume they failed."""
        return self._prepare(tenant, message, checked_at, recipients, parse_mode,
                             'unconfirmed', 'pre_journal_unknown')

    def _prepare(self, tenant, message, checked_at, recipients, parse_mode, state, reason):
        key, digest = report_identity(tenant, message, checked_at, parse_mode)
        now = time.time()
        added = 0
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            report = con.execute('SELECT message_hash FROM reports WHERE tenant=? AND report_key=?',
                                 (tenant, key)).fetchone()
            if report and report['message_hash'] != digest:
                raise ValueError('Report content changed at the same observation time; use the saved report')
            con.execute('INSERT OR IGNORE INTO reports VALUES(?,?,?,?,?,?,?)',
                        (tenant, key, checked_at, message, parse_mode, digest, now))
            for chat_id in dict.fromkeys(map(str, recipients)):
                if not re.fullmatch(r'[1-9][0-9]{0,19}', chat_id):
                    raise ValueError('Invalid private recipient')
                changed = con.execute('INSERT OR IGNORE INTO deliveries VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (tenant, key, chat_id, state, uuid.uuid4().hex, 0, now, None,
                     reason, 0, None)).rowcount
                added += changed
                if changed:
                    con.execute('INSERT INTO delivery_events VALUES(?,?,?,?,?,?,?)',
                        (tenant, key, chat_id, state, now, None, reason))
        return added


class RemoteLedger:
    def __init__(self, request):
        self.request = request

    def prepare(self, tenant, message, checked_at, recipients, parse_mode=None):
        response = self.request({'action': 'delivery_prepare', 'message': message, 'checked_at': checked_at,
                                 'recipients': list(map(str, recipients)), 'parse_mode': parse_mode})
        if response.get('ok') is not True or response.get('prepared') is not True:
            raise RuntimeError('Delivery journal did not confirm recipient plan; nothing sent')

    def claim(self, tenant, message, checked_at, chat_id, parse_mode=None):
        response = self.request({'action': 'delivery_claim', 'message': message, 'checked_at': checked_at,
                                 'chat_id': str(chat_id), 'parse_mode': parse_mode})
        if not response.get('ok') or type(response.get('claimed')) is not bool:
            raise RuntimeError('Delivery journal did not confirm claim; nothing sent')
        return response

    def finish(self, tenant, report_key, chat_id, claim, receipt):
        payload = {'action': 'delivery_finish', 'report_key': report_key, 'chat_id': str(chat_id),
                   'claim': claim, 'receipt': receipt}
        # Receipt storage is idempotent; retrying storage never resends a message.
        for attempt in range(3):
            try:
                result = self.request(payload)
                if result.get('ok') and result.get('stored'):
                    return result
            except (OSError, ValueError, RuntimeError, http.client.HTTPException):
                if attempt == 2:
                    raise RuntimeError('Delivery receipt storage unavailable') from None
        raise RuntimeError('Delivery receipt storage rejected')


def classify(payload, code=None):
    if not isinstance(payload, dict) or payload.get('ok') is not False or type(payload.get('error_code')) is not int:
        return {'state': 'unconfirmed', 'code': code, 'reason': 'invalid_response'}
    code = payload['error_code']
    if not 400 <= code <= 599:
        return {'state': 'unconfirmed', 'reason': 'invalid_response'}
    reason = 'telegram_rejected'
    description = str(payload.get('description', '')).lower()
    for phrase, label in [('bot was blocked by the user', 'bot_blocked'),
                          ('user is deactivated', 'account_deactivated'), ('chat not found', 'chat_not_found'),
                          ('too many requests', 'rate_limited'), ("can't parse entities", 'invalid_formatting'),
                          ('message is too long', 'message_too_long')]:
        if phrase in description:
            reason = label
            break
    receipt = {'state': 'rejected', 'code': code, 'reason': reason}
    params = payload.get('parameters')
    delay = params.get('retry_after') if isinstance(params, dict) else None
    if type(delay) is int and 0 <= delay <= 86400:
        receipt['retry_after'] = delay
    return receipt


def send_once(token, chat_id, message, parse_mode=None, *, opener=urllib.request.urlopen):
    payload = {'chat_id': str(chat_id), 'text': message, 'disable_web_page_preview': 'true'}
    if parse_mode:
        payload['parse_mode'] = parse_mode
    request = urllib.request.Request('https://api.telegram.org/bot' + token + '/sendMessage',
                                     data=urllib.parse.urlencode(payload).encode(), method='POST')
    try:
        with opener(request, timeout=15) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            result = json.loads(error.read(65536))
        except (OSError, ValueError, http.client.HTTPException):
            result = None
        finally:
            error.close()
        return classify(result, error.code)
    except (OSError, http.client.HTTPException):
        return {'state': 'unconfirmed', 'reason': 'network'}
    except ValueError:
        return {'state': 'unconfirmed', 'reason': 'invalid_response'}
    if not isinstance(result, dict) or result.get('ok') is not True:
        return classify(result)
    sent = result.get('result')
    if not isinstance(sent, dict) or type(sent.get('message_id')) is not int or sent['message_id'] <= 0:
        return {'state': 'unconfirmed', 'reason': 'invalid_response'}
    return {'state': 'accepted', 'reason': 'accepted', 'message_id': sent['message_id']}


def deliver(ledger, tenant, message, checked_at, recipients, send, parse_mode=None):
    recipients = list(dict.fromkeys(map(str, recipients)))
    ledger.prepare(tenant, message, checked_at, recipients, parse_mode)
    summary = {'subscribers': len(set(map(str, recipients))), 'delivered': 0,
               'skipped': 0, 'rejected': 0, 'unconfirmed': 0}
    for chat_id in dict.fromkeys(map(str, recipients)):
        for attempt in range(3):
            claim = ledger.claim(tenant, message, checked_at, chat_id, parse_mode)
            ref = recipient_ref(chat_id)
            if not claim['claimed']:
                state = claim['state']
                summary['skipped'] += state == 'accepted'
                summary['rejected'] += state == 'rejected'
                summary['unconfirmed'] += state in {'unconfirmed', 'attempting'}
                print(json.dumps({'event': 'telegram_delivery_skipped', 'tenant': tenant,
                                  'recipient_ref': ref, 'report_key': claim['report_key'], 'state': state}), flush=True)
                break
            print(json.dumps({'event': 'telegram_delivery_attempt', 'tenant': tenant,
                              'recipient_ref': ref, 'report_key': claim['report_key']}), flush=True)
            try:
                receipt = send(chat_id)
            except Exception:
                receipt = {'state': 'unconfirmed', 'reason': 'unexpected_error'}
            ledger.finish(tenant, claim['report_key'], chat_id, claim['claim'], receipt)
            print(json.dumps({'event': 'telegram_delivery', 'tenant': tenant, 'recipient_ref': ref,
                              'report_key': claim['report_key'], **receipt}), flush=True)
            if (receipt['state'] == 'rejected' and receipt.get('code') == 429 and attempt < 2
                    and type(receipt.get('retry_after')) is int and receipt['retry_after'] <= 5):
                time.sleep(max(1, receipt['retry_after']))
                continue
            summary[{'accepted': 'delivered', 'rejected': 'rejected', 'unconfirmed': 'unconfirmed'}[receipt['state']]] += 1
            break
    print('Telegram broadcast: ' + ' '.join(f'{key}={value}' for key, value in summary.items()), flush=True)
    return summary


def install_monitor(module, tenant):
    setting = os.environ.get('TELEGRAM_DELIVERY_JOURNAL')
    if not setting:
        return
    tenant = os.environ.get('MONITOR_TENANT', tenant)
    masters = tenant == 'masters'
    parse_mode = None if masters else 'HTML'
    def state_request(payload):
        if not masters:
            return module._telegram_state_request(payload)
        body = json.dumps(payload, ensure_ascii=False).encode()
        request = urllib.request.Request(os.environ['TELEGRAM_LATEST_MESSAGE_URL'], data=body,
            headers={'X-Masters-Bot-Token': os.environ['TELEGRAM_BOT_TOKEN'], 'Content-Type': 'application/json'},
            method='POST')
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)
    def broadcast(message, checked_at):
        sync = (module.sync_latest_telegram_message if masters else module.sync_latest_message)(message, checked_at)
        if sync.get('stored') is False and sync.get('reason') == 'older_report':
            return {'subscribers': 0, 'delivered': 0, 'skipped_older_report': True}
        recipients = sync.get('recipients')
        if not isinstance(recipients, list) or len(recipients) > 100:
            raise RuntimeError('Subscriber registry unavailable; no untracked sends allowed')
        ids = [str(row['chat_id']) if isinstance(row, dict) else str(row) for row in recipients]
        if any(not re.fullmatch(r'[1-9][0-9]{0,19}', chat_id) for chat_id in ids):
            raise ValueError('Invalid subscriber registry')
        ledger = RemoteLedger(state_request) if setting == 'remote' else Ledger(setting)
        opener = urllib.request.urlopen if masters else module._telegram_urlopen
        send = lambda chat_id: send_once(os.environ['TELEGRAM_BOT_TOKEN'], chat_id, message,
                                        parse_mode, opener=opener)
        summary = deliver(ledger, tenant, message, checked_at, ids, send, parse_mode)
        if summary['rejected'] or summary['unconfirmed']:
            raise RuntimeError('Report delivery requires attention; inspect private recipient journal')
        return summary
    setattr(module, 'broadcast_telegram_message' if masters else 'broadcast_telegram', broadcast)
    module.DELIVERY_JOURNAL_ENABLED = True


def install_relay(cls):
    original = cls.handle
    def handle(self, path, headers, payload):
        action = payload.get('action')
        if action not in {'delivery_prepare', 'delivery_claim', 'delivery_finish'}:
            return original(self, path, headers, payload)
        tenant = self._tenant_from_path(path)
        if tenant != 'masters':
            return 404, {'ok': False}
        authenticated, _, _ = self._authenticate_monitor(tenant, {str(k).casefold(): str(v) for k, v in headers.items()})
        if not authenticated:
            return 403, {'ok': False}
        try:
            ledger = Ledger(self.store.path.with_name('delivery.sqlite3'))
            if action in {'delivery_prepare', 'delivery_claim'}:
                latest = self.store.latest(tenant)
                if any(payload.get(k) != latest.get(k) for k in ('message', 'checked_at', 'parse_mode')):
                    return 409, {'ok': False, 'error': 'saved_report_mismatch'}
                active = {str(r['chat_id']) for r in self.store.subscribers(tenant, active_only=True)}
                if action == 'delivery_prepare':
                    recipients = payload.get('recipients')
                    if (not isinstance(recipients, list) or len(recipients) > 100
                            or any(not isinstance(r, str) or r not in active for r in recipients)):
                        return 409, {'ok': False, 'error': 'invalid_recipient_plan'}
                    ledger.prepare(tenant, payload['message'], payload['checked_at'], recipients, payload.get('parse_mode'))
                    return 200, {'ok': True, 'prepared': True}
                if str(payload.get('chat_id')) not in active:
                    return 409, {'ok': False, 'error': 'inactive_recipient'}
                return 200, ledger.claim(tenant, payload['message'], payload['checked_at'], payload['chat_id'], payload.get('parse_mode'))
            return 200, ledger.finish(tenant, payload['report_key'], payload['chat_id'], payload['claim'], payload['receipt'])
        except (KeyError, TypeError, ValueError):
            return 400, {'ok': False, 'error': 'invalid_delivery_request'}
    cls.handle = handle
