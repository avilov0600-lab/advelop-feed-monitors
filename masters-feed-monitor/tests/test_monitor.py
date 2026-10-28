import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor import (
    OFFER_FEEDS,
    SOURCE_UNAVAILABLE,
    Lot,
    _download,
    _apply_domclick_feed_control,
    _apply_marketplace_api_snapshot,
    compare_master_to_feeds,
    compare_site_to_master,
    download_sources,
    format_telegram,
    parse_cian,
    parse_master,
    parse_platform_page,
    parse_yrl,
    save_latest_telegram_message,
    send_telegram,
    broadcast_telegram_message,
    should_notify,
    telegram_start_reply,
)


class ParserTests(unittest.TestCase):
    def test_invalid_secondary_site_does_not_stop_feed_report(self):
        import monitor as module
        row=Lot('master','1',100,None,None,1,2,12)
        with tempfile.TemporaryDirectory() as directory:
            cache=Path(directory)
            with (mock.patch.object(module,'_read_source_statuses',return_value={'site':{'status':'OK'}}),
                  mock.patch.object(module,'parse_master',return_value=([row],{})),
                  mock.patch.object(module,'parse_feed',return_value=([row],{})),
                  mock.patch.object(module,'parse_site',side_effect=json.JSONDecodeError('not JSON','<html>',0)),
                  mock.patch.object(module,'analyze_platform_pages',return_value={}),
                  mock.patch.object(module,'_read_marketplace_api_snapshot',return_value=None)):
                report=module.analyze_directory(cache)
        self.assertEqual(report['site_vs_master']['status'],SOURCE_UNAVAILABLE)
        self.assertIsNone(report['site']['lot_count'])
        self.assertEqual(report['feeds']['cian']['lot_count'],1)
        self.assertIn('сайт сейчас не проверен',format_telegram(report))

    def test_site_shape_and_pagination_are_validated(self):
        import monitor as module
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'site.json'
            for payload in [[],{}, {'results':[{'id':1}],'count':2}]:
                path.write_text(json.dumps(payload))
                with self.assertRaises(ValueError):module.parse_site(path)

    def test_height_warning_keeps_red_status_and_not_hundreds_of_field_errors(self):
        import monitor as module
        row=Lot('master','1',100,None,None,1,2,85)
        feed=Lot('cian','1',100,None,None,1,2,87)
        with tempfile.TemporaryDirectory() as directory:
            with (mock.patch.object(module,'_read_source_statuses',return_value={'site':{'status':SOURCE_UNAVAILABLE}}),
                  mock.patch.object(module,'parse_master',return_value=([row],{})),
                  mock.patch.object(module,'parse_feed',return_value=([feed],{})),
                  mock.patch.object(module,'analyze_platform_pages',return_value={}),
                  mock.patch.object(module,'_read_marketplace_api_snapshot',return_value=None)):
                report=module.analyze_directory(Path(directory))
        message=format_telegram(report)
        self.assertTrue(message.startswith('🔴'))
        self.assertEqual(message.count('Этажность здания:'),1)
        self.assertIn('85 → XML 87',message)
        self.assertNotIn('ошибки полей: {"floors_total"',message)
    def test_download_retries_network_errors_three_times(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"feed-data"
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.geturl.return_value = "https://feed.example/data"
        response.__enter__.return_value.headers.get.return_value = "application/xml"
        failures = [
            urllib.error.URLError("temporary-1"),
            urllib.error.URLError("temporary-2"),
            response,
        ]
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "feed.xml"
            with (
                mock.patch("monitor.urllib.request.urlopen", side_effect=failures) as open_url,
                mock.patch("monitor.time.sleep") as sleep,
            ):
                result = _download("https://feed.example/data", destination)

            self.assertEqual(destination.read_bytes(), b"feed-data")
        self.assertEqual(open_url.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(2), mock.call(2)])
        self.assertEqual(result["attempts"], 3)

    def test_download_sources_marks_one_feed_unavailable_and_continues(self):
        def fake_download(url, destination, **kwargs):
            if destination.name == "masters-avito.xml":
                raise urllib.error.URLError("temporary")
            destination.write_text("ok", encoding="utf-8")
            return {"status": "OK", "downloaded": True, "attempts": 1}

        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            with (
                mock.patch("monitor._download", side_effect=fake_download),
                mock.patch("monitor.download_marketplace_api_snapshots"),
                mock.patch("monitor.download_platform_pages"),
            ):
                statuses = download_sources(cache_dir)

            saved = json.loads(
                (cache_dir / "source-downloads.json").read_text(encoding="utf-8")
            )
        self.assertEqual(statuses["avito"]["status"], SOURCE_UNAVAILABLE)
        self.assertEqual(saved["avito"]["status"], SOURCE_UNAVAILABLE)
        self.assertTrue(
            all(
                statuses[source]["downloaded"]
                for source in OFFER_FEEDS
                if source != "avito"
            )
        )

    def test_partial_feed_set_is_not_reported_as_zero_lots(self):
        master = [Lot("master", "lot-1", 100, None, None, 1, 2, None)]
        feeds = {
            source: [Lot(source, "lot-1", 100, None, None, 1, 2, None)]
            for source in OFFER_FEEDS
            if source != "avito"
        }
        result = compare_master_to_feeds(master, feeds)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["total_id_difference_count"], 0)
        self.assertEqual(result["sources"]["avito"]["status"], SOURCE_UNAVAILABLE)
        self.assertIsNone(result["sources"]["avito"]["feed_count"])

    def test_telegram_names_unavailable_feed_instead_of_showing_zero(self):
        master = [Lot("master", "lot-1", 100, None, None, 1, 2, None)]
        feed_lots = {
            source: [Lot(source, "lot-1", 100, None, None, 1, 2, None)]
            for source in OFFER_FEEDS
            if source != "avito"
        }
        feeds = {
            source: {
                "status": SOURCE_UNAVAILABLE if source == "avito" else "OK",
                "lot_count": None if source == "avito" else 1,
                "attempts": 3 if source == "avito" else 1,
                "reason": "сетевая ошибка" if source == "avito" else None,
                "stale_content": {},
            }
            for source in OFFER_FEEDS
        }
        report = {
            "checked_at": "2026-09-01T13:00:00+03:00",
            "master": {"total_property_count": 1},
            "feeds": feeds,
            "master_vs_feeds": compare_master_to_feeds(master, feed_lots),
            "cross_feed": {"field_mismatch_counts": {}},
            "site_vs_master": {
                "status": "OK",
                "site_count": 1,
                "site_only": [],
                "master_only": [],
            },
            "platform_cards": {},
        }
        message = format_telegram(report)
        self.assertIn("SOURCE_UNAVAILABLE — Авито", message)
        self.assertIn("доступны 4 из 5", message)
        self.assertNotIn("Авито 0", message)

    def test_master_feed_filters_export_status_and_normalizes_known_storeys(self):
        xml = """<?xml version="1.0"?>
        <mcdsoft_feed><mcdsoft_apartment_complex><mcdsoft_house>
          <mcdsoft_section>
            <mcdsoft_number>1</mcdsoft_number><mcdsoft_high_floor>15</mcdsoft_high_floor>
            <mcdsoft_property>
              <mcdsoft_propertyid>lot-active</mcdsoft_propertyid>
              <mcdsoft_status_chessmatches>686340000</mcdsoft_status_chessmatches>
              <mcdsoft_price>61 051 540,00</mcdsoft_price>
              <mcdsoft_whole_project_area>77,30</mcdsoft_whole_project_area>
              <mcdsoft_number_rooms>1</mcdsoft_number_rooms>
              <mcdsoft_isstudio>true</mcdsoft_isstudio>
              <mcdsoft_floor_height>2</mcdsoft_floor_height>
              <mcdsoft_section_number>1</mcdsoft_section_number>
              <mcdsoft_relevant_number>31</mcdsoft_relevant_number>
            </mcdsoft_property>
            <mcdsoft_property>
              <mcdsoft_propertyid>lot-excluded</mcdsoft_propertyid>
              <mcdsoft_status_chessmatches>686340001</mcdsoft_status_chessmatches>
            </mcdsoft_property>
          </mcdsoft_section>
        </mcdsoft_house></mcdsoft_apartment_complex></mcdsoft_feed>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "master.xml"
            path.write_text(xml, encoding="utf-8")
            lots, meta = parse_master(path)
        self.assertEqual(len(lots), 1)
        self.assertEqual(lots[0].external_id, "lot-active")
        self.assertEqual(lots[0].rooms, 0)
        self.assertEqual(lots[0].floors_total, 14)
        self.assertEqual(lots[0].apartment, "31")
        self.assertEqual(meta["total_property_count"], 2)
        self.assertEqual(meta["excluded_property_count"], 1)

    def test_master_comparison_does_not_compare_domclick_building_floor_to_sections(self):
        master = [
            Lot("master", "lot-1", 100, None, None, 1, 2, None, apartment="31")
        ]
        feeds = {
            source: [
                Lot(source, "lot-1", 90, None, None, 1, 2, 14, apartment="31")
            ]
            for source in OFFER_FEEDS
        }
        feeds["yandex"][0] = Lot(
            "yandex", "lot-1", 90, 100, None, 1, 2, 14,
            section=None, apartment="31"
        )
        result = compare_master_to_feeds(master, feeds)
        self.assertEqual(result["total_id_difference_count"], 0)
        self.assertEqual(result["field_mismatch_counts"], {})

    def test_site_floor_data_is_informational_and_never_an_error(self):
        site = [
            Lot(
                "site", "site:1", 100, None, None, 1, 2, 99,
                section="9", site_id=1
            )
        ]
        master = [
            Lot("master", "lot-1", 100, None, None, 1, 2, 14, section="1")
        ]
        result = compare_site_to_master(site, master)
        self.assertEqual(result["matched_field_mismatch_counts"], {})
        self.assertEqual(result["matched_field_mismatch_samples"], [])

    def test_telegram_uses_relay_when_configured(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = (
            b'{"ok": true, "result": {"message_id": 17}}'
        )
        environment = {
            "TELEGRAM_BOT_TOKEN": "123456:test-token-value",
            "TELEGRAM_CHAT_ID": "42",
            "TELEGRAM_RELAY_URL": "https://relay.example/telegram-send",
            "TELEGRAM_RELAY_KEY": "relay-secret",
            "TELEGRAM_RELAY_AUTH_TOKEN": "sites-secret",
        }
        with (
            mock.patch.dict("os.environ", environment, clear=False),
            mock.patch("monitor.urllib.request.urlopen", return_value=response) as open_url,
        ):
            result = send_telegram("контрольное сообщение")

        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, environment["TELEGRAM_RELAY_URL"])
        self.assertEqual(request.get_header("X-masters-relay-key"), "relay-secret")
        self.assertEqual(
            request.get_header("Oai-sites-authorization"),
            "Bearer sites-secret",
        )
        self.assertIn("контрольное сообщение".encode("utf-8"), request.data)
        self.assertEqual(result["message_id"], 17)

    @mock.patch("monitor.send_telegram")
    def test_private_start_receives_the_exact_cached_digest(self, send):
        update = {
            "update_id": 17,
            "message": {
                "text": "/start",
                "chat": {"id": 42, "type": "private"},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "telegram.json"
            save_latest_telegram_message(state, "последний дайджест", "checked")
            served = telegram_start_reply(update, state)

        self.assertTrue(served)
        send.assert_called_once_with("последний дайджест", chat_id="42")

    @mock.patch("monitor.sync_latest_telegram_message")
    @mock.patch("monitor.send_telegram")
    @mock.patch.dict("os.environ", {"TELEGRAM_LATEST_MESSAGE_URL": "https://state.example"})
    def test_broadcast_delivers_once_to_every_unique_subscriber(self, send, sync):
        sync.return_value = {"ok": True, "recipients": ["42", "43", "42"]}
        with mock.patch("monitor.time.sleep") as sleep:
            result = broadcast_telegram_message("дайджест", "checked")
        self.assertEqual(
            send.call_args_list,
            [mock.call("дайджест", chat_id="42"), mock.call("дайджест", chat_id="43")],
        )
        sleep.assert_called_once_with(0.05)
        self.assertEqual(result, {"subscribers": 2, "delivered": 2, "failed": 0})

    @mock.patch("monitor.sync_latest_telegram_message")
    @mock.patch("monitor.send_telegram")
    @mock.patch.dict("os.environ", {"TELEGRAM_LATEST_MESSAGE_URL": "https://state.example"})
    def test_broadcast_attempts_other_subscribers_and_then_reports_failure(self, send, sync):
        sync.return_value = {"ok": True, "recipients": ["42", "43"]}
        send.side_effect = [urllib.error.URLError("blocked"), {}]
        with mock.patch("monitor.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "1 subscriber"):
                broadcast_telegram_message("дайджест", "checked")
        self.assertEqual(send.call_count, 2)

    @mock.patch("monitor.sync_latest_telegram_message", return_value={"ok": True})
    @mock.patch("monitor.send_telegram")
    @mock.patch.dict(
        "os.environ",
        {
            "TELEGRAM_LATEST_MESSAGE_URL": "https://state.example",
            "TELEGRAM_CHAT_ID": "42",
        },
    )
    def test_broadcast_falls_back_to_owner_when_registry_is_invalid(self, send, _sync):
        result = broadcast_telegram_message("дайджест", "checked")
        send.assert_called_once_with("дайджест", chat_id="42")
        self.assertEqual(result, {"subscribers": 1, "delivered": 1, "failed": 0})

    @mock.patch("monitor.sync_latest_telegram_message", side_effect=TimeoutError)
    @mock.patch("monitor.send_telegram")
    @mock.patch.dict(
        "os.environ",
        {
            "TELEGRAM_LATEST_MESSAGE_URL": "https://state.example",
            "TELEGRAM_CHAT_ID": "42",
        },
    )
    def test_broadcast_falls_back_to_owner_when_registry_times_out(self, send, _sync):
        result = broadcast_telegram_message("дайджест", "checked")
        send.assert_called_once_with("дайджест", chat_id="42")
        self.assertEqual(result["delivered"], 1)

    def test_domclick_is_ok_when_its_feed_matches_all_other_feeds(self):
        summaries = {source: {"lot_count": 165} for source in OFFER_FEEDS}
        result = _apply_domclick_feed_control(
            {"label": "Домклик"}, summaries
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["offer_count"], 165)

    def test_domclick_requires_check_when_its_feed_count_differs(self):
        summaries = {source: {"lot_count": 165} for source in OFFER_FEEDS}
        summaries["domclick"]["lot_count"] = 160
        result = _apply_domclick_feed_control(
            {"label": "Домклик"}, summaries
        )
        self.assertEqual(result["status"], "mismatch")
        self.assertEqual(result["reference_count"], 165)

    def test_cian_api_compares_published_count(self):
        feed_lots = [
            Lot("cian", "lot-1", 1, None, None, None, None, None),
            Lot("cian", "lot-2", 2, None, None, None, None, None),
        ]
        card = {"label": "Циан", "url": "https://cian.example", "feed_count": 2}
        snapshot = {
            "status": "ok",
            "check_method": "api_published_count",
            "published_count": 3,
        }
        result = _apply_marketplace_api_snapshot("cian", card, snapshot, feed_lots)
        self.assertEqual(result["status"], "mismatch")
        self.assertEqual(result["offer_count"], 3)

    def test_avito_api_compares_active_project_count(self):
        feed_lots = [
            Lot("avito", "lot-1", 1, None, None, None, None, None),
            Lot("avito", "lot-2", 2, None, None, None, None, None),
        ]
        card = {"label": "Авито", "url": "https://avito.example", "feed_count": 2}
        snapshot = {
            "status": "ok",
            "check_method": "api_address_count",
            "project_active_count": 3,
            "account_active_count": 3,
        }
        result = _apply_marketplace_api_snapshot("avito", card, snapshot, feed_lots)
        self.assertEqual(result["status"], "mismatch")
        self.assertEqual(result["offer_count"], 3)

    def test_yandex_uses_discount_final_price(self):
        xml = """<?xml version="1.0"?>
        <realty-feed xmlns="http://webmaster.yandex.ru/schemas/feed/realty/2010-06">
          <generation-date>2026-08-19T20:24:19+03:00</generation-date>
          <offer internal-id="lot-1">
            <area><value>30.6</value></area>
            <price><value>29250540</value></price>
            <discount><final-price>26325486</final-price></discount>
            <studio>true</studio><floor>2</floor><floors-total>14</floors-total>
          </offer>
        </realty-feed>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.xml"
            path.write_text(xml, encoding="utf-8")
            lots, _ = parse_yrl(path, "yandex")
        self.assertEqual(lots[0].price, 26325486)
        self.assertEqual(lots[0].base_price, 29250540)
        self.assertEqual(lots[0].rooms, 0)

    def test_notification_waits_for_second_identical_run(self):
        source_results = {
            source: {
                "missing_from_feed": ["lot"] if source == "cian" else [],
                "extra_in_feed": [],
                "field_mismatch_counts": {},
            }
            for source in OFFER_FEEDS
        }
        report = {
            "checked_at": "2026-08-19T20:00:00+03:00",
            "master_vs_feeds": {
                "sources": source_results,
            },
            "site_vs_master": {
                "site_only": [],
                "master_only": [],
                "matched_field_mismatch_counts": {},
            },
            "cross_feed": {
                "field_mismatch_counts": {},
                "source_outlier_counts": {},
            },
            "feeds": {
                source: {"stale_content": {"july_discount": 0}}
                for source in OFFER_FEEDS
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            self.assertFalse(should_notify(report, state, 2))
            self.assertTrue(should_notify(report, state, 2))
            self.assertFalse(should_notify(report, state, 2))

    def test_site_difference_does_not_trigger_feed_alert(self):
        report = {
            "checked_at": "2026-08-27T15:00:00+03:00",
            "master_vs_feeds": {
                "sources": {
                    source: {
                        "missing_from_feed": [],
                        "extra_in_feed": [],
                        "field_mismatch_counts": {},
                    }
                    for source in OFFER_FEEDS
                },
            },
            "site_vs_master": {
                "site_only": [{"site_id": 871}],
                "master_only": [{"external_id": "lot-1"}],
                "matched_field_mismatch_counts": {"floors_total": 1},
            },
            "cross_feed": {
                "field_mismatch_counts": {},
                "source_outlier_counts": {},
            },
            "feeds": {
                source: {"stale_content": {"july_discount": 0}}
                for source in OFFER_FEEDS
            },
            "platform_cards": {},
        }
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            self.assertFalse(should_notify(report, state, 1))


    def test_cian_detects_studio_from_description(self):
        xml = """<?xml version="1.0"?>
        <feed><feed_version>2</feed_version><object>
          <ExternalId>lot-2</ExternalId><TotalArea>28.5</TotalArea>
          <FloorNumber>3</FloorNumber><Description>Представляем студию 28,5 м²</Description>
          <BargainTerms><Price>26619570</Price></BargainTerms>
          <Building><FloorsCount>23</FloorsCount></Building>
        </object></feed>"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.xml"
            path.write_text(xml, encoding="utf-8")
            lots, _ = parse_cian(path)
        self.assertEqual(lots[0].rooms, 0)

    def test_yandex_public_card_count_matches_feed(self):
        document = """
        <html><head><title>Премиальный дом МАСТЕРС — 155 квартир</title></head>
        <body>Премиальный дом МАСТЕРС — 155 квартир по цене от 26 млн ₽</body></html>
        """
        card = parse_platform_page(
            "yandex_realty",
            document,
            {"downloaded": True, "final_url": "https://realty.yandex.ru/masters-4540998/"},
            155,
        )
        self.assertEqual(card["status"], "ok")
        self.assertEqual(card["offer_count"], 155)

    def test_platform_card_count_mismatch_is_explicit(self):
        document = """
        <html><head><title>ЖК «Премиальный дом МАСТЕРС»</title></head>
        <body><div>149 предложений</div></body></html>
        """
        card = parse_platform_page(
            "m2",
            document,
            {"downloaded": True, "final_url": "https://m2.ru/masters/"},
            155,
        )
        self.assertEqual(card["status"], "mismatch")
        self.assertEqual(card["offer_count"], 149)

    def test_telegram_message_separates_site_feeds_and_platforms(self):
        feeds = {
            source: {"lot_count": 155, "stale_content": {"july_discount": 0}}
            for source in OFFER_FEEDS
        }
        report = {
            "checked_at": "2026-08-20T01:00:00+03:00",
            "master": {"lot_count": 155, "total_property_count": 155},
            "feeds": feeds,
            "master_vs_feeds": {
                "master_count": 155,
                "total_id_difference_count": 0,
                "field_mismatch_counts": {},
                "sources": {
                    source: {
                        "missing_from_feed": [],
                        "extra_in_feed": [],
                        "field_mismatch_counts": {},
                    }
                    for source in OFFER_FEEDS
                },
            },
            "site_vs_master": {
                "site_count": 155,
                "site_only": [],
                "master_only": [],
                "matched_field_mismatch_counts": {},
            },
            "cross_feed": {"field_mismatch_counts": {}},
            "platform_cards": {
                "m2": {
                    "label": "М2",
                    "status": "ok",
                    "offer_count": 155,
                    "feed_count": 155,
                }
            },
        }
        message = format_telegram(report)
        self.assertIn("Материнский XML Capital Group (эталон)", message)
        self.assertIn("✅ Все 5 XML совпадают с эталоном", message)
        self.assertIn("Сайт (справочно, не эталон)", message)
        self.assertNotIn("служебный", message)
        self.assertIn("Площадки — факт размещения (API / карточки ЖК)", message)
        self.assertIn("✅ М2: 155 предложений = XML 155", message)
        self.assertNotIn("На сайте, но не в фидах", message)

    def test_telegram_reports_cian_avito_apis_and_july_copy(self):
        feeds = {
            source: {
                "lot_count": 155,
                "stale_content": {"july_discount": 155 if source == "cian" else 0},
            }
            for source in OFFER_FEEDS
        }
        report = {
            "checked_at": "2026-08-20T01:00:00+03:00",
            "master": {"lot_count": 155, "total_property_count": 155},
            "feeds": feeds,
            "master_vs_feeds": {
                "master_count": 155,
                "total_id_difference_count": 0,
                "field_mismatch_counts": {},
                "sources": {
                    source: {
                        "missing_from_feed": [],
                        "extra_in_feed": [],
                        "field_mismatch_counts": {},
                    }
                    for source in OFFER_FEEDS
                },
            },
            "site_vs_master": {
                "site_count": 155,
                "site_only": [],
                "master_only": [],
                "matched_field_mismatch_counts": {},
            },
            "cross_feed": {"field_mismatch_counts": {}},
            "platform_cards": {
                "cian": {
                    "label": "Циан",
                    "status": "ok",
                    "check_method": "api_published_count",
                    "offer_count": 155,
                    "feed_count": 155,
                },
                "avito": {
                    "label": "Авито",
                    "status": "mismatch",
                    "check_method": "api_address_count",
                    "offer_count": 165,
                    "feed_count": 155,
                },
            },
        }
        message = format_telegram(report)
        self.assertIn("Циан API: опубликовано 155 = XML 155", message)
        self.assertIn("Авито API: активно 165, XML 155", message)
        self.assertIn("скидка 10% в июле", message)

    def test_telegram_uses_master_hierarchy_and_avoids_false_floor_error(self):
        feeds = {
            source: {"lot_count": 165, "stale_content": {"july_discount": 165}}
            for source in OFFER_FEEDS
        }
        report = {
            "checked_at": "2026-08-20T11:00:00+03:00",
            "master": {"lot_count": 165, "total_property_count": 176},
            "feeds": feeds,
            "master_vs_feeds": {
                "master_count": 165,
                "total_id_difference_count": 5,
                "field_mismatch_counts": {},
                "sources": {
                    source: {
                        "missing_from_feed": ["lot"] * 5
                        if source == "domclick"
                        else [],
                        "extra_in_feed": [],
                        "field_mismatch_counts": {},
                    }
                    for source in OFFER_FEEDS
                },
            },
            "site_vs_master": {
                "site_count": 155,
                "site_only": [],
                "master_only": [{"external_id": "lot"}],
                "matched_field_mismatch_counts": {},
            },
            "cross_feed": {
                "field_mismatch_counts": {},
                "id_differences": {},
            },
            "platform_cards": {
                "domclick": {
                    "label": "Домклик",
                    "status": "mismatch",
                    "check_method": "feed_assumption",
                    "offer_count": 160,
                    "feed_count": 160,
                    "reference_count": 165,
                }
            },
        }
        message = format_telegram(report)
        self.assertIn("🔴 Домклик: XML 160, остальные XML 165", message)
        self.assertIn("🔴 Домклик ↔ эталон", message)
        self.assertNotIn("этажность различается", message)
        self.assertIn("Сайт (справочно, не эталон)", message)
        self.assertNotIn("Сайт ↔ материнский XML, ошибки полей", message)
        self.assertIn("🔴 Устаревшая фраза", message)


if __name__ == "__main__":
    unittest.main()
