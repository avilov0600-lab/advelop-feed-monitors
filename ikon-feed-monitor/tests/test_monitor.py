import datetime as dt
import decimal
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

import config
import monitor


def make_lot(external_id="1", **changes):
    values = dict(
        external_id=external_id,
        source="test",
        project="new_zelenograd",
        category="residential",
        deal="sale",
        price=1000000,
        area=decimal.Decimal("25"),
        rooms=None,
        floor=1,
        floors_total=9,
    )
    values.update(changes)
    return monitor.Lot(**values)


class ParsingTests(unittest.TestCase):
    def test_master_filters_exact_categories_and_does_not_drop_reserved(self):
        xml = '''<realty-feed xmlns="x">
        <offer internal-id="flat"><type>Продажа</type><category>Квартира</category>
          <price><value>100,25</value><Status>Рекламная бронь</Status></price>
          <area><value>25.5</value></area><floor>2</floor><rooms>1</rooms></offer>
        <offer internal-id="commercial"><type>Продажа</type><category>Помещение</category></offer>
        </realty-feed>'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "master.xml"
            path.write_text(xml)
            lots, meta = monitor.parse_master_xml(
                path, project="new_zelenograd", category="residential", allowed_categories=("Квартира",)
            )
        self.assertEqual([lot.external_id for lot in lots], ["flat"])
        self.assertEqual(lots[0].price, 100)
        self.assertEqual(meta["all_category_counts"], {"Квартира": 1, "Помещение": 1})

    def test_feed_parsers_read_ids_and_deals(self):
        fixtures = {
            "cian": '<feed><object><ExternalId>001-sale</ExternalId><Category>freeAppointmentObjectSale</Category><TotalArea>64</TotalArea><BargainTerms><Price>29696000</Price></BargainTerms></object></feed>',
            "avito": '<Ads><Ad><Id>001-sale</Id><OperationType>Продам</OperationType><Square>64</Square><Price>29696000</Price></Ad></Ads>',
            "yandex": '<realty-feed><offer internal-id="001-sale"><type>продажа</type><area><value>64</value></area><price><value>30000000</value></price><discount><final-price>29696000</final-price></discount></offer></realty-feed>',
            "domclick": '<complexes><complex><buildings><building><flats><flat><flat_id>001-sale</flat_id><area>64</area><price>29696000</price></flat></flats></building></buildings></complex></complexes>',
        }
        with tempfile.TemporaryDirectory() as directory:
            for source, xml in fixtures.items():
                path = Path(directory) / f"{source}.xml"
                path.write_text(xml)
                lots, _ = monitor.FEED_PARSERS[source](path, project="skolkovo_one", category="residential")
                self.assertEqual(lots[0].external_id, "001-sale")
                self.assertEqual(lots[0].deal, "sale")
                self.assertEqual(lots[0].price, 29696000)


class ComparisonTests(unittest.TestCase):
    def test_master_reports_missing_and_extra_without_comparing_raw_price(self):
        expected = [make_lot("1"), make_lot("2")]
        actual = [make_lot("1", price=1005000), make_lot("3")]
        result = monitor.compare_master_to_feed(expected, actual)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["missing_from_feed"], ["2"])
        self.assertEqual(result["extra_in_feed"], ["3"])
        self.assertEqual(result["field_mismatch_counts"], {})

    def test_unavailable_is_not_zero_inventory(self):
        summary = monitor._unavailable_summary({"reason": "HTTP 401"})
        self.assertEqual(summary["status"], monitor.SOURCE_UNAVAILABLE)
        self.assertIsNone(summary["lot_count"])
        self.assertIsNone(summary["unique_id_count"])

    def test_missing_master_file_does_not_abort_other_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            lots, summary = monitor._master_for(Path(directory), {}, "residential", "new_zelenograd")
        self.assertIsNone(lots)
        self.assertEqual(summary["status"], monitor.SOURCE_UNAVAILABLE)

    def test_api_count_is_sum_of_both_projects(self):
        feeds = {"new_zelenograd": {"cian": [make_lot("1")]}, "skolkovo_one": {"cian": [make_lot("2")]}}
        result = monitor.analyze_api("residential", "cian", {"status": "ok", "published_count": 2}, feeds)
        self.assertEqual(result["comparison_status"], "ok")
        self.assertEqual(result["expected_feed_count"], 2)

    def test_residential_avito_excludes_disabled_project_from_expected_count(self):
        feeds = {
            "new_zelenograd": {"avito": [make_lot("1")]},
            "skolkovo_one": {"avito": [make_lot("2"), make_lot("3")]},
        }
        result = monitor.analyze_api("residential", "avito", {"status": "ok", "active_count": 1}, feeds)
        self.assertEqual(result["comparison_status"], "ok")
        self.assertEqual(result["expected_feed_count"], 1)

    def test_duplicate_feed_id_is_error(self):
        result = monitor.compare_master_to_feed([make_lot("1")], [make_lot("1"), make_lot("1")])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["duplicate_ids"], ["1"])

    def test_partial_feed_inventory_is_not_compared_with_api_as_zero(self):
        result = monitor.analyze_api("residential", "cian", {"status": "ok", "published_count": 2}, {})
        self.assertIsNone(result["expected_feed_count"])
        self.assertEqual(result["comparison_status"], "error")

    def test_card_exclusions_are_not_scheduled(self):
        self.assertNotIn("yandex", config.PLATFORM_CARDS["residential"]["new_zelenograd"])
        self.assertNotIn("avito", config.PLATFORM_CARDS["residential"]["skolkovo_one"])
        self.assertFalse(any("yandex3" in str(value) for value in config.FEEDS.values()))

    def test_expired_promo_is_flagged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.xml"
            path.write_text("<feed><discount><end-date>2026-08-31</end-date></discount></feed>")
            issues = monitor.detect_marketing_issues(path, today=dt.date(2026, 9, 3))
        self.assertTrue(any("2026-08-31" in issue for issue in issues))

    def test_july_discount_text_is_checked_like_masters(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'feed.xml'
            path.write_text('<feed><description>Скидка 10% в июле</description></feed>')
            issues = monitor.detect_marketing_issues(path, today=dt.date(2026, 9, 3))
            self.assertTrue(any('месяца №7' in issue for issue in issues))
            path.write_text('<feed><description>Скидка в июле 2027</description></feed>')
            self.assertEqual(monitor.detect_marketing_issues(path, today=dt.date(2026, 9, 3)), [])


class NetworkTests(unittest.TestCase):
    def test_download_retries_three_times_with_two_second_delay(self):
        error = urllib.error.URLError("temporary")
        with tempfile.TemporaryDirectory() as directory, mock.patch("urllib.request.urlopen", side_effect=error) as request, mock.patch("time.sleep") as sleep:
            with self.assertRaises(urllib.error.URLError):
                monitor._download("https://example.test/feed", Path(directory) / "feed.xml")
        self.assertEqual(request.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(2), mock.call(2)])

    def test_unauthorized_source_is_not_retried(self):
        error = urllib.error.HTTPError("https://example.test", 401, "Unauthorized", {}, None)
        with tempfile.TemporaryDirectory() as directory, mock.patch("urllib.request.urlopen", side_effect=error) as request:
            result = monitor._capture_download("https://example.test", Path(directory) / "feed.xml")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result["status"], monitor.SOURCE_UNAVAILABLE)
        self.assertEqual(result["attempts"], 1)


class TelegramTests(unittest.TestCase):
    def _report(self):
        def master(cache_dir, statuses, category, project):
            lots = [make_lot("1", category=category, project=project)]
            return lots, {**monitor.summarize_lots(lots), "status": "OK"}

        def parser(path, *, project, category):
            return [make_lot("1", category=category, project=project)], {}

        def snapshot(cache_dir, category, source):
            count = 1 if category == "residential" and source == "avito" else 2
            return {"status": "ok", "published_count": count, "active_count": count}

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(monitor, "_master_for", side_effect=master), mock.patch.dict(monitor.FEED_PARSERS, {key: parser for key in monitor.FEED_PARSERS}), mock.patch.object(monitor, "_load_api_snapshot", side_effect=snapshot), mock.patch.object(monitor, "analyze_cards", return_value={}):
            return monitor.analyze_directory(Path(directory))

    def test_green_report_contains_only_residential_category(self):
        report = self._report()
        self.assertEqual(report["overall_status"], "ok")
        message = monitor.format_telegram(report)
        self.assertTrue(message.startswith("🟢"))
        self.assertIn("ЖИЛАЯ НЕДВИЖИМОСТЬ", message)
        self.assertNotIn("КОММЕРЦИЯ", message)

    def test_report_has_bold_headings_and_separated_projects(self):
        message = monitor.format_telegram(self._report())
        self.assertIn("<b>ЖИЛАЯ НЕДВИЖИМОСТЬ</b>\n\n<b>ЖК «Новый Зеленоград»</b>", message)
        self.assertIn("\n\n<b>ЖК Skolkovo ONE</b>\n<i>Основной источник</i>", message)
        self.assertNotIn("<b>КОММЕРЦИЯ</b>", message)
        self.assertEqual(message.count("Даты и месяцы акций: замечаний нет"), 2)

    def test_api_totals_show_cabinet_feed_counts_and_project_scope(self):
        report = self._report()
        message = monitor.format_telegram(report)
        self.assertIn("<i>Площадки — факт размещения по API</i>", message)
        self.assertIn("✅ Циан API (ЖК «Новый Зеленоград», ЖК Skolkovo ONE): всего в кабинете 2, наши XML 2; разница +0", message)
        self.assertIn("✅ Авито API (ЖК «Новый Зеленоград»): всего в кабинете 1, наши XML 1; разница +0", message)
        self.assertNotIn("аккаунт подтверждён", message)
        compact = monitor._format_compact_telegram(report)
        self.assertIn("Циан · оба ЖК: в кабинете — 2, в фидах — 2", compact)
        self.assertIn("Авито · ЖК «Новый Зеленоград»: в кабинете — 1, в фидах — 1", compact)

    def test_category_api_result_is_not_presented_as_project_card_verification(self):
        report = self._report()
        category = report["categories"]["residential"]
        category["platform_cards"] = {"new_zelenograd": {
            "cian": {"status": "covered_by_api", "feed_count": 1, "offer_count": None},
            "avito": {"status": "covered_by_api", "feed_count": 1, "offer_count": None},
        }}
        self.assertEqual(monitor._platform_lines(category, "new_zelenograd"), [])
        message = monitor.format_telegram(report)
        self.assertNotIn("аккаунт подтверждён", message)
        self.assertNotIn("карточка не показывает", message)
        self.assertNotIn("<i>Площадки проекта</i>", message)
        self.assertIn("Циан API (ЖК «Новый Зеленоград», ЖК Skolkovo ONE): всего в кабинете 2, наши XML 2", message)

    def test_actual_project_card_difference_remains_visible(self):
        category = self._report()["categories"]["residential"]
        category["platform_cards"] = {"skolkovo_one": {"yandex": {
            "status": "error", "check_method": "public_page", "offer_count": 40, "feed_count": 41,
        }}}
        lines = monitor._platform_lines(category, "skolkovo_one")
        self.assertIn("🔴 Яндекс Недвижимость: 40 на карточке / 41 в XML — требуется проверить", lines)

    def test_api_mismatch_keeps_both_counts_and_shows_difference(self):
        category = self._report()["categories"]["residential"]
        category["api_checks"]["cian"].update(comparison_status="error", actual_platform_count=1,
            reason="число опубликованных объявлений отличается от XML")
        line = monitor._api_lines(category)[0]
        self.assertTrue(line.startswith("🔴"))
        self.assertIn("в кабинете — 1, в фидах — 2. Расхождение: 1", line)

    def test_import_errors_stay_red_even_when_counts_match(self):
        category = self._report()["categories"]["residential"]
        category["api_checks"]["cian"].update(comparison_status="error", reason="ошибок импорта Циан: 3")
        line = monitor._api_lines(category)[0]
        self.assertTrue(line.startswith("🔴"))
        self.assertIn("в кабинете — 2, в фидах — 2", line)
        self.assertIn("Количество совпадает, но проверка API не пройдена", line)
        self.assertIn("ошибок импорта Циан: 3", line)

    def test_unavailable_api_is_not_a_zero_or_a_successful_comparison(self):
        category = self._report()["categories"]["residential"]
        api = category["api_checks"]["cian"]
        api.update(comparison_status="error", actual_platform_count=None, reason="HTTP 403")
        line = monitor._api_lines(category)[0]
        self.assertIn("в кабинете — нет данных, в фидах — 2. Сравнение не выполнено", line)
        self.assertNotIn("Количество совпадает", line)
        api.update(actual_platform_count=0, expected_feed_count=None)
        self.assertIn("в кабинете — 0, в фидах — нет полных данных", monitor._api_lines(category)[0])

    def test_unverifiable_covered_card_is_not_hidden(self):
        category = self._report()["categories"]["residential"]
        category["api_checks"] = {}
        category["platform_cards"] = {"new_zelenograd": {"cian": {
            "status": "covered_by_api", "feed_count": 1,
        }}}
        self.assertIn("⚪ Циан: в фиде ЖК — 1; число на карточке ЖК не проверено",
            monitor._platform_lines(category, "new_zelenograd"))

    def test_report_escapes_source_text_instead_of_interpreting_html(self):
        report = self._report()
        project = report["categories"]["residential"]["projects"]["new_zelenograd"]
        project["master_vs_feeds"]["cian"]["extra_in_feed"] = ['<b>fake</b>&ID']
        project["feeds"]["cian"]["marketing_issues"] = ['<script>bad & text</script>']
        message = monitor.format_telegram(report)
        # Public summaries now omit individual lot IDs; never render a raw ID as HTML.
        self.assertNotIn('<b>fake</b>&ID', message)
        self.assertIn('&lt;script&gt;bad &amp; text&lt;/script&gt;', message)
        self.assertNotIn('<script>', message)

    def test_marketing_errors_survive_unavailable_master(self):
        report = self._report()
        project = report["categories"]["residential"]["projects"]["new_zelenograd"]
        project["master"]["status"] = monitor.SOURCE_UNAVAILABLE
        project["master_vs_feeds"]["cian"]["status"] = monitor.SOURCE_UNAVAILABLE
        project["feeds"]["cian"]["marketing_issues"] = ['истёк срок акции 2026-07-31']
        self.assertIn('текст акции: истёк срок акции 2026-07-31', monitor.format_telegram(report))

    def test_unavailable_text_is_not_reported_as_checked(self):
        project = self._report()["categories"]["residential"]["projects"]["new_zelenograd"]
        project["feeds"]["cian"] = {"status": monitor.SOURCE_UNAVAILABLE}
        lines = monitor._marketing_lines(project)
        self.assertTrue(any('не проверены — Циан' in line for line in lines))
        self.assertFalse(any('замечаний нет' in line for line in lines))

    def test_checked_time_is_readable_and_moscow_local(self):
        self.assertEqual(monitor._checked_label('2026-09-03T10:01:48Z'), '03.09.2026, 13:01:48 МСК')

    @mock.patch.dict('os.environ', {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "42"})
    def test_send_and_sync_preserve_html_format(self):
        with mock.patch.object(monitor, '_telegram_urlopen') as call, \
                mock.patch('urllib.request.urlopen') as state:
            for request in (call, state):
                request.return_value.__enter__.return_value.read.return_value = b'{"ok":true}'
            monitor.send_telegram('<b>Heading</b>')
            data = urllib.parse.parse_qs(call.call_args.args[0].data.decode())
            self.assertEqual(data['parse_mode'], ['HTML'])
            with mock.patch.dict('os.environ', {"TELEGRAM_LATEST_MESSAGE_URL": "https://example.test"}):
                monitor.sync_latest_message('<b>Heading</b>', '2026-09-03T10:00:00Z')
            self.assertEqual(json.loads(state.call_args.args[0].data)['parse_mode'], 'HTML')

    def test_compact_report_preserves_residential_section(self):
        report = self._report()
        report["overall_status"] = "error"
        for category in report["categories"].values():
            category["status"] = "error"
            for project in category["projects"].values():
                for value in project["master_vs_feeds"].values():
                    value["missing_from_feed"] = ["very-long-id-" + ("x" * 500)] * 50
        message = monitor.format_telegram(report)
        self.assertLessEqual(len(message), monitor.MAX_TELEGRAM_MESSAGE)
        self.assertIn("ЖИЛАЯ НЕДВИЖИМОСТЬ", message)
        self.assertNotIn("КОММЕРЦИЯ", message)
        self.assertIn("Проверено:", message)
        self.assertIn("\n\n<b>ЖК Skolkovo ONE</b>", message)
        # Escaped dynamic values and our balanced tags must remain parseable.
        monitor.ET.fromstring('<message>' + message + '</message>')

    def test_start_replays_saved_message_without_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            monitor.save_latest_telegram_message(path, "последний отчёт", "2026-09-03")
            result = monitor.telegram_start_payload({"message": {"text": "/start", "chat": {"id": 42, "type": "private"}}}, path)
        self.assertEqual(result, {"chat_id": "42", "text": "последний отчёт"})

    def test_group_start_is_ignored(self):
        result = monitor.telegram_start_payload({"message": {"text": "/start", "chat": {"id": 42, "type": "group"}}}, Path("missing"))
        self.assertIsNone(result)

    def test_start_replays_saved_html_mode_without_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.json'
            monitor.save_latest_telegram_message(path, '<b>ЖИЛАЯ</b>', '2026-09-03', parse_mode='HTML')
            result = monitor.telegram_start_payload({"message": {"text": "/start", "chat": {"id": 42, "type": "private"}}}, path)
        self.assertEqual(result['parse_mode'], 'HTML')
        self.assertEqual(result['text'], '<b>ЖИЛАЯ</b>')

    def test_manual_alert_requires_repeated_problem(self):
        report = {"overall_status": "error", "categories": {}, "checked_at": "now"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            self.assertFalse(monitor.should_notify(report, path, 2))
            self.assertTrue(monitor.should_notify(report, path, 2))
            self.assertFalse(monitor.should_notify(report, path, 2))


if __name__ == "__main__":
    unittest.main()
