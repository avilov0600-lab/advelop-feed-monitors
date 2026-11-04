import concurrent.futures
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

import telegram_delivery as delivery

STAMP = '2026-11-04T09:00:00+03:00'
OK = {'state': 'accepted', 'reason': 'accepted', 'message_id': 17}
NO = {'state': 'rejected', 'reason': 'chat_not_found', 'code': 400}
UNKNOWN = {'state': 'unconfirmed', 'reason': 'network'}


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'delivery.sqlite3'
        self.ledger = delivery.Ledger(self.path)
        self.output = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.output)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)

    def run_delivery(self, sender, ledger=None, stamp=STAMP, recipients=('101', '102')):
        return delivery.deliver(ledger or self.ledger, 'dar', 'report', stamp, recipients, sender)

    def test_retry_after_restart_only_sends_to_rejected_recipient(self):
        send = Mock(side_effect=[OK, NO])
        first = self.run_delivery(send)
        self.assertEqual(first['delivered'], 1)
        retry = Mock(return_value=OK)
        second = self.run_delivery(retry, delivery.Ledger(self.path))
        retry.assert_called_once_with('102')
        self.assertEqual(second['skipped'], 1)
        self.assertEqual(second['delivered'], 1)
        final = Mock(return_value=OK)
        self.run_delivery(final)
        final.assert_not_called()

    def test_uncertain_result_is_not_resent_to_any_successful_or_unknown_recipient(self):
        self.run_delivery(Mock(side_effect=[OK, UNKNOWN]))
        send = Mock(return_value=OK)
        result = self.run_delivery(send)
        send.assert_not_called()
        self.assertEqual(result['unconfirmed'], 1)
        self.assertEqual(result['skipped'], 1)

    def test_crash_after_claim_never_reclaims_automatically(self):
        self.ledger.claim('dar', 'report', STAMP, '101')
        send = Mock(return_value=OK)
        result = self.run_delivery(send, recipients=['101'])
        send.assert_not_called()
        self.assertEqual(result['unconfirmed'], 1)

    def test_crash_preserves_entire_recipient_plan_including_unattempted_chats(self):
        self.ledger.prepare('dar', 'report', STAMP, ['101', '102'])
        claim = self.ledger.claim('dar', 'report', STAMP, '101')
        self.ledger.finish('dar', claim['report_key'], '101', claim['claim'], OK)
        # Restart before the second chat was even claimed.
        ledger = delivery.Ledger(self.path)
        self.assertEqual({r['chat_id']: r['state'] for r in ledger.status('dar')[1]},
                         {'101': 'accepted', '102': 'pending'})
        sender = Mock(return_value=OK)
        self.run_delivery(sender, ledger)
        sender.assert_called_once_with('102')

    def test_parallel_workers_cannot_claim_same_recipient(self):
        def claim(_):
            return self.ledger.claim('dar', 'report', STAMP, '101')['claimed']
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(claim, range(8))), 1)

    def test_new_scheduled_report_and_other_tenant_are_independent(self):
        self.run_delivery(Mock(return_value=OK), recipients=['101'])
        send = Mock(return_value=OK)
        self.run_delivery(send, stamp='2026-11-04T13:00:00+03:00', recipients=['101'])
        send.assert_called_once_with('101')
        self.assertTrue(self.ledger.claim('ikon', 'report', STAMP, '101')['claimed'])

    def test_changed_text_cannot_turn_same_report_into_duplicate_broadcast(self):
        self.run_delivery(Mock(return_value=OK), recipients=['101'])
        with self.assertRaisesRegex(ValueError, 'content changed'):
            self.ledger.claim('dar', 'report with different formatting', STAMP, '101')

    def test_receipt_storage_retry_is_idempotent_but_cannot_overwrite_success(self):
        claim = self.ledger.claim('dar', 'report', STAMP, '101')
        args = ('dar', claim['report_key'], '101', claim['claim'])
        self.ledger.finish(*args, OK)
        self.ledger.finish(*args, OK)
        with self.assertRaisesRegex(ValueError, 'already finalized'):
            self.ledger.finish(*args, NO)
        with self.assertRaisesRegex(ValueError, 'Stale'):
            self.ledger.finish('dar', claim['report_key'], '101', 'other-claim', OK)

    def test_rate_limit_retry_is_recipient_scoped_and_bounded(self):
        limited = {'state': 'rejected', 'code': 429, 'reason': 'rate_limited', 'retry_after': 0}
        send = Mock(side_effect=[OK, limited, limited, limited])
        with patch.object(delivery.time, 'sleep'):
            result = self.run_delivery(send)
        self.assertEqual([c.args[0] for c in send.call_args_list], ['101', '102', '102', '102'])
        self.assertEqual(result['rejected'], 1)

    def test_long_rate_limit_does_not_retry_early(self):
        send = Mock(return_value={'state': 'rejected', 'code': 429, 'reason': 'rate_limited', 'retry_after': 600})
        self.run_delivery(send, recipients=['101'])
        send.reset_mock()
        self.run_delivery(send, recipients=['101'])
        send.assert_not_called()

    def test_journal_failure_prevents_any_untracked_send(self):
        ledger = Mock()
        ledger.claim.side_effect = OSError('disk unavailable')
        send = Mock()
        with self.assertRaises(OSError):
            self.run_delivery(send, ledger)
        send.assert_not_called()

    def test_log_privacy_and_private_file_permissions(self):
        self.run_delivery(Mock(return_value=OK), recipients=['123456789'])
        log = self.output.getvalue()
        self.assertNotIn('123456789', log)
        self.assertNotIn('chat_id', log)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        _, rows = self.ledger.status('dar')
        self.assertEqual(rows[0]['chat_id'], '123456789')

    def test_rollout_does_not_resend_preexisting_reports_or_overwrite_receipts(self):
        self.run_delivery(Mock(return_value=OK), recipients=['101'])
        self.ledger.seed_previous('dar', 'report', STAMP, ['101', '102'])
        send = Mock(return_value=OK)
        result = self.run_delivery(send)
        send.assert_not_called()
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(result['unconfirmed'], 1)
        with self.assertRaisesRegex(ValueError, 'content changed'):
            self.ledger.seed_previous('dar', 'changed', STAMP, ['103'])

    def test_monitor_integration_reuses_journal_and_honors_older_report(self):
        opener = Mock(side_effect=lambda *a, **k: io.BytesIO(b'{"ok":true,"result":{"message_id":7}}'))
        module = SimpleNamespace(sync_latest_message=Mock(return_value={
            'stored': True, 'recipients': [{'chat_id': '101', 'revision': 1}]}),
            _telegram_urlopen=opener)
        with patch.dict(os.environ, {'TELEGRAM_DELIVERY_JOURNAL': str(self.path), 'TELEGRAM_BOT_TOKEN': 'secret'}, clear=True):
            delivery.install_monitor(module, 'dar')
            module.broadcast_telegram('report', STAMP)
            module.broadcast_telegram('report', STAMP)
            opener.assert_called_once()
            module.sync_latest_message.return_value = {'stored': False, 'reason': 'older_report'}
            self.assertTrue(module.broadcast_telegram('old', STAMP)['skipped_older_report'])


class SenderTests(unittest.TestCase):
    def test_timeout_is_unknown_and_no_second_request_is_made(self):
        opener = Mock(side_effect=TimeoutError('URL_WITH_SECRET'))
        result = delivery.send_once('SECRET', '101', 'report', opener=opener)
        self.assertEqual(result, UNKNOWN)
        opener.assert_called_once()
        self.assertNotIn('SECRET', json.dumps(result))

    def test_confirmed_block_and_gateway_error_are_distinguished(self):
        for body, code, state in [(b'{"ok":false,"error_code":403,"description":"Forbidden: bot was blocked by the user"}',403,'rejected'),
                                  (b'<html>gateway</html>',502,'unconfirmed')]:
            error = urllib.error.HTTPError('secret', code, 'secret', {}, io.BytesIO(body))
            result = delivery.send_once('secret','101','report',opener=Mock(side_effect=error))
            self.assertEqual(result['state'], state)
            self.assertNotIn('secret', json.dumps(result))

    def test_success_requires_real_message_receipt(self):
        result = delivery.send_once('secret', '101', 'report',
                                    opener=Mock(return_value=io.BytesIO(b'{"ok":true}')))
        self.assertEqual(result['state'], 'unconfirmed')

    def test_remote_receipt_retry_does_not_call_sender(self):
        request = Mock(side_effect=[TimeoutError, {'ok': True, 'stored': True}])
        delivery.RemoteLedger(request).finish('masters', 'report', '101', 'claim', OK)
        self.assertEqual(request.call_count, 2)
        self.assertTrue(all(c.args[0]['action'] == 'delivery_finish' for c in request.call_args_list))


class RelayTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'advelop-telegram-relay/relay.py'
        spec = importlib.util.spec_from_file_location('delivery_test_relay', path)
        relay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(relay)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = relay.RelayStore(Path(self.temp.name) / 'relay.sqlite3')
        self.token = '123456789:' + 'a' * 35
        self.store.register_token('masters', relay._token_hash(self.token))
        self.store.mark_webhook_configured('masters')
        self.store.subscribe('masters', '101', 1)
        self.store.save_latest('masters', 'report', STAMP, None)
        self.app = relay.RelayApplication(self.store, 'https://example.test', {'masters': 'secret', 'ikon': 'second'})
        self.headers = {'X-Masters-Bot-Token': self.token}
        self.payload = {'action': 'delivery_claim', 'message': 'report', 'checked_at': STAMP, 'chat_id': '101'}

    def test_auth_and_tenant_scope_are_required(self):
        self.assertEqual(self.app.handle('/v1/masters/secret', {}, self.payload)[0], 403)
        self.assertEqual(self.app.handle('/v1/ikon/second', self.headers, self.payload)[0], 404)
        self.assertFalse((Path(self.temp.name) / 'delivery.sqlite3').exists())

    def test_remote_plan_cannot_include_another_recipient_or_report(self):
        payload = {**self.payload, 'action': 'delivery_prepare', 'recipients': ['101']}
        self.assertEqual(self.app.handle('/v1/masters/secret', self.headers, payload),
                         (200, {'ok': True, 'prepared': True}))
        ledger = delivery.Ledger(Path(self.temp.name) / 'delivery.sqlite3')
        self.assertEqual(ledger.status('masters')[1][0]['state'], 'pending')
        for override in [{'recipients':['101','102']}, {'message':'other'}]:
            self.assertEqual(self.app.handle('/v1/masters/secret', self.headers, {**payload, **override})[0], 409)

    def test_only_current_report_and_active_subscriber_can_be_claimed(self):
        for override in [{'message':'other'}, {'chat_id':'102'}, {'checked_at':'2026-11-03T09:00:00+03:00'}]:
            self.assertEqual(self.app.handle('/v1/masters/secret', self.headers, {**self.payload, **override})[0], 409)
        code, result = self.app.handle('/v1/masters/secret', self.headers, self.payload)
        self.assertEqual(code, 200)
        self.assertTrue(result['claimed'])
        self.assertFalse(self.app.handle('/v1/masters/secret', self.headers, self.payload)[1]['claimed'])


if __name__ == '__main__':
    unittest.main()
