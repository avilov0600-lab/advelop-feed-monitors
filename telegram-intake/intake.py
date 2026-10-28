"""Shared durable /start intake. Each bot retains its own existing subscriber registry."""
import datetime as dt
from contextlib import contextmanager
from html.parser import HTMLParser
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import sys
import time
import urllib.parse

from transport import Telegram, TelegramError, NetworkError


def notify(message):
    address = os.environ.get('NOTIFY_SOCKET')
    if not address:
        return
    if address.startswith('@'):
        address = '\0' + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
            channel.connect(address)
            channel.sendall(message.encode())
    except OSError:
        pass


def log(event, **fields):
    print(json.dumps({'event': event, **fields}, ensure_ascii=False), flush=True)


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain_chunks(response):
    text = response['text']
    if response.get('parse_mode') == 'HTML':
        parser = PlainText()
        parser.feed(text)
        text = ''.join(parser.parts)
    chunks, current, size = [], '', 0
    for character in text:
        width = len(character.encode('utf-16-le')) // 2
        if size + width > 3500:
            chunks.append(current)
            current, size = '', 0
        current += character
        size += width
    if current:
        chunks.append(current)
    return [{**{key: value for key, value in response.items() if key not in {'text', 'parse_mode'}},
             'text': chunk} for chunk in chunks]


class Intake:
    def __init__(self, tenant, app, path, token, database, telegram):
        self.tenant, self.app, self.path = tenant, app, path
        self.token, self.database, self.telegram = token, database, telegram
        with self.connect() as con:
            con.executescript('''
                CREATE TABLE IF NOT EXISTS intake_cursor (
                    tenant TEXT PRIMARY KEY, update_id INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS intake_outbox (
                    tenant TEXT NOT NULL, update_id INTEGER NOT NULL,
                    chat_id TEXT NOT NULL, method TEXT NOT NULL,
                    parts TEXT NOT NULL, part_index INTEGER NOT NULL DEFAULT 0,
                    plain_fallback INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'pending', error_code TEXT,
                    PRIMARY KEY(tenant, update_id));
                CREATE TABLE IF NOT EXISTS intake_health (
                    tenant TEXT PRIMARY KEY, poll_ok_at REAL,
                    command_at REAL, reply_ok_at REAL, last_error TEXT);
            ''')
            con.execute('INSERT OR IGNORE INTO intake_health(tenant) VALUES(?)', (tenant,))
            # Existing dedicated pollers used one cursor per isolated database.
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='poll_cursor'").fetchone():
                previous = con.execute('SELECT update_id FROM poll_cursor WHERE id=1').fetchone()
                if previous:
                    con.execute('INSERT OR IGNORE INTO intake_cursor VALUES(?,?)', (tenant, previous[0]))

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA busy_timeout=10000')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def offset(self):
        with self.connect() as con:
            row = con.execute('SELECT update_id FROM intake_cursor WHERE tenant=?', (self.tenant,)).fetchone()
        return row[0] + 1 if row else 0

    def receive(self, update):
        update_id = update.get('update_id')
        if type(update_id) is not int or update_id < 0:
            raise ValueError('Invalid update ID')
        if update_id < self.offset():
            return
        status, response = self.app.handle(self.path,
            {'X-Telegram-Bot-Api-Secret-Token': self.app.store.token_record(self.tenant)['token_hash']}, update)
        if status != 200:
            raise RuntimeError('Command registry rejected update')
        with self.connect() as con:
            if response.get('method') == 'sendMessage':
                payload = {key: value for key, value in response.items() if key != 'method'}
                # Cancel obsolete pending replies to the same chat (e.g. /start then /stop).
                con.execute("UPDATE intake_outbox SET state='superseded' WHERE tenant=? AND chat_id=? AND state='pending' AND update_id<?",
                            (self.tenant, str(payload['chat_id']), update_id))
                con.execute('INSERT OR IGNORE INTO intake_outbox(tenant,update_id,chat_id,method,parts) VALUES(?,?,?,?,?)',
                            (self.tenant, update_id, str(payload['chat_id']), 'sendMessage', json.dumps([payload])))
                con.execute('UPDATE intake_health SET command_at=? WHERE tenant=?', (time.time(), self.tenant))
            con.execute('INSERT INTO intake_cursor VALUES(?,?) ON CONFLICT(tenant) DO UPDATE SET update_id=excluded.update_id',
                        (self.tenant, update_id))
        if response.get('method'):
            log('command_queued', tenant=self.tenant)

    def drain(self):
        with self.connect() as con:
            rows = con.execute("SELECT * FROM intake_outbox WHERE tenant=? AND state='pending' AND next_attempt<=? ORDER BY update_id LIMIT 20",
                               (self.tenant, time.time())).fetchall()
        for row in rows:
            parts = json.loads(row['parts'])
            try:
                self.telegram.call(row['method'], parts[row['part_index']])
            except TelegramError as error:
                if error.code == 403:
                    self.app.store.mark_unreachable(self.tenant, row['chat_id'], row['update_id'])
                    with self.connect() as con:
                        con.execute("UPDATE intake_outbox SET state='blocked',error_code='403' WHERE tenant=? AND update_id=?", (self.tenant, row['update_id']))
                    log('reply_blocked', tenant=self.tenant)
                elif error.code == 400 and not row['plain_fallback']:
                    fallback = plain_chunks(parts[row['part_index']])
                    with self.connect() as con:
                        con.execute('UPDATE intake_outbox SET parts=?,part_index=0,plain_fallback=1,next_attempt=0 WHERE tenant=? AND update_id=?',
                                    (json.dumps(fallback), self.tenant, row['update_id']))
                elif error.code in (400, 401, 404):
                    with self.connect() as con:
                        con.execute("UPDATE intake_outbox SET state='failed',error_code=? WHERE tenant=? AND update_id=?",
                                    (str(error.code), self.tenant, row['update_id']))
                    log('reply_permanent_failure', tenant=self.tenant, code=error.code)
                else:
                    self.retry(row, str(error.code), error.retry_after)
            except NetworkError:
                self.retry(row, 'network')
            else:
                next_index = row['part_index'] + 1
                state = 'sent' if next_index >= len(parts) else 'pending'
                with self.connect() as con:
                    con.execute('UPDATE intake_outbox SET part_index=?,state=?,next_attempt=0,error_code=NULL WHERE tenant=? AND update_id=?',
                                (next_index, state, self.tenant, row['update_id']))
                    con.execute('UPDATE intake_health SET reply_ok_at=? WHERE tenant=?', (time.time(), self.tenant))
                log('reply_sent', tenant=self.tenant, complete=(state == 'sent'))

    def retry(self, row, code, retry_after=None):
        delay = min(60, 2 ** min(row['attempts'] + 1, 6))
        if type(retry_after) in (int, float):
            delay = max(delay, min(3600, max(1, retry_after)))
        with self.connect() as con:
            con.execute('UPDATE intake_outbox SET attempts=attempts+1,next_attempt=?,error_code=? WHERE tenant=? AND update_id=?',
                        (time.time() + delay, code, self.tenant, row['update_id']))
        log('reply_retry', tenant=self.tenant, code=code)

    def poll(self):
        with self.connect() as con:
            pending = con.execute("SELECT 1 FROM intake_outbox WHERE tenant=? AND state='pending' LIMIT 1", (self.tenant,)).fetchone()
        updates = self.telegram.call('getUpdates', {'offset': self.offset(), 'timeout': 3 if pending else 20,
                                                    'limit': 50, 'allowed_updates': json.dumps(['message'])})
        if not isinstance(updates, list):
            raise ValueError('Invalid updates response')
        for update in updates:
            self.receive(update)
        with self.connect() as con:
            con.execute('UPDATE intake_health SET poll_ok_at=?,last_error=NULL WHERE tenant=?', (time.time(), self.tenant))
        notify('WATCHDOG=1')

    def cleanup(self):
        with self.connect() as con:
            con.execute("DELETE FROM intake_outbox WHERE tenant=? AND state IN ('sent','superseded','blocked') AND update_id < COALESCE((SELECT update_id-1000 FROM intake_cursor WHERE tenant=?),0)", (self.tenant, self.tenant))


def setup():
    tenant = os.environ['INTAKE_TENANT']
    spec = importlib.util.spec_from_file_location('intake_registry', os.environ['INTAKE_RELAY_MODULE'])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    if not token:
        token = Path(os.environ['INTAKE_TOKEN_FILE']).read_text().strip()
    telegram = Telegram(token)
    identity = telegram.call('getMe')
    if identity.get('username', '').casefold() != os.environ['INTAKE_USERNAME'].casefold():
        raise RuntimeError('Bot identity mismatch')
    database = Path(os.environ['INTAKE_DATABASE'])
    store = module.RelayStore(database)
    record = store.token_record(tenant)
    if record is None or record['token_hash'] != module._token_hash(token):
        raise RuntimeError('Registry credential mismatch')
    base = os.environ.get('RELAY_PUBLIC_BASE', 'https://localhost').rstrip('/')
    secret = os.environ[os.environ['INTAKE_SECRET_ENV']]
    path = urllib.parse.urlsplit(base).path + '/v1/' + tenant + '/' + secret
    app = module.RelayApplication(store, base, {tenant: secret})
    # Preserve the backlog. Never consume commands in two workers for the same bot.
    telegram.call('deleteWebhook', {'drop_pending_updates': 'false'})
    store.mark_webhook_configured(tenant)
    worker = Intake(tenant, app, path, token, database, telegram)
    log('intake_ready', tenant=tenant, username=identity['username'])
    notify('READY=1')
    return worker


def main():
    worker = setup()
    cycles = 0
    while True:
        try:
            worker.drain()
            worker.poll()
            worker.drain()
            cycles += 1
            if cycles % 100 == 0:
                worker.cleanup()
        except (NetworkError, TelegramError, OSError, ValueError, RuntimeError, sqlite3.Error) as error:
            code = str(error.code) if isinstance(error, TelegramError) else type(error).__name__
            with worker.connect() as con:
                con.execute('UPDATE intake_health SET last_error=? WHERE tenant=?', (code, worker.tenant))
            log('intake_retry', tenant=worker.tenant, code=code)
            # No watchdog heartbeat on failed polls: systemd restarts stalled intake.
            time.sleep(3)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        log('intake_startup_failure', error_class=type(error).__name__)
        raise SystemExit(1) from None
