import ast
import copy
import json
import http.client
from pathlib import Path
import socket
import ssl
import tempfile
from types import SimpleNamespace
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

import site_availability as checks
import site_availability_integration as integration
from publication_control import install


class SiteAvailabilityTests(unittest.TestCase):
    def response(self, status=200):
        context = MagicMock()
        context.__enter__.return_value.status = status
        return context

    def test_get_only_headers_no_response_body_and_default_tls(self):
        response = self.response()
        with patch.object(checks.urllib.request, 'urlopen', return_value=response) as request:
            result = checks.check_url('https://example.test/', attempts=1)
        self.assertTrue(result.available)
        self.assertEqual(result.status_code, 200)
        response.__enter__.return_value.read.assert_not_called()
        self.assertEqual(request.call_args.args[0].get_method(), 'GET')
        self.assertNotIn('context', request.call_args.kwargs)

    def test_all_http_failures_retry_and_stop_on_success(self):
        for code in (401, 403, 404, 429, 500, 503):
            with self.subTest(code=code):
                error = urllib.error.HTTPError('https://example.test', code, 'error', {}, None)
                with patch.object(checks.urllib.request, 'urlopen', side_effect=[error, self.response()]) as request:
                    with patch.object(checks.time, 'sleep') as sleep:
                        result = checks.check_url('https://example.test', retry_delay=30)
                self.assertTrue(result.available)
                self.assertEqual(request.call_count, 2)
                sleep.assert_called_once_with(30)

    def test_failed_attempts_keep_human_reason_and_never_sleep_after_last(self):
        cases = [(socket.gaierror('getaddrinfo failed'), 'Ошибка DNS'),
                 (ssl.SSLCertVerificationError('certificate verify failed'), 'Ошибка HTTPS'),
                 (TimeoutError(), 'Таймаут'),
                 (http.client.BadStatusLine('invalid response'), 'Ошибка соединения')]
        for error, reason in cases:
            with self.subTest(reason=reason):
                with patch.object(checks.urllib.request, 'urlopen', side_effect=error) as request:
                    with patch.object(checks.time, 'sleep') as sleep:
                        result = checks.check_url('https://example.test', timeout=8, retry_delay=2)
                self.assertFalse(result.available)
                self.assertIsNone(result.status_code)
                self.assertIn(reason, result.reason)
                self.assertEqual(request.call_count, 3)
                self.assertEqual([c.args for c in sleep.call_args_list], [(2,), (2,)])

    def test_wrapped_dns_and_redirect_loop_not_available_despite_302(self):
        errors = [(urllib.error.URLError(socket.gaierror('getaddrinfo failed')), 'Ошибка DNS'),
                  (urllib.error.HTTPError('https://example.test', 302, 'infinite loop', {}, None), 'Ошибка перенаправлений')]
        for error, reason in errors:
            with patch.object(checks.urllib.request, 'urlopen', side_effect=error):
                result = checks.check_url('https://example.test', attempts=1)
            self.assertFalse(result.available)
            self.assertIn(reason, result.reason)

    def test_response_without_http_status_is_not_a_false_success(self):
        with patch.object(checks.urllib.request, 'urlopen', return_value=self.response(None)):
            result = checks.check_url('https://example.test', attempts=1)
        self.assertFalse(result.available)
        self.assertIn('не вернул HTTP-код', result.reason)

    def test_duplicate_url_checked_once_but_each_project_receives_result(self):
        sites = [{'client': 'Клиент', 'project': p, 'url': 'https://example.test/'} for p in ('A', 'B')]
        observed = []
        with patch.object(checks, 'check_url', return_value=checks.CheckResult(200, True)) as request:
            failed = checks.check_sites(sites, on_result=lambda site, result: observed.append(site['project']))
        self.assertEqual(failed, [])
        self.assertEqual(observed, ['A', 'B'])
        request.assert_called_once()

    def test_default_separate_notifier_sends_nothing_for_healthy_sites(self):
        send = MagicMock()
        with patch.object(checks, 'check_sites', return_value=[]):
            self.assertEqual(checks.run_availability_check([], send), [])
        send.assert_not_called()

    def test_telegram_utf16_limit_and_atomic_site_blocks(self):
        sites = [{'client': 'Клиент', 'project': '🔥' * 150, 'url': 'https://example.test/' + str(i),
                  'reason': 'HTTP 503 — временно недоступен.'} for i in range(30)]
        messages = checks.format_unavailable_messages(sites)
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(m.encode('utf-16-le')) // 2 <= 4000 for m in messages))
        self.assertEqual(sum(message.count('🌐') for message in messages), 30)
        with self.assertRaises(ValueError):
            checks.format_unavailable_messages([dict(sites[0], project='🔥' * 3000)])

    def test_invalid_attempt_settings(self):
        for attempts in (0, 1.5, True):
            with self.assertRaises(ValueError):
                checks.check_url('https://example.test', attempts=attempts)


class ReportAvailabilityTests(unittest.TestCase):
    def test_site_only_refresh_preserves_saved_inventory_without_feed_or_api_calls(self):
        root = Path(__file__).resolve().parents[1]
        for folder in ('dar-feed-monitor', 'ikon-feed-monitor', 'developer-feed-monitor'):
            with self.subTest(folder=folder), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / 'latest.json'
                saved = {'checked_at': '2026-10-28T15:30:00+03:00', 'categories': {'residential': {'projects': {}}}}
                output.write_text(json.dumps(saved))
                args = SimpleNamespace(refresh_site_availability=True, output=output, offline=False,
                                       send_telegram=False)
                download, analyze = MagicMock(), MagicMock()
                namespace = dict(build_parser=lambda: SimpleNamespace(parse_args=lambda _: args),
                                 download_sources=download, analyze_directory=analyze,
                                 CLIENT_NAME='Клиент', PROJECTS={}, WEBSITES={},
                                 json=json, format_telegram=lambda report: 'report')
                tree = ast.parse((root / folder / 'monitor.py').read_text())
                main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
                main.returns = None
                for arg in main.args.args:
                    arg.annotation = None
                exec(compile(ast.Module(body=[main], type_ignores=[]), folder, 'exec'), namespace)
                with patch.object(integration, 'check_report_sites') as probe:
                    self.assertEqual(namespace['main']([]), 0)
                download.assert_not_called()
                analyze.assert_not_called()
                self.assertEqual(probe.call_args.args[0], saved)
                self.assertEqual(json.loads(output.read_text()), saved)

    def test_site_only_refresh_requires_a_valid_existing_report(self):
        with self.assertRaises(ValueError):
            integration.read_existing_report(None)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'latest.json'
            output.write_text('{}')
            with self.assertRaises(ValueError):
                integration.read_existing_report(output)

    def sites(self):
        return integration.build_public_sites('Клиент', {'p': 'Проект'}, {'p': 'https://example.test/'})

    def test_current_results_preserve_feed_status_counts_and_do_not_send_messages(self):
        report = {'overall_status': 'ok', 'feeds': {'cian': {'lot_count': 12}},
                  'site': {'lot_count': None}, 'checked_at': '2026-10-28T13:00:00+03:00'}
        original = copy.deepcopy(report)
        with patch.object(checks.urllib.request, 'urlopen', return_value=SiteAvailabilityTests().response()):
            integration.enrich_report_with_availability(report, self.sites())
        self.assertEqual({k: v for k, v in report.items() if k != 'site_availability'}, original)
        self.assertEqual(report['site_availability']['p']['status_code'], 200)
        self.assertIn('✅ Сайт отвечает', integration.availability_status_line(report, 'p'))
        self.assertIsNone(report['site']['lot_count'])

    def test_offline_never_requests_a_site_or_reuses_old_green_status(self):
        report = {'site_availability': {'p': {'status': 'available'}}}
        with patch.object(integration, 'check_sites') as probe:
            integration.enrich_report_with_availability(report, self.sites(), offline=True)
        probe.assert_not_called()
        self.assertIsNone(report['site_availability']['p']['available'])
        self.assertIn('не проверена', integration.availability_status_line(report, 'p'))

    def test_probe_defect_does_not_prevent_the_original_feed_report(self):
        report = {'overall_status': 'ok', 'site': {'lot_count': 17}}
        with patch.object(integration, 'check_sites', side_effect=RuntimeError('broken probe')):
            integration.enrich_report_with_availability(report, self.sites())
        self.assertEqual(report['overall_status'], 'ok')
        self.assertEqual(report['site']['lot_count'], 17)
        self.assertEqual(report['site_availability']['p']['status'], 'not_checked')

    def test_public_configuration_excludes_projects_without_sites(self):
        self.assertEqual(integration.build_public_sites('Клиент', {'p': 'Проект'}, {'p': None}), [])
        report = {'overall_status': 'ok'}
        with patch.object(integration, 'check_sites') as probe:
            integration.check_report_sites(report, 'Клиент', {'p': 'Проект'}, {'p': 'not-a-url'})
        probe.assert_not_called()
        self.assertEqual(report['overall_status'], 'ok')
        self.assertEqual(report['site_availability']['p']['status'], 'not_checked')

    def test_live_render_separates_reachability_from_inventory_read_error(self):
        m = SimpleNamespace(download_sources=lambda p: {}, analyze_directory=lambda p: {},
            CATEGORIES={'r': 'Жилая недвижимость'}, PROJECTS={'p': 'Проект'},
            SOURCE_UNAVAILABLE='SOURCE_UNAVAILABLE', FEED_LABELS={}, FIELD_LABELS={},
            _feed_counts_line=lambda f: 'нет данных', _marketing_lines=lambda p: [],
            _checked_label=lambda t: t)
        install(m, 'Клиент')
        project = {'master': {'status': 'OK', 'lot_count': 12}, 'feeds': {},
                   'master_vs_feeds': {}, 'cross_feed': {},
                   'website': {'status': 'SOURCE_UNAVAILABLE', 'lot_count': None, 'reason': 'каталог не прочитан'}}
        report = {'overall_status': 'ok', 'checked_at': '2026-10-28',
                  'site_availability': {'p': {'status': 'available', 'status_code': 200}},
                  'categories': {'r': {'projects': {'p': project}, 'api_checks': {}, 'platform_cards': {}}}}
        text = m.format_telegram(report)
        self.assertIn('<b>🟢 Клиент</b>', text)
        self.assertTrue(text.startswith('✅ Сайт доступен'))
        self.assertIn('Количество квартир на сайте не определено', text)
        self.assertNotIn('Сайт недоступен', text)
        report['site_availability']['p'] = {'status': 'unavailable', 'reason': 'Ошибка DNS'}
        text = m.format_telegram(report)
        self.assertIn('<b>🟢 Клиент</b>', text)
        self.assertTrue(text.startswith('❌ Сайт недоступен — не найден адрес сайта'))

    def test_masters_standalone_copies_match_shared_modules(self):
        root = Path(__file__).resolve().parents[1]
        for name in ('site_availability.py', 'site_availability_integration.py'):
            self.assertEqual((root / name).read_bytes(), (root / 'masters-feed-monitor' / name).read_bytes())


if __name__ == '__main__':
    unittest.main()
