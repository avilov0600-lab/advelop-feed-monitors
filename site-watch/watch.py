"""Persistent website-only confirmation and private subscriber alerts. No feed/API collection."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import fcntl
import hashlib
import html
import json
import logging
import os
from pathlib import Path
import re
import shlex
import signal
import sqlite3
import sys
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

sys.path.extend(['/opt', '/opt/advelop-command-intake'])
from site_availability import CheckResult, check_url
from concise_reports import brief_reason

MOSCOW = ZoneInfo('Europe/Moscow')
SLOTS = ((9, 0), (13, 0), (15, 30), (18, 30))
INTERVAL = 15 * 60
TENANTS = {'masters','dar','ikon','asterus','rodina','plato','aston','summa',
           'donstroy','kortros','afi','apsis','sovcombank'}
LOG = logging.getLogger('site-watch')


def next_regular(now):
    current = dt.datetime.fromtimestamp(now, MOSCOW)
    for day in (current.date(), current.date() + dt.timedelta(days=1)):
        for hour, minute in SLOTS:
            candidate = dt.datetime.combine(day, dt.time(hour, minute), MOSCOW).timestamp()
            if candidate > now:
                return candidate
    raise ValueError('No future slot')


def site_key(tenant, url):
    return hashlib.sha256((tenant+'\0'+url).encode()).hexdigest()[:24]


def load_config(path):
    config = json.loads(Path(path).read_text())
    assert config.get('version') == 1
    assert set(config['tenants']) == TENANTS
    sites = []
    for tenant, client in config['tenants'].items():
        assert re.fullmatch(r'[A-Za-z0-9_]+bot', client['username'], re.I)
        grouped = {}
        for row in client['sites']:
            parsed = urlsplit(row['url'])
            assert parsed.scheme in {'http','https'} and parsed.hostname and not parsed.username and not parsed.password
            assert all(isinstance(n, str) and n for n in row['projects'])
            grouped.setdefault(row['url'], []).extend(row['projects'])
        for url, projects in grouped.items():
            sites.append({'id': site_key(tenant,url), 'tenant':tenant, 'url':url,
                          'projects':list(dict.fromkeys(projects)), 'client':client['name']})
    return config, sites


def incident_message(site, checks, reason, *, recovered=False):
    title = html.escape(site['client']) + ' · ' + html.escape(', '.join(site['projects']))
    url = html.escape(site['url'], quote=True)
    if recovered:
        return ('✅ <b>Сайт снова доступен</b>\n'+title+'\n\n'+
                'Ответ сайта получен при повторной проверке.\n'+
                '<a href="'+url+'">Открыть сайт</a>')
    times = ', '.join(dt.datetime.fromtimestamp(stamp, MOSCOW).strftime('%H:%M') for stamp in checks)
    return ('❌ <b>Сайт недоступен</b>\n'+title+'\n\n'+
            'Три проверки подряд завершились ошибкой: '+times+' МСК.\n'+
            'Причина: '+html.escape(reason)+'.\n'+
            '<a href="'+url+'">Открыть сайт</a>\n\n'+
            'Проверьте посадочную страницу и передайте информацию ответственному за сайт.')


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
              CREATE TABLE IF NOT EXISTS sites (
                id TEXT PRIMARY KEY, tenant TEXT NOT NULL, url TEXT NOT NULL,
                projects TEXT NOT NULL, client TEXT NOT NULL, enabled INTEGER NOT NULL,
                next_due REAL NOT NULL, rounds INTEGER NOT NULL DEFAULT 0,
                first_failed REAL, last_checked REAL, last_available INTEGER,
                reason TEXT, status_code INTEGER, incident_id INTEGER,
                confirmed INTEGER NOT NULL DEFAULT 0, seeded INTEGER NOT NULL DEFAULT 0,
                last_report_at REAL);
              CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT, site_id TEXT NOT NULL,
                started REAL NOT NULL, checks TEXT NOT NULL, confirmed_at REAL, recovered_at REAL);
              CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id INTEGER NOT NULL,
                site_id TEXT NOT NULL, tenant TEXT NOT NULL, chat_id TEXT NOT NULL,
                kind TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                next_attempt REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, message_id INTEGER,
                UNIQUE(incident_id,chat_id,kind));
            ''')
            if 'last_report_at' not in {r[1] for r in db.execute('PRAGMA table_info(sites)')}:
                db.execute('ALTER TABLE sites ADD COLUMN last_report_at REAL')
            db.execute("UPDATE outbox SET status='pending' WHERE status='sending'")
            db.execute("UPDATE outbox SET status='cancelled' WHERE status='pending' AND kind='outage' AND incident_id IN (SELECT id FROM incidents WHERE recovered_at IS NOT NULL)")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=10000')
        return db

    def configure(self, sites, now):
        with self.connect() as db:
            db.execute('UPDATE sites SET enabled=0')
            for site in sites:
                db.execute('''INSERT INTO sites(id,tenant,url,projects,client,enabled,next_due)
                    VALUES(?,?,?,?,?,1,?) ON CONFLICT(id) DO UPDATE SET
                    projects=excluded.projects,client=excluded.client,enabled=1''',
                    (site['id'],site['tenant'],site['url'],json.dumps(site['projects'],ensure_ascii=False),site['client'],now))
            db.execute("UPDATE outbox SET status='cancelled' WHERE status='pending' AND site_id IN (SELECT id FROM sites WHERE enabled=0)")

    def get(self, ident):
        with self.connect() as db:
            row = db.execute('SELECT * FROM sites WHERE id=?', (ident,)).fetchone()
        return self.decode(row) if row else None

    @staticmethod
    def decode(row):
        result = dict(row); result['projects'] = json.loads(result['projects']); return result

    def due(self, now):
        with self.connect() as db:
            return [self.decode(r) for r in db.execute('SELECT * FROM sites WHERE enabled=1 AND next_due<=? ORDER BY next_due,id', (now,))]

    def record(self, ident, result, now):
        """One real observation per round; never accelerate delayed/restarted confirmations."""
        with self.connect() as db:
            row = db.execute('SELECT * FROM sites WHERE id=? AND enabled=1', (ident,)).fetchone()
            if row is None:
                return None
            site = self.decode(row)
            if now < site['next_due']:
                raise ValueError('A confirmation is not due yet')
            reason = brief_reason(result.reason, result.status_code) if not result.available else None
            if result.available:
                if site['incident_id'] is not None:
                    incident = site['incident_id']
                    db.execute('UPDATE incidents SET recovered_at=? WHERE id=?', (now,incident))
                    db.execute("UPDATE outbox SET status='cancelled' WHERE incident_id=? AND kind='outage' AND status='pending'", (incident,))
                    if site['confirmed']:
                        body = incident_message(site, [], None, recovered=True)
                        recipients = db.execute("SELECT chat_id FROM outbox WHERE incident_id=? AND kind='outage' AND status='sent'", (incident,)).fetchall()
                        for recipient in recipients:
                            self.queue(db,site,incident,recipient['chat_id'],'recovery',body,now)
                db.execute('''UPDATE sites SET next_due=?,rounds=0,first_failed=NULL,
                    last_checked=?,last_available=1,reason=NULL,status_code=?,incident_id=NULL,
                    confirmed=0,seeded=0 WHERE id=?''', (next_regular(now)+600,now,result.status_code,ident))
            else:
                incident = site['incident_id']
                if incident is None:
                    cursor = db.execute('INSERT INTO incidents(site_id,started,checks) VALUES(?,?,?)',
                                        (ident,now,json.dumps([now])))
                    incident = cursor.lastrowid
                    first = now
                else:
                    first = site['first_failed']
                    checks = json.loads(db.execute('SELECT checks FROM incidents WHERE id=?',(incident,)).fetchone()[0])
                    if len(checks) < 3:
                        checks.append(now)
                        db.execute('UPDATE incidents SET checks=? WHERE id=?', (json.dumps(checks),incident))
                rounds = min(site['rounds']+1,3)
                confirmed = int(rounds == 3)
                if confirmed and not site['confirmed']:
                    db.execute('UPDATE incidents SET confirmed_at=? WHERE id=?', (now,incident))
                db.execute('''UPDATE sites SET next_due=?,rounds=?,first_failed=?,last_checked=?,
                    last_available=0,reason=?,status_code=?,incident_id=?,confirmed=? WHERE id=?''',
                    (now+INTERVAL,rounds,first,now,reason,result.status_code,incident,confirmed,ident))
        return self.get(ident)

    def ingest(self, ident, result, observed_at, now):
        """Use new regular-report observations; stale/duplicate reports never advance rounds."""
        site = self.get(ident)
        if site is None or not site['enabled'] or observed_at > now+60 or now-observed_at > 1800:
            return False
        if site['last_report_at'] is not None and observed_at <= site['last_report_at']:
            return False
        with self.connect() as db:
            db.execute('UPDATE sites SET last_report_at=? WHERE id=?',(observed_at,ident))
        if site['last_checked'] is not None and observed_at <= site['last_checked']:
            return False
        if result.available or site['rounds'] == 0:
            with self.connect() as db:
                db.execute('UPDATE sites SET next_due=? WHERE id=?',(now,ident))
            self.record(ident,result,now)
            return True
        return False

    @staticmethod
    def queue(db, site, incident, chat_id, kind, body, now):
        db.execute('''INSERT OR IGNORE INTO outbox(incident_id,site_id,tenant,chat_id,kind,body,next_attempt)
            VALUES(?,?,?,?,?,?,?)''', (incident,site['id'],site['tenant'],str(chat_id),kind,body,now))

    def unseeded(self):
        with self.connect() as db:
            return [self.decode(r) for r in db.execute('SELECT * FROM sites WHERE enabled=1 AND confirmed=1 AND seeded=0')]

    def seed(self, ident, recipients, now):
        with self.connect() as db:
            row = db.execute('SELECT * FROM sites WHERE id=? AND enabled=1 AND confirmed=1 AND seeded=0',(ident,)).fetchone()
            if row is None:
                return
            site = self.decode(row)
            incident = db.execute('SELECT * FROM incidents WHERE id=?',(site['incident_id'],)).fetchone()
            body = incident_message(site,json.loads(incident['checks']),site['reason'])
            for recipient in recipients:
                self.queue(db,site,incident['id'],recipient,'outage',body,now)
            db.execute('UPDATE sites SET seeded=1 WHERE id=?',(ident,))

    def pending(self, now):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM outbox WHERE status='pending' AND next_attempt<=? ORDER BY id LIMIT 10", (now,))]

    def claim(self, ident):
        with self.connect() as db:
            return db.execute("UPDATE outbox SET status='sending' WHERE id=? AND status='pending'", (ident,)).rowcount == 1

    def finish(self, ident, status, now, message_id=None, delay=None):
        with self.connect() as db:
            item = db.execute("SELECT * FROM outbox WHERE id=? AND status IN ('pending','sending')",(ident,)).fetchone()
            if item is None:
                return
            if delay is not None:
                recovered = db.execute('SELECT recovered_at FROM incidents WHERE id=?',(item['incident_id'],)).fetchone()[0]
                if item['kind'] == 'outage' and recovered is not None:
                    db.execute("UPDATE outbox SET status='cancelled' WHERE id=?",(ident,))
                else:
                    db.execute("UPDATE outbox SET status='pending',attempts=attempts+1,next_attempt=? WHERE id=?", (now+delay,ident))
            else:
                db.execute('UPDATE outbox SET status=?,message_id=?,attempts=attempts+1 WHERE id=?', (status,message_id,ident))
                # Recovery can occur while Telegram is sending an outage notice.
                # Queue a matching recovery even if it completed during that send.
                if status == 'sent' and item['kind'] == 'outage':
                    recovered = db.execute('SELECT recovered_at FROM incidents WHERE id=?',(item['incident_id'],)).fetchone()[0]
                    if recovered is not None:
                        site = self.decode(db.execute('SELECT * FROM sites WHERE id=?',(item['site_id'],)).fetchone())
                        self.queue(db,site,item['incident_id'],item['chat_id'],'recovery',
                                   incident_message(site,[],None,recovered=True),now)


def registry_path(tenant):
    assert tenant in TENANTS
    if tenant in {'masters','ikon'}:
        return Path('/var/lib/advelop-telegram-relay/relay.sqlite3')
    if tenant == 'dar':
        return Path('/var/lib/dar-telegram-relay/relay.sqlite3')
    return Path('/var/lib/'+tenant+'-feed-monitor/subscribers.sqlite3')


def recipients(tenant):
    path = registry_path(tenant)
    # Read only: existing /start, /stop, revisions and subscription state remain authoritative.
    db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=10)
    try:
        rows = db.execute('SELECT chat_id FROM subscribers WHERE tenant=? AND active=1',(tenant,)).fetchall()
        return {str(r[0]) for r in rows if re.fullmatch(r'[1-9][0-9]{0,19}',str(r[0]))}
    finally:
        db.close()


def bot_token(tenant):
    assert tenant in TENANTS
    if tenant == 'masters':
        value = Path('/var/lib/advelop-telegram-relay/intake-tokens/masters.token').read_text().strip()
    else:
        value = None
        for line in Path('/etc/'+tenant+'-feed-monitor.env').read_text().splitlines():
            key,sep,raw = line.strip().removeprefix('export ').partition('=')
            if sep and key == 'TELEGRAM_BOT_TOKEN':
                parts = shlex.split(raw, comments=True)
                value = parts[0] if len(parts) == 1 else None
                break
    if not value or not re.fullmatch(r'[0-9]{6,12}:[A-Za-z0-9_-]{20,}',value):
        raise ValueError('Bot credential is unavailable')
    return value


def aware_timestamp(value):
    try:
        stamp = dt.datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return stamp.timestamp() if stamp.tzinfo else None
    except (ValueError,TypeError):
        return None


class ReportReader:
    def __init__(self, sites):
        self.sites = sites
        self.modified = {}

    def ingest(self, store, now):
        for tenant in sorted({s['tenant'] for s in self.sites}):
            try:
                if tenant == 'masters':
                    db = sqlite3.connect(registry_path(tenant).as_uri()+'?mode=ro',uri=True,timeout=10)
                    try:
                        row = db.execute("SELECT message,checked_at FROM latest WHERE tenant='masters'").fetchone()
                    finally:
                        db.close()
                    if not row:
                        continue
                    first = row[0].splitlines()[0]
                    observed = aware_timestamp(row[1])
                    if first.startswith('✅ Сайт доступен'):
                        result = CheckResult(200,True)
                    elif first.startswith('❌ Сайт недоступен'):
                        result = CheckResult(None,False,first.partition(' — ')[2])
                    else:
                        continue
                    if observed is not None:
                        for site in self.sites:
                            if site['tenant'] == tenant and store.ingest(site['id'],result,observed,now):
                                LOG.info('REPORT_OBSERVATION tenant=%s available=%s',tenant,result.available)
                    continue
                path = Path('/var/lib/'+tenant+'-feed-monitor/latest.json')
                modified = path.stat().st_mtime_ns
                if self.modified.get(tenant) == modified:
                    continue
                report = json.loads(path.read_text())
                for site in self.sites:
                    if site['tenant'] != tenant:
                        continue
                    for observation in report.get('site_availability',{}).values():
                        if observation.get('url') != site['url'] or observation.get('status') not in {'available','unavailable'}:
                            continue
                        observed = aware_timestamp(observation.get('checked_at'))
                        if observed is not None:
                            result = CheckResult(observation.get('status_code'),observation['status']=='available',observation.get('reason'))
                            if store.ingest(site['id'],result,observed,now):
                                LOG.info('REPORT_OBSERVATION tenant=%s available=%s',tenant,result.available)
                self.modified[tenant] = modified
            except (OSError,sqlite3.Error,ValueError,IndexError) as error:
                LOG.warning('REPORT_READ_RETRY tenant=%s error=%s',tenant,type(error).__name__)


class Sender:
    def __init__(self, config):
        from transport import Telegram
        self.telegram = Telegram
        self.config = config
        self.clients = {}

    def send(self, tenant, chat_id, body):
        token = bot_token(tenant)
        stamp = hashlib.sha256(token.encode()).hexdigest()
        if tenant not in self.clients or self.clients[tenant][0] != stamp:
            bot = self.telegram(token)
            try:
                identity = bot.call('getMe')
            except Exception:
                raise RuntimeError('Cannot verify bot identity') from None
            if identity.get('username','').lower() != self.config['tenants'][tenant]['username'].lower():
                raise RuntimeError('Bot identity mismatch')
            self.clients[tenant] = (stamp,bot)
        return self.clients[tenant][1].call('sendMessage',{'chat_id':chat_id,'text':body,
                    'parse_mode':'HTML','disable_web_page_preview':'true'})


def deliver(store, sender, now, get_recipients=recipients, clock=None):
    from transport import TelegramError
    stamp = clock or (lambda: now)
    for item in store.pending(now):
        if not store.claim(item['id']):
            continue
        try:
            if item['chat_id'] not in get_recipients(item['tenant']):
                store.finish(item['id'],'cancelled',stamp())
                continue
            response = sender.send(item['tenant'],item['chat_id'],item['body'])
            store.finish(item['id'],'sent',stamp(),response.get('message_id'))
            LOG.info('ALERT_SENT tenant=%s kind=%s',item['tenant'],item['kind'])
        except TelegramError as error:
            if error.code in (400,403):
                store.finish(item['id'],'blocked' if error.code == 403 else 'rejected',stamp())
            else:
                delay = max(1,error.retry_after or 30) if error.code == 429 else min(300,20*2**min(item['attempts'],4))
                store.finish(item['id'],'pending',stamp(),delay=delay)
            LOG.warning('ALERT_TELEGRAM_ERROR tenant=%s code=%s',item['tenant'],error.code)
        except Exception as error:
            store.finish(item['id'],'pending',stamp(),delay=min(300,20*2**min(item['attempts'],4)))
            LOG.warning('ALERT_RETRY tenant=%s error=%s',item['tenant'],type(error).__name__)


def run(config_path, state_path):
    config, sites = load_config(config_path)
    Path(state_path).parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    lock = open(Path(state_path).parent/'worker.lock','a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    store = Store(state_path)
    store.configure(sites,time.time())
    sender = Sender(config)
    reader = ReportReader(sites)
    next_read = 0
    stop = [False]
    for signum in (signal.SIGTERM,signal.SIGINT):
        signal.signal(signum, lambda *_: stop.__setitem__(0,True))
    LOG.info('WATCH_STARTED tenants=%s configured_sites=%s interval_seconds=%s',len(config['tenants']),len(sites),INTERVAL)
    active = {}
    delivery = None
    with ThreadPoolExecutor(max_workers=5) as pool:
        while not stop[0]:
            now = time.time()
            for ident,(future,host) in list(active.items()):
                if not future.done():
                    continue
                del active[ident]
                try:
                    result = future.result()
                    current = store.record(ident,result,time.time())
                    if current:
                        LOG.info('SITE_CHECK tenant=%s available=%s round=%s confirmed=%s',
                                 current['tenant'],result.available,current['rounds'],current['confirmed'])
                except Exception as error:
                    # A checker defect is not evidence of downtime. Back off without advancing rounds.
                    with store.connect() as db:
                        db.execute('UPDATE sites SET next_due=? WHERE id=?',(time.time()+60,ident))
                    LOG.warning('PROBE_ERROR error=%s',type(error).__name__)
            if now >= next_read:
                reader.ingest(store,now)
                next_read = now+15
            busy_hosts = {host for _,host in active.values()}
            for site in store.due(now):
                host = urlsplit(site['url']).hostname
                if len(active) >= 4:
                    break
                if site['id'] in active or host in busy_hosts:
                    continue
                future = pool.submit(check_url,site['url'],timeout=8,attempts=3,retry_delay=2)
                active[site['id']] = (future,host)
                busy_hosts.add(host)
            for site in store.unseeded():
                try:
                    store.seed(site['id'],recipients(site['tenant']),time.time())
                except (OSError,sqlite3.Error) as error:
                    LOG.warning('REGISTRY_RETRY tenant=%s error=%s',site['tenant'],type(error).__name__)
            if delivery is not None and delivery.done():
                try:
                    delivery.result()
                except Exception as error:
                    LOG.warning('DELIVERY_WORKER_RETRY error=%s',type(error).__name__)
                    # Only one delivery worker exists; release interrupted claims for retry.
                    with store.connect() as db:
                        db.execute("UPDATE outbox SET status='pending',next_attempt=? WHERE status='sending'",(time.time()+30,))
                        db.execute("UPDATE outbox SET status='cancelled' WHERE status='pending' AND kind='outage' AND incident_id IN (SELECT id FROM incidents WHERE recovered_at IS NOT NULL)")
                delivery = None
            if delivery is None and store.pending(time.time()):
                delivery = pool.submit(deliver,store,sender,time.time(),recipients,time.time)
            time.sleep(2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    parser.add_argument('--state')
    parser.add_argument('--preflight',action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(levelname)s %(message)s')
    if args.preflight:
        config,sites=load_config(args.config)
        counts={tenant:len(recipients(tenant)) for tenant in config['tenants']}
        for tenant in config['tenants']:bot_token(tenant)
        print('PREFLIGHT_OK tenants='+str(len(config['tenants']))+' sites='+str(len(sites)))
    else:
        assert args.state, 'State database required'
        run(args.config,args.state)
