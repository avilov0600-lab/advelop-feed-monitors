import unittest
from types import SimpleNamespace as Row
from floor_control import height_profile,height_lines,non_height_counts,cross_height_profile,cross_height_lines,invalid_floor_count


class FloorTests(unittest.TestCase):
    def test_many_lots_one_height_pair(self):
        master=[Row(external_id=str(i),floors_total=85) for i in range(281)]
        feed=[Row(external_id=str(i),floors_total=87) for i in range(281)]
        profile=height_profile(master,feed)
        self.assertEqual(profile['floors_total_groups'],[{'master':85,'feed':87,'lot_count':281}])
        lines=height_lines({'a':profile,'b':profile},{'a':'Циан','b':'Авито'})
        self.assertEqual(len(lines),1)
        self.assertIn('85 → XML 87',lines[0])
        self.assertNotIn('281',lines[0])

    def test_three_different_heights_not_one_fabricated_building(self):
        profile=height_profile([Row(external_id=str(i),floors_total=h) for i,h in enumerate([13,18,26])],
                               [Row(external_id=str(i),floors_total=44) for i in range(3)])
        self.assertEqual(len(profile['floors_total_groups']),3)
        self.assertEqual(profile['floors_total_grouping'],'height_pair_not_building_identity')

    def test_missing_values_not_zero_or_a_mismatch(self):
        p=height_profile([Row(external_id='1',floors_total=None)],[Row(external_id='1',floors_total=12)])
        self.assertEqual(p['floors_total_groups'],[])
        self.assertEqual(p['floors_total_coverage']['missing_in_master'],1)

    def test_real_apartment_floor_errors_preserved(self):
        self.assertEqual(non_height_counts({'field_mismatch_counts':{'floors_total':281,'floor':2,'area':1}}),{'floor':2,'area':1})

    def test_cross_feed_can_be_checked_without_a_master(self):
        groups=cross_height_profile({'a':[Row(external_id='1',floors_total=12)],'b':[Row(external_id='1',floors_total=8)]})
        self.assertEqual(groups,[{'values':{'a':12,'b':8},'lot_count':1}])
        self.assertIn('Циан 12, Яндекс 8',cross_height_lines(groups,{'a':'Циан','b':'Яндекс'})[0])

    def test_floor_above_building_detected_without_a_master(self):
        rows=[Row(floor=40,floors_total=28),Row(floor=28,floors_total=28),Row(floor=40,floors_total=None)]
        self.assertEqual(invalid_floor_count(rows),1)

    def test_missing_height_explicit_not_claimed_checked(self):
        lines=height_lines({'a':{'floors_total_coverage':{'missing_in_feed':58}}},{'a':'Домклик'})
        self.assertIn('сверка не выполнена',lines[0])

if __name__=='__main__':unittest.main()
