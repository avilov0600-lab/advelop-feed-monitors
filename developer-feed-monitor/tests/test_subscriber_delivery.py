"""Exercise the actual new-client registry with the deployed durable intake."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'telegram-intake'))
from intake import Intake
from transport import NetworkError, TelegramError
from deploy import relay

TENANTS = ('donstroy', 'kortros', 'afi', 'apsis', 'sovcombank')


class SubscriberDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def worker(self, tenant):
        database = Path(self.temporary.name) / (tenant + '.sqlite3')
        store = relay.RelayStore(database)
        token = '123456789:' + 'a' * 35
        store.register_token(tenant, relay._token_hash(token))
        store.mark_webhook_configured(tenant)
        store.save_latest(tenant, '<b>' + tenant + '</b> последний отчёт',
                          '2026-10-28T13:00:00+03:00', 'HTML')
        app = relay.RelayApplication(store, 'https://localhost', {tenant: 'test-secret'})
        telegram = MagicMock()
        worker = Intake(tenant, app, '/v1/' + tenant + '/test-secret', token, database, telegram)
        return worker, store, telegram

    @staticmethod
    def update(number, chat=1001, command='/start'):
        return {'update_id': number, 'message': {'text': command,
                'chat': {'id': chat, 'type': 'private'}}}

    def test_new_colleagues_get_saved_report_and_join_next_broadcast_in_every_bot(self):
        for tenant in TENANTS:
            with self.subTest(tenant=tenant):
                worker, store, telegram = self.worker(tenant)
                for number, chat in enumerate((1001, 1002, 1003), 1):
                    worker.receive(self.update(number, chat))
                worker.drain()
                self.assertEqual(telegram.call.call_count, 3)
                self.assertEqual(len(store.subscribers(tenant, active_only=True)), 3)
                self.assertEqual(store.subscribers('another-client', active_only=True), [])
                for call in telegram.call.call_args_list:
                    self.assertEqual(call.args[0], 'sendMessage')
                    self.assertIn('<b>' + tenant + '</b> последний отчёт', call.args[1]['text'])
                    self.assertIn('подключены', call.args[1]['text'])
                    self.assertEqual(call.args[1]['parse_mode'], 'HTML')
                # Use the monitor's real authenticated sync action. No owner ID
                # is supplied, so every recipient must come from /start.
                status, result = worker.app.handle(worker.path, {'X-DAR-Bot-Token': worker.token},
                    {'action': 'sync_latest', 'message': 'Следующая рассылка',
                     'checked_at': '2026-10-28T15:30:00+03:00'})
                self.assertEqual(status, 200)
                self.assertEqual(result['subscriber_count'], 3)
                self.assertEqual({r['chat_id'] for r in result['recipients']}, {'1001', '1002', '1003'})

    def test_failed_reply_survives_restart_without_losing_subscription(self):
        for tenant in TENANTS:
            with self.subTest(tenant=tenant):
                worker, store, telegram = self.worker(tenant)
                worker.receive(self.update(1))
                telegram.call.side_effect = NetworkError()
                worker.drain()
                self.assertEqual(len(store.subscribers(tenant, active_only=True)), 1)
                restarted = Intake(tenant, worker.app, worker.path, worker.token, worker.database, telegram)
                with restarted.connect() as con:
                    con.execute('UPDATE intake_outbox SET next_attempt=0')
                telegram.call.side_effect = None
                restarted.drain()
                with restarted.connect() as con:
                    self.assertEqual(con.execute('SELECT state FROM intake_outbox').fetchone()[0], 'sent')
                self.assertIn('последний отчёт', telegram.call.call_args.args[1]['text'])

    def test_repeated_start_keeps_one_subscription_and_replays_current_saved_report(self):
        for tenant in TENANTS:
            with self.subTest(tenant=tenant):
                worker, store, telegram = self.worker(tenant)
                worker.receive(self.update(1))
                worker.drain()
                store.save_latest(tenant, 'Самый свежий сохранённый отчёт',
                                  '2026-10-28T15:30:00+03:00', None)
                worker.receive(self.update(2))
                worker.drain()
                self.assertEqual(len(store.subscribers(tenant, active_only=True)), 1)
                self.assertIn('Самый свежий сохранённый отчёт', telegram.call.call_args.args[1]['text'])

    def test_one_users_telegram_error_does_not_block_the_next_colleague(self):
        for tenant in TENANTS:
            with self.subTest(tenant=tenant):
                worker, store, telegram = self.worker(tenant)
                worker.receive(self.update(1, 1001))
                worker.receive(self.update(2, 1002))
                telegram.call.side_effect = [TelegramError(429, 30), {}]
                worker.drain()
                self.assertEqual(len(store.subscribers(tenant, active_only=True)), 2)
                with worker.connect() as con:
                    states = [r[0] for r in con.execute('SELECT state FROM intake_outbox ORDER BY update_id')]
                self.assertEqual(states, ['pending', 'sent'])


if __name__ == '__main__':
    unittest.main()
