import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch, MagicMock
import monitor
from config import API_ACCOUNTS, DATA
from deploy.relay import RelayApplication, RelayStore, _token_hash


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'source'

    def test_profitbase_only_available_residential(self):
        offers=''.join('<offer internal-id="'+str(i)+'"><property_type>Квартира</property_type><status>'+status+'</status><area><value>45</value></area><floor>3</floor><house><floors-total>12</floors-total></house><studio>1</studio><rooms>1</rooms></offer>' for i,status in enumerate(['AVAILABLE','BOOKED','SOLD','UNAVAILABLE']))
        self.path.write_text('<realty-feed>'+offers+'</realty-feed>')
        lots,meta=monitor.parse_profitbase(self.path,project='a',category='residential')
        self.assertEqual(len(lots),1)
        self.assertEqual((lots[0].rooms,lots[0].floors_total),(0,12))
        self.assertEqual(meta['excluded_reserved'],1)

    def test_profitbase_invalid_schema_is_not_empty_success(self):
        self.path.write_text('<html/>')
        with self.assertRaises(ValueError):monitor.parse_profitbase(self.path,project='a',category='residential')

    def test_macro_shared_source_filters_project_and_property(self):
        rows=[dict(id=i,parent_id=parent,category=kind,estate_area=45,estate_floor=3,
            is_published=1,is_sold=0,is_reserved=reserved,activity='sell',estate_rooms=2)
            for i,parent,kind,reserved in [(1,10,'flat',0),(2,11,'flat',0),(3,10,'garage',0),(4,10,'flat',1)]]
        self.path.write_text(json.dumps(rows))
        lots,meta=monitor.parse_macro(self.path,project='a',category='residential',source={'parent_ids':[10]})
        self.assertEqual([lot.external_id for lot in lots],['1'])
        self.assertEqual(meta['excluded_reserved'],1)

    def test_macro_missing_project_is_unavailable_not_zero(self):
        self.path.write_text('[{"id":1,"parent_id":99,"category":"flat"}]')
        with self.assertRaises(ValueError):monitor.parse_macro(self.path,project='a',category='residential',source={'parent_ids':[10]})

    def test_macro_uses_bti_area_and_preserves_euro_two_rooms(self):
        self.path.write_text(json.dumps([dict(id=1,parent_id=10,category='flat',estate_area=78.79,
            estate_areaBti=74.5,estate_floor=3,is_published=1,is_sold=0,is_reserved=0,
            activity='sell',estate_rooms=2,estate_studia=1)]))
        lots,_=monitor.parse_macro(self.path,project='a',category='residential',source={'parent_ids':[10]})
        self.assertEqual(str(lots[0].area),'74.5')
        self.assertEqual(lots[0].rooms,2)

    def test_macro_building_height_has_priority_over_entrance(self):
        row=dict(id=1,parent_id=10,category='flat',estate_area=45,estate_floor=3,
                 is_published=1,is_sold=0,is_reserved=0,activity='sell',estate_rooms=2,
                 estate_floors_in_entrance=8,estate_floors_in_house=12)
        self.path.write_text(json.dumps([row]))
        lots,_=monitor.parse_macro(self.path,project='a',category='residential',source={'parent_ids':[10]})
        self.assertEqual(lots[0].floors_total,12)
        self.assertEqual(lots[0].floor,3)
        row['estate_floors_in_house']=None
        self.path.write_text(json.dumps([row]))
        lots,_=monitor.parse_macro(self.path,project='a',category='residential',source={'parent_ids':[10]})
        self.assertEqual(lots[0].floors_total,8)

    def test_shared_maternal_download_has_bounded_longer_timeout(self):
        sources={'residential':{p:{'kind':'macro','url':'https://example.test/export'} for p in ('a','b')}}
        def capture(url,destination,**kwargs):
            destination.write_text('[]')
            return {'status':'OK'}
        with patch.multiple(monitor, WEBSITES={}, MASTER_SOURCES=sources,
                            FEEDS={}, PLATFORM_CARDS={}, CATEGORIES={}):
            with patch.object(monitor,'_capture_download',side_effect=capture) as download:
                status=monitor.download_sources(Path(self.tmp.name))
        self.assertEqual(download.call_count,1)
        self.assertEqual(download.call_args.kwargs['timeout'],60)
        self.assertEqual(status['masters']['b']['status'],'OK')
        self.assertEqual((Path(self.tmp.name)/'master-b.xml').read_text(),'[]')

    def test_capture_propagates_timeout_and_preserves_unavailability(self):
        with patch.object(monitor,'_download',return_value={'status':'OK'}) as download:
            self.assertEqual(monitor._capture_download('https://example.test',self.path,timeout=60)['status'],'OK')
        self.assertEqual(download.call_args.kwargs['timeout'],60)
        with patch.object(monitor,'_download',side_effect=TimeoutError):
            result=monitor._capture_download('https://example.test',self.path,timeout=60)
        self.assertEqual(result['status'],'SOURCE_UNAVAILABLE')

    def test_telegram_retries_transient_network_failure(self):
        response=MagicMock()
        response.__enter__.return_value.read.return_value=b'{"ok": true}'
        with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'12345678:'+'A'*30}):
            with patch.object(monitor,'_telegram_urlopen',side_effect=[urllib.error.URLError('timeout'),response]) as send:
                with patch.object(monitor.time,'sleep') as sleep:
                    self.assertTrue(monitor.send_telegram('report',chat_id='1234')['ok'])
        self.assertEqual(send.call_count,2)
        sleep.assert_called_once_with(2)

    def test_telegram_does_not_retry_blocked_user(self):
        error=urllib.error.HTTPError('https://example.test',403,'Forbidden',{},None)
        with patch.dict(os.environ,{'TELEGRAM_BOT_TOKEN':'12345678:'+'A'*30}):
            with patch.object(monitor,'_telegram_urlopen',side_effect=error) as send:
                with self.assertRaises(urllib.error.HTTPError):
                    monitor.send_telegram('report',chat_id='1234')
        self.assertEqual(send.call_count,1)

    def test_api_xml_scope_uses_account_mapping(self):
        account=next(iter(API_ACCOUNTS))
        projects=API_ACCOUNTS[account]['projects']
        feed_source=API_ACCOUNTS[account]['source']
        feeds={p:{feed_source:[object(),object()]} for p in projects}
        snapshot={'status':'ok','published_count':2*len(projects),'active_count':2*len(projects)}
        result=monitor.analyze_api('residential',account,snapshot,feeds)
        self.assertEqual(result['comparison_status'],'ok')
        del feeds[projects[0]]
        result=monitor.analyze_api('residential',account,snapshot,feeds)
        self.assertIsNone(result['expected_feed_count'])

    def test_new_start_subscribes_and_returns_last_report(self):
        store=RelayStore(Path(self.tmp.name)/'subscribers.sqlite3')
        token='12345678:'+'A'*30
        store.register_token('tenant',_token_hash(token))
        store.save_latest('tenant','<b>Latest</b>','2026-10-12T12:00:00+03:00','HTML')
        app=RelayApplication(store,'https://localhost',{'tenant':'secret'})
        status,result=app.handle('/v1/tenant/secret',{'X-Telegram-Bot-Api-Secret-Token':_token_hash(token)},
            {'update_id':10,'message':{'chat':{'id':1234,'type':'private'},'text':'/start'}})
        self.assertEqual(status,200)
        self.assertIn('<b>Latest</b>',result['text'])
        self.assertEqual(len(store.subscribers('tenant',active_only=True)),1)
        self.assertEqual(store.subscribers('other',active_only=True),[])

    def test_unknown_tenant_and_token_do_not_access_report(self):
        store=RelayStore(Path(self.tmp.name)/'subscribers.sqlite3')
        app=RelayApplication(store,'https://localhost',{'tenant':'secret'})
        status,_=app.handle('/v1/other/secret',{}, {'action':'sync_latest','message':'x'})
        self.assertEqual(status,404)
        status,_=app.handle('/v1/tenant/wrong',{}, {'action':'subscription_status'})
        self.assertEqual(status,404)

    def test_aston_projects_have_disjoint_parent_sets(self):
        clients=json.loads((Path(__file__).resolve().parents[1]/'clients.json').read_text())
        sets=[set(p['master']['parent_ids']) for p in clients['aston']['projects'].values()]
        self.assertEqual(sum(map(len,sets)),len(set.union(*sets)))
        for tenant in ['rodina','plato']:
            self.assertTrue(all(a['source']!='avito' for a in clients[tenant]['accounts'].values()))
            self.assertTrue(all('avito' not in p['feeds'] for p in clients[tenant]['projects'].values()))


if __name__=='__main__':unittest.main()
