import datetime as dt
import tempfile
import unittest
from pathlib import Path
from publication_control import extract_count, daily_cian, MOSCOW, statistics_lines


class PublicationTests(unittest.TestCase):
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


if __name__=='__main__': unittest.main()
