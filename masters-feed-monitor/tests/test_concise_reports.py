import copy
from pathlib import Path
import unittest

from concise_reports import render_report, render_masters, is_morning, website_header, publication_lines, emergency_status

LABELS = {'cian': 'Циан', 'avito': 'Авито', 'yandex': 'Яндекс', 'domclick': 'Домклик'}


def fixture():
    project = {'master': {'status': 'OK', 'lot_count': 50},
               'website': {'lot_count': 50, 'id_comparison': {'missing_from_website': 0, 'extra_on_website': 0}},
               'feeds': {s: {'lot_count': 50, 'status': 'OK', 'marketing_issues': []} for s in ('cian', 'avito')},
               'master_vs_feeds': {s: {'status': 'ok', 'missing_from_feed': [], 'extra_in_feed': [],
                                        'field_mismatch_counts': {}} for s in ('cian', 'avito')}, 'cross_feed': {}}
    return {'overall_status': 'ok', 'checked_at': '2026-10-30T09:02:00+03:00',
            'site_availability': {'p': {'status': 'available', 'status_code': 200}},
            'categories': {'residential': {'projects': {'p': project}, 'platform_cards': {},
                'api_checks': {'cian': {'comparison_status': 'ok', 'actual_platform_count': 50,
                    'expected_feed_count': 50, 'active_projects': ['p'],
                    'daily_statistics': {'status': 'ok', 'date': '2026-10-29', 'shows': 800, 'views': 40}},
                    'avito': {'status': 'ok', 'comparison_status': 'ok', 'actual_platform_count': 50,
                              'expected_feed_count': 50, 'active_projects': ['p']}}}}}


class ConciseReportTests(unittest.TestCase):
    def render(self, report):
        return render_report(report, 'Клиент', {'p': 'Проект', 'q': 'Другой ЖК'}, LABELS)

    def test_header_is_first_and_inventory_not_repeated_for_each_feed(self):
        text = self.render(fixture())
        self.assertTrue(text.startswith('<b>🟢 Клиент — экстренных ошибок нет</b>\n'))
        self.assertIn('✅ Сайт доступен\n', text)
        self.assertIn('Источник, сайт и наши 2 фида: по 50 квартир.', text)
        self.assertEqual(text.count('Состав и параметры фидов совпадают'), 1)
        self.assertNotIn('HTTP 200', text)
        self.assertNotIn('ошибок полей 0', text)

    def test_site_failure_precedes_good_sites_with_short_reason(self):
        report = fixture()
        report['site_availability']['q'] = {'status': 'unavailable', 'reason': 'Таймаут запроса — 8 с'}
        text = self.render(report)
        self.assertTrue(text.startswith('<b>🔴 Клиент — требует внимания</b>'))
        self.assertIn('❌ Сайт недоступен (Другой ЖК) — превышено время ожидания', text)
        self.assertIn('✅ Сайт доступен: Проект', text)

    def test_unknown_status_never_claims_available_or_unavailable(self):
        report = fixture(); report['site_availability']['p'] = {'status': 'not_checked'}
        text = self.render(report)
        self.assertTrue(text.startswith('<b>🔴 Клиент — проверка не завершена</b>'))
        self.assertIn('⚪ Доступность сайта не проверена', text)
        self.assertNotIn('✅ Сайт доступен', text)
        self.assertNotIn('❌ Сайт недоступен', text)

    def test_legacy_yandex_card_key_is_shown_as_a_human_name(self):
        report = fixture()
        report['categories']['residential']['platform_cards']['p'] = {
            'yandex_realty': {'status': 'ok', 'offer_count': 50, 'feed_count': 50}}
        text = self.render(report)
        self.assertIn('Яндекс Недвижимость: опубликовано 50', text)
        self.assertNotIn('yandex_realty', text)

    def test_morning_only_uses_aware_moscow_time_not_host_clock(self):
        for stamp, expected in [('2026-10-30T06:01:00Z', True), ('2026-10-30T08:59:00+03:00', False),
                                ('2026-10-30T11:59:00+03:00', True), ('2026-10-30T13:00:00+03:00', False),
                                ('2026-10-30T15:30:00+03:00', False), ('2026-10-30T18:30:00+03:00', False),
                                ('invalid', False), ('2026-10-30T09:00:00', False)]:
            report = fixture(); report['checked_at'] = stamp
            self.assertEqual(is_morning(report), expected)
            self.assertEqual('800 показов, 40 просмотров' in self.render(report), expected)

    def test_equal_counts_do_not_hide_swapped_ids(self):
        report = fixture(); project = report['categories']['residential']['projects']['p']
        for comparison in project['master_vs_feeds'].values():
            comparison.update(status='error', missing_from_feed=['A'], extra_in_feed=['B'])
        text = self.render(report)
        self.assertEqual(text.count('не хватает квартир: 1; лишних квартир: 1'), 1)
        self.assertNotIn('Состав и параметры фидов совпадают', text)

    def test_field_and_cross_feed_results_share_a_line_without_losing_counts(self):
        report = fixture(); project = report['categories']['residential']['projects']['p']
        project['master_vs_feeds']['avito']['field_mismatch_counts'] = {'area': 12}
        project['cross_feed']['field_mismatch_counts'] = {'area': 17, 'price': 3}
        text = self.render(report)
        self.assertIn('Площадь: с источником — 12 (Авито); между фидами — 17 расхождений.', text)
        self.assertIn('Цена: между фидами — 3', text)

    def test_unknown_inventory_is_not_zero_and_no_green_api_when_check_failed(self):
        report = fixture(); project = report['categories']['residential']['projects']['p']
        project['website'] = {'status': 'SOURCE_UNAVAILABLE', 'lot_count': None}
        api = report['categories']['residential']['api_checks']['cian']; api['actual_platform_count'] = None
        text = self.render(report)
        self.assertIn('Количество квартир на сайте не определено', text)
        self.assertNotIn('сайт — 0', text)
        self.assertIn('API не проверено', text)
        self.assertNotIn('✅ Циан', text)

    def test_invalid_apartment_floor_and_expired_promotion_stay_visible(self):
        report = fixture(); feed = report['categories']['residential']['projects']['p']['feeds']['avito']
        feed.update(invalid_floor_above_building=2, marketing_issues=['истёк срок акции 2026-09-30'])
        text = self.render(report)
        self.assertIn('этаж квартиры выше этажности здания — 2', text)
        self.assertIn('истёк срок акции 2026-09-30', text)
        self.assertNotIn('Состав и параметры фидов совпадают', text)

    def test_shared_cabinet_stays_explicitly_multi_project(self):
        report = fixture(); category = report['categories']['residential']
        category['projects']['q'] = copy.deepcopy(category['projects']['p'])
        api = category['api_checks']['cian']; api.update(active_projects=['p', 'q'], actual_platform_count=100, expected_feed_count=100)
        text = self.render(report)
        self.assertIn('Циан (Проект, Другой ЖК): опубликовано 100', text)
        self.assertNotIn('Циан (Проект): опубликовано 100', text)
        self.assertEqual(text.count('<b>Другой ЖК</b>'), 1)

    def test_proxy_xml_count_never_becomes_confirmed_publication(self):
        cards = {'domclick': {'status': 'ok', 'offer_count': 50, 'feed_count': 50, 'check_method': 'feed_assumption'}}
        text = '\n'.join(publication_lines(cards, LABELS))
        self.assertIn('размещение не подтверждено', text)
        self.assertNotIn('опубликовано', text)
        self.assertNotIn('✅', text)

    def test_no_mutation_and_html_is_escaped(self):
        report = fixture(); before = copy.deepcopy(report)
        text = render_report(report, '<script>Клиент</script>', {'p': 'Проект & ЖК'}, LABELS)
        self.assertEqual(report, before)
        self.assertIn('&lt;script&gt;', text)
        self.assertIn('Проект &amp; ЖК', text)

    def test_threshold_uses_maternal_count_not_feed_count_and_is_strict(self):
        for actual, expected_status in [(94, 'error'), (95, 'ok'), (100, 'ok'), (104, 'ok'), (0, 'error')]:
            report = fixture(); category = report['categories']['residential']
            category['projects']['p']['master']['lot_count'] = 100
            category['api_checks']['cian']['actual_platform_count'] = actual
            category['api_checks']['avito']['actual_platform_count'] = 100
            result = emergency_status(report, {'p': 'Проект'}, LABELS)
            self.assertEqual(result['status'], expected_status)
            if actual < 95:
                self.assertIn(f'опубликовано {actual} из 100', '\n'.join(result['problems']))

    def test_near_threshold_is_not_rounded_to_green(self):
        report = fixture(); category = report['categories']['residential']
        category['projects']['p']['master']['lot_count'] = 10001
        for api in category['api_checks'].values(): api['actual_platform_count'] = 9500
        result = emergency_status(report, {'p':'Проект'}, LABELS)
        self.assertEqual(result['status'], 'error')
        self.assertIn('(94.9%)', result['problems'][0])

    def test_expired_promotions_and_field_errors_do_not_drive_header(self):
        report = fixture(); report['overall_status'] = 'error'
        project = report['categories']['residential']['projects']['p']
        project['feeds']['avito']['marketing_issues'] = ['истёк срок акции']
        project['master_vs_feeds']['cian']['field_mismatch_counts'] = {'price': 2}
        text = self.render(report)
        self.assertTrue(text.startswith('<b>🟢'))
        self.assertIn('истёк срок акции', text)
        self.assertIn('Цена: с источником — 2', text)

    def test_failed_feed_and_maternal_source_make_red_even_with_published_counts(self):
        for source in ('master', 'agency'):
            report = fixture(); project = report['categories']['residential']['projects']['p']
            value = project['master'] if source == 'master' else project['feeds']['avito']
            value.update(status='SOURCE_UNAVAILABLE', lot_count=None)
            result = emergency_status(report, {'p':'Проект'}, LABELS)
            self.assertEqual(result['status'], 'error')
            self.assertTrue(result['problems'])
            self.assertNotIn('опубликовано 0', self.render(report))

    def test_unknown_platform_count_does_not_mean_zero_or_green(self):
        report = fixture(); report['categories']['residential']['api_checks']['cian']['actual_platform_count'] = None
        result = emergency_status(report, {'p':'Проект'}, LABELS)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['problems'], [])
        self.assertTrue(result['unknown'])

    def test_card_coverage_and_proxy_evidence_are_distinct(self):
        report = fixture(); category = report['categories']['residential']
        category['platform_cards']['p'] = {'domclick': {
            'status':'ok', 'offer_count':40, 'feed_count':40, 'check_method':'public_page'}}
        result = emergency_status(report, {'p':'Проект'}, LABELS)
        self.assertIn('опубликовано 40 из 50 (80%)', result['problems'][0])
        category['platform_cards']['p']['domclick']['check_method'] = 'feed_proxy'
        result = emergency_status(report, {'p':'Проект'}, LABELS)
        self.assertFalse(result['problems'])
        self.assertTrue(result['unknown'])

    def test_shared_account_uses_matching_maternal_scope(self):
        report = fixture(); category = report['categories']['residential']
        category['projects']['q'] = copy.deepcopy(category['projects']['p'])
        category['projects']['q']['master']['lot_count'] = 150
        report['site_availability']['q'] = {'status':'available'}
        for api in category['api_checks'].values():
            api.update(active_projects=['p','q'], actual_platform_count=189, expected_feed_count=100)
        result = emergency_status(report, {'p':'Проект','q':'Второй'}, LABELS)
        self.assertIn('суммарно: Проект, Второй', result['problems'][0])
        self.assertIn('189 из 200 (94.5%)', result['problems'][0])

    def test_zero_baseline_and_no_website_do_not_invent_failures(self):
        report = fixture(); category = report['categories']['residential']
        category['projects']['p']['master']['lot_count'] = 0
        category['projects']['p']['website'] = {'status':'not_applicable'}
        report['site_availability'] = {}
        for api in category['api_checks'].values(): api['actual_platform_count'] = 0
        self.assertEqual(emergency_status(report, {'p':'Проект'}, LABELS)['status'], 'ok')

    def test_failed_api_with_stale_count_is_unknown(self):
        report = fixture(); report['categories']['residential']['api_checks']['cian']['status'] = 'unavailable'
        result = emergency_status(report, {'p':'Проект'}, LABELS)
        self.assertEqual(result['status'], 'error')
        self.assertFalse(result['problems'])
        self.assertTrue(result['unknown'])



if __name__ == '__main__':
    unittest.main()
