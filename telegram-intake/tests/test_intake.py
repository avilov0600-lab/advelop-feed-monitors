import importlib.util
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from intake import Intake, plain_chunks
from transport import Telegram, TelegramError, NetworkError, connect_family, urlopen

spec = importlib.util.spec_from_file_location('test_registry', ROOT / 'deploy/shared_relay.py')
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Path(self.tmp.name) / 'state.sqlite3'
        self.store = relay.RelayStore(self.database)
        self.token = '123456789:' + 'a' * 35
        for tenant in ('masters', 'ikon'):
            self.store.register_token(tenant, relay._token_hash(self.token))
            self.store.mark_webhook_configured(tenant)
            self.store.save_latest(tenant, '<b>' + tenant + '</b> latest', '2026-10-19T09:00:00+03:00', 'HTML')
        self.app = relay.RelayApplication(self.store, 'https://example.test/relay', {'masters': 'secret', 'ikon': 'other'}, telegram_request=lambda *args: {'ok': True})
        self.telegram = MagicMock()
        self.worker = Intake('masters', self.app, '/relay/v1/masters/secret', self.token, self.database, self.telegram)

    def update(self, update_id=1, chat_id=100, command='/start'):
        return {'update_id': update_id, 'message': {'text': command, 'chat': {'id': chat_id, 'type': 'private'}}}

    def state(self):
        with self.worker.connect() as con:
            return [dict(row) for row in con.execute('SELECT state,attempts,part_index,plain_fallback FROM intake_outbox WHERE tenant=? ORDER BY update_id', ('masters',))]

    def test_start_subscribes_and_replays_latest_immediately(self):
        self.worker.receive(self.update())
        self.worker.drain()
        self.assertEqual(len(self.store.subscribers('masters', active_only=True)), 1)
        method, payload = self.telegram.call.call_args.args
        self.assertEqual(method, 'sendMessage')
        self.assertIn('<b>masters</b> latest', payload['text'])
        self.assertIn('подключены', payload['text'])
        self.assertEqual(payload['parse_mode'], 'HTML')
        self.assertEqual(self.state()[0]['state'], 'sent')

    def test_each_colleague_is_in_next_broadcast(self):
        for number in range(1, 6):
            self.worker.receive(self.update(number, number + 100))
        self.worker.drain()
        status, sync = self.app.handle('/relay/v1/masters/secret', {'X-MASTERS-Bot-Token': self.token},
                                      {'action': 'sync_latest', 'message': 'new', 'checked_at': '2026-10-19T13:00:00+03:00'})
        self.assertEqual(status, 200)
        self.assertEqual(sync['subscriber_count'], 5)
        self.assertEqual(self.telegram.call.call_count, 5)
        self.assertEqual(self.store.subscribers('ikon', active_only=True), [])

    def test_reply_network_failure_does_not_lose_command_or_block_next_user(self):
        self.worker.receive(self.update(1, 100))
        self.worker.receive(self.update(2, 200))
        self.telegram.call.side_effect = [NetworkError(), {}]
        self.worker.drain()
        self.assertEqual(self.worker.offset(), 3)
        self.assertEqual([r['state'] for r in self.state()], ['pending', 'sent'])
        self.assertEqual(len(self.store.subscribers('masters', active_only=True)), 2)
        restored = Intake('masters', self.app, self.worker.path, self.token, self.database, self.telegram)
        with restored.connect() as con:
            con.execute('UPDATE intake_outbox SET next_attempt=0')
        self.telegram.call.side_effect = None
        restored.drain()
        self.assertEqual([r['state'] for r in self.state()], ['sent', 'sent'])

    def test_stop_supersedes_pending_start_and_resubscribe_works(self):
        self.worker.receive(self.update(1))
        self.worker.receive(self.update(2, command='/stop'))
        self.assertEqual(self.store.subscribers('masters', active_only=True), [])
        self.worker.drain()
        self.assertEqual([r['state'] for r in self.state()], ['superseded', 'sent'])
        self.worker.receive(self.update(3))
        self.worker.drain()
        self.assertEqual(len(self.store.subscribers('masters', active_only=True)), 1)

    def test_duplicates_and_old_updates_do_not_duplicate_replies(self):
        self.worker.receive(self.update())
        self.worker.receive(self.update())
        self.worker.receive(self.update(0))
        self.worker.drain()
        self.assertEqual(self.telegram.call.call_count, 1)

    def test_blocked_chat_does_not_block_other_users(self):
        self.worker.receive(self.update(1, 100))
        self.worker.receive(self.update(2, 200))
        self.telegram.call.side_effect = [TelegramError(403), {}]
        self.worker.drain()
        self.assertEqual([r['state'] for r in self.state()], ['blocked', 'sent'])
        self.assertEqual(len(self.store.subscribers('masters', active_only=True)), 1)

    def test_bad_html_falls_back_without_losing_report(self):
        self.worker.receive(self.update())
        self.telegram.call.side_effect = TelegramError(400)
        self.worker.drain()
        self.assertEqual(self.state()[0]['plain_fallback'], 1)
        self.telegram.call.side_effect = None
        self.worker.drain()
        self.assertNotIn('parse_mode', self.telegram.call.call_args.args[1])
        self.assertIn('masters latest', self.telegram.call.call_args.args[1]['text'])

    def test_rate_limit_uses_retry_after_without_blocking_cursor(self):
        self.worker.receive(self.update())
        self.telegram.call.side_effect = TelegramError(429, 120)
        with patch('intake.time.time', return_value=100):
            self.worker.drain()
        with self.worker.connect() as con:
            row = con.execute('SELECT next_attempt FROM intake_outbox').fetchone()
        self.assertEqual(row[0], 220)
        self.assertEqual(self.worker.offset(), 2)

    def test_legacy_cursor_is_preserved_and_tenant_cursors_are_separate(self):
        with self.worker.connect() as con:
            con.execute('CREATE TABLE poll_cursor(id INTEGER PRIMARY KEY,update_id INTEGER)')
            con.execute('INSERT INTO poll_cursor VALUES(1,50)')
        other = Intake('ikon', self.app, '/relay/v1/ikon/other', self.token, self.database, self.telegram)
        self.assertEqual(other.offset(), 51)
        self.worker.receive(self.update(4))
        self.assertEqual(self.worker.offset(), 5)
        self.assertEqual(other.offset(), 51)

    def test_poll_heartbeats_only_after_success(self):
        self.telegram.call.return_value = []
        with patch('intake.notify') as notify:
            self.worker.poll()
        notify.assert_called_once_with('WATCHDOG=1')
        self.telegram.call.side_effect = NetworkError()
        with patch('intake.notify') as notify:
            with self.assertRaises(NetworkError):
                self.worker.poll()
        notify.assert_not_called()

    def test_non_command_does_not_subscribe(self):
        self.worker.receive(self.update(command='hello'))
        self.worker.drain()
        self.assertEqual(self.store.subscribers('masters', active_only=True), [])
        self.telegram.call.assert_not_called()

    def test_plain_chunks_preserve_text_and_telegram_utf16_limits(self):
        original = '🔥' * 3000
        result = plain_chunks({'chat_id': 1, 'text': '<b>' + original + '</b>', 'parse_mode': 'HTML'})
        self.assertEqual(''.join(p['text'] for p in result), original)
        self.assertTrue(all(len(p['text'].encode('utf-16-le')) // 2 <= 3500 for p in result))

    def test_masters_token_is_provisioned_only_after_authentication_on_server(self):
        directory = Path(self.tmp.name) / 'intake-tokens'
        with patch.dict(os.environ, {'RELAY_CAPTURE_MASTERS_TOKEN': '1'}), patch.object(relay, 'Path', return_value=directory):
            ok, _, _ = self.app._authenticate_monitor('masters', {'x-masters-bot-token': '987654321:' + 'b' * 35})
            self.assertFalse(ok)
            self.assertFalse(directory.exists())
            ok, _, _ = self.app._authenticate_monitor('masters', {'x-masters-bot-token': self.token})
            self.assertTrue(ok)
            path = directory / 'masters.token'
            self.assertEqual(path.read_text().strip(), self.token)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

    def test_polling_mode_never_reenables_webhook(self):
        request = MagicMock()
        self.app.telegram_request = request
        with patch.dict(os.environ, {'RELAY_DELIVERY_MODE': 'polling'}):
            self.app._configure_webhook('masters', self.token)
        request.assert_not_called()


class TransportTests(unittest.TestCase):
    def test_scheduled_sender_adapter_uses_ipv4_and_preserves_response(self):
        connection = MagicMock()
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.read.return_value = b'{"ok":true}'
        request = urllib.request.Request('https://api.telegram.org/botFAKE/sendMessage', data=b'x=y')
        with patch('transport.TelegramConnection', return_value=connection) as factory:
            with urlopen(request, timeout=15) as response:
                self.assertTrue(json.load(response)['ok'])
        factory.assert_called_once_with(socket.AF_INET, timeout=15)
        self.assertEqual(connection.request.call_args.args[3]['Content-Type'], 'application/x-www-form-urlencoded')

    def test_scheduled_sender_preserves_http_code_without_token_in_exception(self):
        connection = MagicMock()
        connection.getresponse.return_value.status = 403
        connection.getresponse.return_value.read.return_value = b'{"ok":false}'
        with patch('transport.TelegramConnection', return_value=connection):
            with self.assertRaises(urllib.error.HTTPError) as error:
                urlopen(urllib.request.Request('https://api.telegram.org/botPRIVATE/sendMessage'), timeout=8)
        self.assertEqual(error.exception.code, 403)
        self.assertNotIn('PRIVATE', str(error.exception))

    def test_scheduled_adapter_rejects_non_telegram_host(self):
        with self.assertRaises(ValueError):
            urlopen(urllib.request.Request('https://example.test/'), timeout=5)

    def test_ipv4_is_first(self):
        connection = MagicMock()
        connection.getresponse.return_value.read.return_value = b'{"ok":true,"result":[]}'
        with patch('transport.TelegramConnection', return_value=connection) as factory:
            self.assertEqual(Telegram('token').call('getUpdates'), [])
        factory.assert_called_once_with(socket.AF_INET, timeout=35)

    def test_fails_over_but_not_on_api_errors(self):
        bad, good = MagicMock(), MagicMock()
        bad.request.side_effect = TimeoutError()
        good.getresponse.return_value.read.return_value = b'{"ok":true,"result":[]}'
        api = Telegram('token')
        with patch('transport.TelegramConnection', side_effect=[bad, good]) as factory:
            api.call('getUpdates')
        self.assertEqual([c.args[0] for c in factory.call_args_list], [socket.AF_INET, socket.AF_INET6])
        self.assertEqual(api.preferred_family, socket.AF_INET6)
        good.getresponse.return_value.read.return_value = b'{"ok":false,"error_code":403}'
        with patch('transport.TelegramConnection', return_value=good) as factory:
            with self.assertRaises(TelegramError):
                api.call('sendMessage')
        self.assertEqual(factory.call_count, 1)

    def test_network_error_does_not_contain_token(self):
        connection = MagicMock()
        connection.request.side_effect = OSError('request URL with secret')
        with patch('transport.TelegramConnection', return_value=connection):
            with self.assertRaises(NetworkError) as caught:
                Telegram('PRIVATE_TOKEN').call('getUpdates')
        self.assertNotIn('PRIVATE_TOKEN', str(caught.exception))
        self.assertNotIn('secret', str(caught.exception))

    def test_connect_failure_closes_socket(self):
        connection = MagicMock()
        connection.connect.side_effect = TimeoutError()
        with patch('transport.socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]), patch('transport.socket.socket', return_value=connection):
            with self.assertRaises(TimeoutError):
                connect_family(('api.telegram.org', 443), 35, socket.AF_INET)
        connection.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
