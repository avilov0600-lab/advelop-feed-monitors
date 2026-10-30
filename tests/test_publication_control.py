import datetime as dt
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from publication_control import extract_count, daily_cian, MOSCOW, statistics_lines, install


class PublicationTests(unittest.TestCase):
    def test_invalid_height_sets_project_and_overall_red_without_master(self):
        project={'status':'ok','master':{'status':'SOURCE_UNAVAILABLE'},'feeds':{'a':{'status':'OK'}}}
        report={'categories':{'r':{'status':'ok','projects':{'p':project},'platform_cards':{}}}}
        m=SimpleNamespace(download_sources=lambda p:{},analyze_directory=lambda p:report,
            SOURCE_UNAVAILABLE='SOURCE_UNAVAILABLE',
            FEED_PARSERS={'a':lambda *a,**k:([SimpleNamespace(floor=40,floors_total=28)],{})},
            _feed_path=lambda *a:Path('unused'))
        install(m,'test')
        result=m.analyze_directory(Path('unused'))
        self.assertEqual(project['feeds']['a']['invalid_floor_above_building'],1)
        self.assertEqual((project['status'],result['overall_status']),('error','error'))

    def test_height_warning_is_grouped_without_hiding_real_floor_errors(self):
        m=SimpleNamespace(download_sources=lambda p:{},analyze_directory=lambda p:{},
            CATEGORIES={'r':'Жилая недвижимость'},PROJECTS={'p':'Проект'},
            SOURCE_UNAVAILABLE='SOURCE_UNAVAILABLE',FEED_LABELS={'a':'Циан','b':'Авито'},
            FIELD_LABELS={'floor':'этаж квартиры'},_feed_counts_line=lambda f:'a 281, b 281',
            _marketing_lines=lambda p:[],_checked_label=lambda t:t)
        comparison={'status':'error','missing_from_feed':[],'extra_in_feed':[],
            'field_mismatch_counts':{'floors_total':281},
            'floors_total_groups':[{'master':85,'feed':87,'lot_count':281}]}
        report={'overall_status':'error','checked_at':'2026-10-14','categories':{'r':{
            'projects':{'p':{'master':{'status':'OK','lot_count':281},'feeds':{},
                'master_vs_feeds':{'a':comparison,'b':dict(comparison)},'cross_feed':{}}},
            'platform_cards':{},'api_checks':{}}}}
        install(m,'test')
        message=m.format_telegram(report)
        self.assertEqual(message.count('Этажность здания:'),1)
        self.assertIn('в источнике 85, в фиде 87',message)
        self.assertNotIn('ошибок полей 281',message)
        self.assertIn('<b>🔴 test</b>',message)
        comparison['field_mismatch_counts']['floor']=2
        message=m.format_telegram(report)
        self.assertIn('Этаж квартиры: с источником — 2',message)
        self.assertIn('Этаж квартиры:',message)
    def test_yandex_real_project_wording(self):
        page='<title>ЖК «РАКУРС» от застройщика Dar — 130 квартир по цене от 19 млн</title><p>Общее число корпусов 4, в настоящий момент продажи 130 квартир открыты в 3 корпусах</p>'
        self.assertEqual(extract_count('yandex',page),130)

    def test_conflicting_counters_not_confirmed(self):
        page='<title>ЖК — 130 квартир по цене от 19 млн</title><p>в настоящий момент продажи 132 квартир открыты</p>'
        self.assertIsNone(extract_count('yandex',page))

    def test_recommendations_and_script_counts_ignored(self):
        page='<title>ЖК Ракурс</title><script>"offersCount":999</script><div>Другой ЖК: 20 квартир от застройщика</div>'
        self.assertIsNone(extract_count('yandex',page))

    def test_zero_and_nonbreaking_space(self):
        self.assertEqual(extract_count('yandex','<title>ЖК — 0 квартир по цене</title>'),0)
        self.assertEqual(extract_count('domclick','<p>1&nbsp;230 предложений от застройщика</p>'),1230)

    def test_statistics_only_yesterday_moscow_and_one_fetch_per_day(self):
        calls=[]
        def request(url,token):
            calls.append(url)
            if 'get-my-offers' in url:
                return {'result':{'totalCount':1,'announcements':[{'id':123}]}}
            return {'result':{'offerId':123,'showsByDays':[{'date':'2026-10-12','shows':21},{'date':'2026-10-13','shows':99}],
                'viewsByDays':[{'date':'2026-10-12','views':2},{'date':'2026-10-13','views':10}]}}
        with tempfile.TemporaryDirectory() as directory:
            now=dt.datetime(2026,10,13,1,tzinfo=MOSCOW)
            stats=daily_cian(Path(directory),'test','test',now=now,request=request)
            self.assertEqual((stats['shows'],stats['views']),(21,2))
            daily_cian(Path(directory),'test','test',now=now,request=request)
        self.assertEqual(len(calls),2)

    def test_partial_statistics_not_presented_as_zero(self):
        def request(url,token):
            if 'get-my-offers' in url:
                return {'result':{'totalCount':1,'announcements':[{'id':123}]}}
            raise TimeoutError()
        with tempfile.TemporaryDirectory() as directory:
            stats=daily_cian(Path(directory),'test','test',request=request)
        self.assertEqual(stats['status'],'partial')
        self.assertIsNone(stats['shows'])
        text=' '.join(statistics_lines({'cian':{'daily_statistics':stats}},{'cian':'Циан'}))
        self.assertIn('итог не рассчитан',text)

    def test_api_cards_removed_and_matching_xml_not_publication_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            m=SimpleNamespace(download_sources=lambda p: {},analyze_directory=lambda p: {},
                PLATFORM_CARDS={'residential':{'p':{'cian':'c','avito':'a','domclick':'d'}}},
                FEED_LABELS={'cian':'Циан','avito':'Авито','domclick':'Домклик'},
                _card_path=lambda *args:Path(directory)/'card.html')
            install(m,'test')
            cards=m.analyze_cards(Path(directory),{},'residential',
                {'p':{'cian':[1,2],'avito':[1,2],'domclick':[1,2]}},{})
        self.assertEqual(set(cards['p']),{'domclick'})
        self.assertEqual(cards['p']['domclick']['status'],'not_verified')
        self.assertIsNone(cards['p']['domclick']['offer_count'])
        self.assertTrue(m._card_has_error(cards))

    def test_partial_day_resumes_without_reloading_inventory(self):
        calls=[]
        def first(url,token):
            calls.append(url)
            if 'get-my-offers' in url:
                return {'result':{'totalCount':1,'announcements':[{'id':123}]}}
            raise TimeoutError()
        def second(url,token):
            calls.append(url)
            self.assertNotIn('get-my-offers',url)
            return {'result':{'offerId':123,'showsByDays':[],'viewsByDays':[]}}
        with tempfile.TemporaryDirectory() as directory:
            daily_cian(Path(directory),'test','test',request=first)
            result=daily_cian(Path(directory),'test','test',request=second)
        self.assertEqual(result['status'],'ok')
        self.assertEqual((result['shows'],result['views']),(0,0))
        self.assertEqual(len(calls),3)


if __name__=='__main__': unittest.main()
