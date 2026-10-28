import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
import monitor
from deploy.relay import RelayStore, RelayApplication, _token_hash
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'deploy'))
from poller import process_update


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'master.json'
        self.addCleanup(self.temp.cleanup)

    def parse(self, rows, project='rakurs'):
        self.path.write_text(json.dumps(rows))
        return monitor.parse_planeta(self.path, project=project, category='residential')

    def row(self, **kwargs):
        return dict(id=30721, t='квартира', st='free', sq=58.1, f=2, fs=44, rc='2', n='К3-7', pr=28984967, **kwargs)

    def test_master_fields_and_number_prefix(self):
        lots, meta = self.parse([self.row()])
        self.assertEqual((lots[0].external_id, lots[0].apartment, lots[0].floor, lots[0].floors_total), ('30721','7',2,44))

    def test_reserved_not_expected_in_advertising(self):
        row = self.row(); row['st'] = 'reserved'
        lots, meta = self.parse([row])
        self.assertEqual(lots, [])
        self.assertEqual(meta['excluded_reserved'], 1)

    def test_empty_or_broken_data_never_green(self):
        for rows in ([], {}, [{'id':1}]):
            with self.assertRaises(ValueError): self.parse(rows)

    def test_chasovaya_apartment_number_preserved(self):
        row=self.row(); row['n']='132'
        lots, _ = self.parse([row], 'chasovaya')
        self.assertEqual(lots[0].apartment,'132')

    def test_cian_studio_code(self):
        path=Path(self.temp.name)/'cian.xml'
        path.write_text('<feed><object><ExternalId>1</ExternalId><FlatRoomsCount>9</FlatRoomsCount></object></feed>')
        lots,_=monitor.parse_cian(path,project='rakurs',category='residential')
        self.assertEqual(lots[0].rooms,0)

    def test_schedule_exact_four_moscow_slots(self):
        timer=(Path(__file__).resolve().parents[1]/'deploy/dar-feed-monitor.timer').read_text()
        slots=[line for line in timer.splitlines() if line.startswith('OnCalendar=')]
        self.assertEqual(slots,['OnCalendar=*-*-* 09,13:00:00 Europe/Moscow','OnCalendar=*-*-* 15,18:30:00 Europe/Moscow'])

    def test_all_source_projects_isolated(self):
        self.assertEqual(set(monitor.PROJECTS), {'chasovaya','rakurs'})
        self.assertEqual(monitor.FEED_PARSERS['yandex3'],monitor.parse_yandex)
        self.assertNotIn('commercial', monitor.CATEGORIES)

    def test_missing_source_no_stale_success(self):
        lots, result = monitor._master_for(Path(self.temp.name), {'masters':{'rakurs':{'status':monitor.SOURCE_UNAVAILABLE}}},'residential','rakurs')
        self.assertIsNone(lots); self.assertIsNone(result['lot_count'])

    def test_repeated_avito_page_is_not_a_complete_count(self):
        page={'resources':[{'id':i} for i in range(1,101)]}
        with patch.dict('os.environ',{'AVITO_CLIENT_ID_RESIDENTIAL':'test','AVITO_CLIENT_SECRET_RESIDENTIAL':'test'}), patch.object(monitor,'_request_json',side_effect=[{'access_token':'test'},page,page]), patch.object(monitor,'AVITO_PAGE_DELAY_SECONDS',0):
            self.assertEqual(monitor.fetch_avito_snapshot('residential')['status'],'unavailable')

    def test_sparse_avito_pages_are_walked_until_a_terminal_empty_suffix(self):
        responses=[{'access_token':'test'},{'resources':[{'id':1}]},{'resources':[]},{'resources':[{'id':2}]},{'resources':[]},{'resources':[]},{'resources':[]},{'resources':[]},{'resources':[]}]
        with patch.dict('os.environ',{'AVITO_CLIENT_ID_RESIDENTIAL':'test','AVITO_CLIENT_SECRET_RESIDENTIAL':'test'}), patch.object(monitor,'_request_json',side_effect=responses), patch.object(monitor,'AVITO_PAGE_DELAY_SECONDS',0):
            result=monitor.fetch_avito_snapshot('residential')
            self.assertEqual(result['status'],'ok')
            self.assertEqual(result['active_count'],2)

    def test_normal_avito_terminal_pages_confirm_count(self):
        responses=[{'access_token':'test'},{'resources':[{'id':1}]},{'resources':[]},{'resources':[]},{'resources':[]},{'resources':[]},{'resources':[]}]
        with patch.dict('os.environ',{'AVITO_CLIENT_ID_RESIDENTIAL':'test','AVITO_CLIENT_SECRET_RESIDENTIAL':'test'}), patch.object(monitor,'_request_json',side_effect=responses), patch.object(monitor,'AVITO_PAGE_DELAY_SECONDS',0):
            result=monitor.fetch_avito_snapshot('residential')
            self.assertEqual(result['status'],'ok')
            self.assertEqual(result['active_count'],1)


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=RelayStore(Path(self.temp.name)/'relay.sqlite3')

    def test_every_start_registers_separate_user(self):
        self.store.subscribe('dar','100',1)
        self.store.subscribe('dar','200',2)
        self.assertEqual(len(self.store.subscribers('dar',active_only=True)),2)
        self.assertEqual(self.store.subscribers('ikon',active_only=True),[])

    def test_stop_and_restart(self):
        self.store.subscribe('dar','100',1)
        self.store.unsubscribe('dar','100',2)
        self.assertEqual(self.store.subscribers('dar',active_only=True),[])
        self.store.subscribe('dar','100',3)
        self.assertEqual(len(self.store.subscribers('dar',active_only=True)),1)

    def test_latest_persisted(self):
        self.store.save_latest('dar','<b>DAR</b>','2026-10-12T17:00:00+03:00','HTML')
        self.assertEqual(self.store.latest('dar')['message'],'<b>DAR</b>')

    def test_only_dar_route(self):
        app=RelayApplication(self.store,'https://example.org/dar-telegram',{'dar':'x'*32})
        self.assertEqual(app._tenant_from_path('/dar-telegram/v1/dar/'+'x'*32),'dar')
        self.assertIsNone(app._tenant_from_path('/dar-telegram/v1/ikon/'+'x'*32))

    def test_start_replays_latest_and_every_user_in_broadcast(self):
        token='123456789:'+'a'*35
        app=RelayApplication(self.store,'https://example.org/dar-telegram',{'dar':'x'*32},
                             telegram_request=lambda *args: {'ok':True})
        path='/dar-telegram/v1/dar/'+'x'*32
        raw={'X-DAR-Bot-Token':token}
        status,sync=app.handle(path,raw,{'action':'sync_latest','message':'<b>DAR</b>',
            'checked_at':'2026-10-12T17:00:00+03:00','parse_mode':'HTML'})
        self.assertEqual(status,200)
        secret={'X-Telegram-Bot-Api-Secret-Token':_token_hash(token)}
        for index,chat in enumerate((100,200),1):
            status,reply=app.handle(path,secret,{'update_id':index,
                'message':{'text':'/start','chat':{'type':'private','id':chat}}})
            self.assertEqual(status,200)
            self.assertIn('<b>DAR</b>',reply['text'])
            self.assertEqual(reply['parse_mode'],'HTML')
        status,sync=app.handle(path,raw,{'action':'sync_latest','message':'<b>DAR 2</b>',
            'checked_at':'2026-10-12T19:00:00+03:00','parse_mode':'HTML'})
        self.assertEqual({r['chat_id'] for r in sync['recipients']},{'100','200'})

    def test_invalid_webhook_secret_cannot_subscribe(self):
        self.store.register_token('dar',_token_hash('123456789:'+'a'*35))
        app=RelayApplication(self.store,'https://example.org/dar-telegram',{'dar':'x'*32})
        status,_=app.handle('/dar-telegram/v1/dar/'+'x'*32,{},
            {'update_id':1,'message':{'text':'/start','chat':{'type':'private','id':100}}})
        self.assertEqual(status,403)
        self.assertEqual(self.store.subscribers('dar'),[])

    def test_polling_start_sends_cached_report_and_subscribes(self):
        token='123456789:'+'a'*35
        self.store.register_token('dar',_token_hash(token))
        self.store.save_latest('dar','<b>DAR</b>','2026-10-12T17:00:00+03:00','HTML')
        app=RelayApplication(self.store,'https://example.org/dar-telegram',{'dar':'x'*32})
        sent=[]
        process_update(app,'/dar-telegram/v1/dar/'+'x'*32,token,
            {'update_id':1,'message':{'text':'/start','chat':{'type':'private','id':100}}},
            lambda token,method,payload:sent.append((method,payload)))
        self.assertEqual(sent[0][0],'sendMessage')
        self.assertIn('<b>DAR</b>',sent[0][1]['text'])
        self.assertEqual(len(self.store.subscribers('dar',active_only=True)),1)

    def test_blocked_command_reply_does_not_block_other_colleagues(self):
        token='123456789:'+'a'*35
        self.store.register_token('dar',_token_hash(token))
        self.store.save_latest('dar','DAR','2026-10-12T17:00:00+03:00','HTML')
        app=RelayApplication(self.store,'https://example.org/dar-telegram',{'dar':'x'*32})
        path='/dar-telegram/v1/dar/'+'x'*32
        def blocked(*args): raise urllib.error.HTTPError('https://example.org',403,'blocked',{},None)
        process_update(app,path,token,{'update_id':1,'message':{'text':'/start','chat':{'type':'private','id':100}}},blocked)
        process_update(app,path,token,{'update_id':2,'message':{'text':'/start','chat':{'type':'private','id':200}}},lambda *args:None)
        self.assertEqual([x['chat_id'] for x in self.store.subscribers('dar',active_only=True)],['200'])

class TelegramNetworkTests(unittest.TestCase):
    def test_sender_delegates_to_shared_resilient_transport(self):
        from unittest.mock import Mock
        backend = Mock()
        with patch.dict(sys.modules, {'advelop_intake_transport':backend}), \
                patch.dict('os.environ', {'TELEGRAM_IP_FAMILY':'ipv6'}), \
                patch.object(monitor.urllib.request,'urlopen') as normal:
            monitor._telegram_urlopen('https://api.telegram.org',timeout=8)
            backend.urlopen.assert_called_once_with('https://api.telegram.org',timeout=8)
            normal.assert_not_called()

    def test_ipv6_connection_keeps_tls_hostname(self):
        connection=monitor._IPv6HTTPSConnection('api.telegram.org')
        self.assertEqual(connection.host,'api.telegram.org')
        self.assertIs(connection._create_connection,monitor._connect_ipv6)
        with patch.object(monitor.socket,'getaddrinfo',return_value=[(None,None,None,None,('2001:db8::1',443,0,0))]), \
                patch.object(monitor.socket,'create_connection') as connect:
            monitor._connect_ipv6(('api.telegram.org',443),8)
            connect.assert_called_once_with(('2001:db8::1',443),8)


if __name__ == '__main__': unittest.main()
