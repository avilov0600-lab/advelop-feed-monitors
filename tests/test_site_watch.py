import copy
import datetime as dt
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'site-watch'))
sys.path.append(str(ROOT/'telegram-intake'))
import watch
from transport import TelegramError, NetworkError
from site_availability import CheckResult

DOWN = CheckResult(None,False,'Таймаут запроса — 8 секунд')
UP = CheckResult(200,True)


class Sender:
    def __init__(self, error=None):
        self.calls=[]; self.error=error

    def send(self,tenant,chat,body):
        self.calls.append((tenant,chat,body))
        if self.error:
            raise self.error
        return {'message_id':len(self.calls)}


class SiteWatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'state.sqlite3'
        self.site={'id':watch.site_key('dar','https://example.test/'), 'tenant':'dar',
                   'url':'https://example.test/','projects':['Первый ЖК'],'client':'DAR'}
        self.store=watch.Store(self.path)
        self.store.configure([self.site],0)

    def tearDown(self):
        self.tmp.cleanup()

    def confirm(self, recipients=('101',)):
        for now in (0,900,1800): self.store.record(self.site['id'],DOWN,now)
        self.store.seed(self.site['id'],recipients,1800)

    def test_regular_healthy_report_defers_network_fallback(self):
        self.assertTrue(self.store.ingest(self.site['id'],UP,100,100))
        self.assertEqual(self.store.get(self.site['id'])['next_due'],watch.next_regular(100)+600)
        self.assertEqual(self.store.due(101),[])

    def test_repeated_failed_regular_reports_do_not_advance_confirmation(self):
        self.assertTrue(self.store.ingest(self.site['id'],DOWN,100,100))
        self.assertFalse(self.store.ingest(self.site['id'],DOWN,100,110))
        self.assertFalse(self.store.ingest(self.site['id'],DOWN,500,500))
        site=self.store.get(self.site['id'])
        self.assertEqual(site['rounds'],1)
        self.assertEqual(site['next_due'],1000)
        self.store.record(site['id'],DOWN,1000)
        self.assertEqual(self.store.get(site['id'])['rounds'],2)

    def test_healthy_report_closes_incident_before_followup(self):
        self.store.ingest(self.site['id'],DOWN,100,100)
        self.assertTrue(self.store.ingest(self.site['id'],UP,110,120))
        self.assertEqual(self.store.get(self.site['id'])['rounds'],0)
        self.assertEqual(self.store.pending(9999),[])

    def test_stale_future_and_older_reports_are_ignored(self):
        self.assertFalse(self.store.ingest(self.site['id'],DOWN,0,1801))
        self.assertFalse(self.store.ingest(self.site['id'],DOWN,200,100))
        self.store.record(self.site['id'],UP,300)
        self.assertFalse(self.store.ingest(self.site['id'],DOWN,200,301))
        self.assertEqual(self.store.get(self.site['id'])['rounds'],0)
        self.assertIsNone(watch.aware_timestamp('2026-10-30T09:00:00'))
        self.assertIsNone(watch.aware_timestamp('invalid'))
        self.assertIsNotNone(watch.aware_timestamp('2026-10-30T09:00:00+03:00'))

    def test_master_report_reader_uses_header_and_read_only_cache(self):
        path=Path(self.tmp.name)/'relay.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE latest(tenant TEXT,message TEXT,checked_at TEXT)')
            db.execute('INSERT INTO latest VALUES(?,?,?)',('masters','❌ Сайт недоступен — таймаут\nМАСТЕРС','2026-10-30T09:00:00+03:00'))
        site=dict(self.site,tenant='masters',id=watch.site_key('masters',self.site['url']))
        now=watch.aware_timestamp('2026-10-30T09:00:05+03:00')
        self.store.configure([site],now)
        with patch.object(watch,'registry_path',return_value=path):
            reader=watch.ReportReader([site]);reader.ingest(self.store,now)
            reader.ingest(self.store,now+10)
        self.assertEqual(self.store.get(site['id'])['rounds'],1)
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM latest').fetchone()[0],1)

    def test_exact_zero_fifteen_thirty_confirmation(self):
        self.store.record(self.site['id'],DOWN,0)
        self.assertEqual(self.store.due(899),[])
        self.assertEqual(self.store.pending(899),[])
        self.store.record(self.site['id'],DOWN,900)
        self.assertEqual(self.store.pending(1799),[])
        self.assertEqual(self.store.due(1799),[])
        self.store.record(self.site['id'],DOWN,1800)
        self.assertEqual(self.store.get(self.site['id'])['rounds'],3)
        self.store.seed(self.site['id'],['101','102'],1800)
        self.assertEqual(len(self.store.pending(1800)),2)
        self.assertIn('Три проверки подряд',self.store.pending(1800)[0]['body'])

    def test_early_extra_observation_does_not_accelerate_confirmation(self):
        self.store.record(self.site['id'],DOWN,0)
        with self.assertRaises(ValueError): self.store.record(self.site['id'],DOWN,899)
        self.assertEqual(self.store.get(self.site['id'])['rounds'],1)

    def test_recovery_on_second_or_third_check_sends_no_alert(self):
        for failures in (1,2):
            with self.subTest(failures=failures):
                path=Path(self.tmp.name)/('case'+str(failures)+'.sqlite3')
                store=watch.Store(path);store.configure([self.site],0)
                for index in range(failures):store.record(self.site['id'],DOWN,index*900)
                store.record(self.site['id'],UP,failures*900)
                self.assertEqual(store.get(self.site['id'])['rounds'],0)
                self.assertEqual(store.unseeded(),[])
                self.assertEqual(store.pending(9999),[])

    def test_restarts_preserve_due_time_and_do_not_send_again(self):
        self.confirm();sender=Sender()
        watch.deliver(self.store,sender,1800,lambda t:{'101'})
        store=watch.Store(self.path);store.configure([self.site],1900)
        self.assertEqual(store.get(self.site['id'])['next_due'],2700)
        self.assertEqual(store.due(2699),[])
        store.record(self.site['id'],DOWN,2700)
        self.assertEqual(store.unseeded(),[])
        watch.deliver(store,sender,2700,lambda t:{'101'})
        self.assertEqual(len(sender.calls),1)

    def test_recovery_only_for_notified_and_still_subscribed_people(self):
        self.confirm(('101','102'));sender=Sender()
        watch.deliver(self.store,sender,1800,lambda t:{'101','102'})
        self.store.record(self.site['id'],UP,2700)
        watch.deliver(self.store,sender,2700,lambda t:{'101','103'})
        recoveries=[(chat,body) for _,chat,body in sender.calls if 'снова доступен' in body]
        self.assertEqual([chat for chat,_ in recoveries],['101'])

    def test_no_stale_outage_notice_after_recovery_before_delivery(self):
        self.confirm();self.store.record(self.site['id'],UP,2700)
        sender=Sender();watch.deliver(self.store,sender,2700,lambda t:{'101'})
        self.assertEqual(sender.calls,[])

    def test_recovery_during_send_is_delivered_exactly_once(self):
        self.confirm()
        store=self.store; ident=self.site['id']
        class DuringSend(Sender):
            def send(self,tenant,chat,body):
                if 'снова доступен' not in body:store.record(ident,UP,2700)
                return super().send(tenant,chat,body)
        sender=DuringSend()
        watch.deliver(store,sender,1800,lambda t:{'101'})
        watch.deliver(store,sender,2700,lambda t:{'101'})
        watch.deliver(store,sender,2800,lambda t:{'101'})
        self.assertEqual(len(sender.calls),2)
        self.assertIn('снова доступен',sender.calls[-1][2])

    def test_blocked_recipient_does_not_block_other_delivery(self):
        self.confirm(('101','102'))
        class WithBlocked(Sender):
            def send(self,tenant,chat,body):
                if chat=='101':raise TelegramError(403)
                return super().send(tenant,chat,body)
        sender=WithBlocked();watch.deliver(self.store,sender,1800,lambda t:{'101','102'})
        self.assertEqual([x[1] for x in sender.calls],['102'])
        self.assertEqual(self.store.pending(9999),[])

    def test_telegram_429_retry_after_is_respected_and_persisted(self):
        self.confirm();sender=Sender(TelegramError(429,35))
        watch.deliver(self.store,sender,1800,lambda t:{'101'})
        self.assertEqual(self.store.pending(1834),[])
        restarted=watch.Store(self.path)
        sender.error=None
        watch.deliver(restarted,sender,1835,lambda t:{'101'})
        self.assertEqual(len(sender.calls),2)
        self.assertEqual(restarted.pending(9999),[])

    def test_transport_failure_retries_from_actual_completion_time(self):
        self.confirm();sender=Sender(NetworkError())
        watch.deliver(self.store,sender,1800,lambda t:{'101'},clock=lambda:1870)
        self.assertEqual(self.store.pending(1889),[])
        self.assertEqual(len(self.store.pending(1890)),1)

    def test_shared_host_url_is_deduplicated_per_tenant(self):
        config,sites=watch.load_config(ROOT/'site-watch/sites.json')
        self.assertEqual(len(config['tenants']),13)
        self.assertEqual(len({(s['tenant'],s['url']) for s in sites}),len(sites))
        self.assertFalse(config['tenants']['sovcombank']['sites'])
        self.assertGreater(len(next(s for s in sites if s['tenant']=='aston' and len(s['projects'])>1)['projects']),1)

    def test_regular_schedule_is_moscow_and_never_five_minute_polling(self):
        stamp=dt.datetime(2026,10,30,9,tzinfo=watch.MOSCOW).timestamp()
        next_stamp=watch.next_regular(stamp)
        self.assertEqual(dt.datetime.fromtimestamp(next_stamp,watch.MOSCOW).hour,13)
        stamp=dt.datetime(2026,10,30,18,30,tzinfo=watch.MOSCOW).timestamp()
        next_stamp=dt.datetime.fromtimestamp(watch.next_regular(stamp),watch.MOSCOW)
        self.assertEqual((next_stamp.day,next_stamp.hour),(31,9))

    def test_subscriber_lookup_is_tenant_scoped_and_read_only(self):
        path=Path(self.tmp.name)/'registry.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE subscribers(tenant TEXT,chat_id TEXT,active INTEGER)')
            db.executemany('INSERT INTO subscribers VALUES(?,?,?)',[
                ('dar','101',1),('dar','102',0),('dar','-99',1),('ikon','201',1)])
        before=path.read_bytes()
        with patch.object(watch,'registry_path',return_value=path):
            self.assertEqual(watch.recipients('dar'),{'101'})
            self.assertEqual(watch.recipients('ikon'),{'201'})
        self.assertEqual(path.read_bytes(),before)

    def test_no_recovery_for_blocked_or_unnotified_users(self):
        self.confirm();watch.deliver(self.store,Sender(TelegramError(403)),1800,lambda t:{'101'})
        self.store.record(self.site['id'],UP,2700)
        self.assertEqual(self.store.pending(2700),[])

    def test_missing_registry_does_not_lose_confirmed_alert(self):
        for now in (0,900,1800):self.store.record(self.site['id'],DOWN,now)
        self.assertEqual(len(self.store.unseeded()),1)
        store=watch.Store(self.path)
        store.seed(self.site['id'],['101'],1900)
        self.assertEqual(len(store.pending(1900)),1)

    def test_config_removal_cancels_pending_alert_without_touching_subscriptions(self):
        self.confirm();self.store.configure([],2000)
        self.assertEqual(self.store.due(99999),[])
        self.assertEqual(self.store.pending(99999),[])

    def test_new_incident_after_recovery_can_send_a_new_alert(self):
        self.confirm();sender=Sender();watch.deliver(self.store,sender,1800,lambda t:{'101'})
        self.store.record(self.site['id'],UP,2700)
        now=self.store.get(self.site['id'])['next_due']
        for offset in (0,900,1800):self.store.record(self.site['id'],DOWN,now+offset)
        self.store.seed(self.site['id'],['101'],now+1800)
        watch.deliver(self.store,sender,now+1800,lambda t:{'101'})
        self.assertEqual(sum('Три проверки подряд' in x[2] for x in sender.calls),2)

    def test_dynamic_project_and_url_are_html_escaped(self):
        site={**self.site,'client':'<b>fake</b>','projects':['A & B'], 'url':'https://example.test/?a=1&b=2'}
        text=watch.incident_message(site,[0,900,1800],'ошибка <test>')
        self.assertIn('&lt;b&gt;fake&lt;/b&gt;',text)
        self.assertIn('A &amp; B',text)
        self.assertIn('a=1&amp;b=2',text)


if __name__=='__main__':unittest.main()
