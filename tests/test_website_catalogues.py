import json
import unittest
from website_catalogues import read_planeta_catalogue, download_chasovaya_catalogue, parse_domdau_catalogue


def row(ident, **values):
    return {'id':ident,'id_house':1189,'status':'free','saleStatus':1,**values}


class CatalogueTests(unittest.TestCase):
    def test_domdau_counts_available_only(self):
        data={'apartments':{'a':{'id':1,'st':1},'b':{'id':2,'st':2},'c':{'id':3,'st':0}}}
        result=parse_domdau_catalogue(json.dumps(data))
        self.assertEqual((result['lot_count'],result['excluded_reserved'],result['ids']),(1,1,['1']))

    def test_domdau_incomplete_unknown_or_duplicate_is_not_zero(self):
        for data in [{}, {'apartments':{}}, {'apartments':{'a':{'id':1,'st':3}}},
                     {'apartments':{'a':{'id':1,'st':1},'b':{'id':1,'st':1}}}]:
            with self.subTest(data=data),self.assertRaises(ValueError):
                parse_domdau_catalogue(json.dumps(data))

    def test_all_pages_and_unique_ids(self):
        pages=[{'count':3,'places':[row(1),row(2)]},{'count':3,'places':[row(3)]}]
        result=read_planeta_catalogue(lambda page:pages[page-1],1189)
        self.assertEqual((result['lot_count'],result['pages']),(3,2))
        self.assertEqual(result['ids'],['1','2','3'])

    def test_partial_changed_duplicate_or_wrong_scope_rejected(self):
        invalid=[{'count':1,'places':[row(1,status='reserved')]},
                 {'count':1,'places':[row(1,id_house=999)]},
                 {'count':1,'places':[row(1,saleStatus=0)]},
                 {'count':2,'places':[row(1),row(1)]},
                 {'count':3,'places':[]}, {'places':[]}, {'count':True,'places':[]}]
        for data in invalid:
            with self.subTest(data=data),self.assertRaises(ValueError):
                read_planeta_catalogue(lambda page:data,1189)
        with self.assertRaises(ValueError):
            read_planeta_catalogue(lambda page:{'count':2 if page==1 else 3,'places':[row(page)]},1189)

    def test_explicit_complete_zero_allowed_not_html_placeholder(self):
        self.assertEqual(read_planeta_catalogue(lambda page:{'count':0,'places':[]},1189)['lot_count'],0)
        with self.assertRaises(ValueError):
            download_chasovaya_catalogue('https://ddoomm.moscow/apartments','Показано: 0 квартир')

    def test_frontend_request_scope_and_ssr_zero_ignored(self):
        requests=[]
        def read(url,headers=None):
            requests.append((url,headers))
            if '/_app-' in url:
                return 'production:"https://api.planetarf.ru/api/v3"},""),l="'+'a'*32+'"'
            return json.dumps({'count':1,'places':[row(7)]})
        document='<script src="/_next/static/chunks/pages/_app-test.js"></script>Показано: 0 квартир'
        result=download_chasovaya_catalogue('https://ddoomm.moscow/apartments',document,read_url=read)
        self.assertEqual(result['lot_count'],1)
        self.assertIn('noBooking%5B%5D=1',requests[1][0])
        self.assertIn('id_house%5B%5D=1189',requests[1][0])
        self.assertNotIn('a'*32,json.dumps(result))


if __name__ == '__main__': unittest.main()
