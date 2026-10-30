import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import monitor

ROOT = Path(__file__).resolve().parents[1]
CLIENTS = json.loads((ROOT / 'clients.json').read_text())


class NewClientTests(unittest.TestCase):
    def test_shared_cian_source_is_filtered_by_exact_project_and_residential_category(self):
        xml = '<feed><feed_version>2</feed_version>'
        for i, name, category in [('1', 'Река', 'newBuildingFlatSale'),
                                  ('2', 'Остров', 'newBuildingFlatSale'),
                                  ('3', 'Река', 'commercialSale')]:
            xml += ('<object><ExternalId>' + i + '</ExternalId><Category>' + category +
                    '</Category><JKSchema><Name>' + name + '</Name></JKSchema>'
                    '<TotalArea>40</TotalArea></object>')
        xml += '</feed>'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'client.xml'
            path.write_text(xml)
            lots, meta = monitor.parse_cian(path, project='reka', category='residential',
                                            source={'project_names': ['Река']})
            self.assertEqual([lot.external_id for lot in lots], ['1'])
            self.assertEqual(meta['raw_count'], 3)
            self.assertEqual(meta['project_residential_count'], 1)
            with self.assertRaises(ValueError):
                monitor.parse_cian(path, project='missing', category='residential',
                                   source={'project_names': ['Неверное название']})

    def test_html_and_empty_client_cian_do_not_become_zero_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'client.xml'
            for xml in ('<html/>', '<feed><feed_version>2</feed_version></feed>'):
                path.write_text(xml)
                with self.assertRaises(ValueError):
                    monitor.parse_cian(path, project='p', category='residential', source={})

    def test_zero_storeys_in_client_source_is_unknown_not_zero_storey_building(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'client.xml'
            path.write_text('<feed><object><ExternalId>1</ExternalId>'
                            '<Category>newBuildingFlatSale</Category><TotalArea>40</TotalArea>'
                            '<Building><FloorsCount>0</FloorsCount></Building></object></feed>')
            lots, meta = monitor.parse_cian(path, project='p', category='residential', source={})
            self.assertIsNone(lots[0].floors_total)
            self.assertEqual(meta['missing_building_height_count'], 1)

    def test_disabled_cian_never_requests_api_even_if_token_is_set(self):
        account = {'enabled': False, 'disabled_reason': 'Нужен подтверждённый ключ',
                   'source': 'cian', 'projects': ['p']}
        with patch.dict(monitor.API_ACCOUNTS, {'blocked': account}):
            with patch.object(monitor, '_request_json') as request:
                snapshot = monitor.fetch_cian_snapshot('residential', 'blocked')
        request.assert_not_called()
        self.assertEqual(snapshot['status'], 'not_configured')
        self.assertIn('подтверждённый', snapshot['reason'])

    def test_only_verified_project_specific_id_suffix_is_removed(self):
        with patch.object(monitor, 'DATA', {'projects': {'p': {'feed_id_suffixes': {'cian': '_11'}}}}):
            self.assertEqual(monitor._feed_external_id('123_11', source='cian', project='p'), '123')
            self.assertEqual(monitor._feed_external_id('123_11', source='cian', project='other'), '123_11')
            self.assertEqual(monitor._feed_external_id('123_11', source='yandex', project='p'), '123_11')
            with self.assertRaises(ValueError):
                monitor._feed_external_id('123_12', source='cian', project='p')
            with self.assertRaises(ValueError):
                monitor._feed_external_id('_11', source='cian', project='p')

    def test_eleven_new_projects_have_five_isolated_tenants(self):
        tenants = ['donstroy', 'kortros', 'afi', 'apsis', 'sovcombank']
        self.assertEqual(sum(len(CLIENTS[t]['projects']) for t in tenants), 11)
        self.assertEqual(len({CLIENTS[t]['username'] for t in tenants}), 5)
        for tenant in ('donstroy', 'afi'):
            self.assertFalse(CLIENTS[tenant]['accounts']['cian_1']['enabled'])
        for tenant in ('afi', 'sovcombank', 'apsis'):
            self.assertTrue(all(p['master']['kind'] == 'unavailable'
                                for p in CLIENTS[tenant]['projects'].values()))

    def test_all_new_tenants_can_render_offline_without_secrets_or_false_green(self):
        for tenant in ('donstroy', 'kortros', 'afi', 'apsis', 'sovcombank'):
            with self.subTest(tenant=tenant), tempfile.TemporaryDirectory() as directory:
                env = {**os.environ, 'MONITOR_TENANT': tenant, 'PYTHONDONTWRITEBYTECODE': '1'}
                for key in list(env):
                    if key.startswith(('CIAN_', 'AVITO_', 'TELEGRAM_')):
                        del env[key]
                proc = subprocess.run([sys.executable, str(ROOT / 'monitor.py'), '--offline',
                                       '--cache-dir', directory], env=env, cwd=ROOT,
                                      capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn('<b>🔴 ', proc.stdout)
                self.assertLessEqual(len(proc.stdout.strip().encode('utf-16-le')) // 2, 4096)
                self.assertIn('Проверено:', proc.stdout)
                for project in CLIENTS[tenant]['projects'].values():
                    self.assertIn(project['name'], proc.stdout)
                if tenant == 'sovcombank':
                    self.assertIn('проверка сайта не применяется', proc.stdout)
                    self.assertNotIn('Сайт недоступен', proc.stdout)
                if tenant in ('donstroy', 'afi'):
                    self.assertIn('Ключ Циан совпадает', proc.stdout)

    def test_installer_rejects_duplicate_or_disabled_credentials(self):
        spec = importlib.util.spec_from_file_location('new_install', ROOT / 'deploy/onboard_clients.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        bundle = {t: {'TELEGRAM_BOT_TOKEN': 'test-' + t} for t in module.NEW_TENANTS}
        module.validate_bundle(bundle, CLIENTS)
        bundle['afi']['CIAN_API_KEY_CIAN_1'] = 'not-allowed-test-value'
        with self.assertRaises(ValueError):
            module.validate_bundle(bundle, CLIENTS)
        del bundle['afi']['CIAN_API_KEY_CIAN_1']
        bundle['afi']['TELEGRAM_BOT_TOKEN'] = bundle['donstroy']['TELEGRAM_BOT_TOKEN']
        with self.assertRaises(ValueError):
            module.validate_bundle(bundle, CLIENTS)


if __name__ == '__main__':
    unittest.main()
