import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor


class DeliveryTests(unittest.TestCase):
    def broadcast(self, effects):
        output = io.StringIO()
        with (
            mock.patch.dict('os.environ', {'TELEGRAM_LATEST_MESSAGE_URL': 'https://state.example'}),
            mock.patch.object(monitor, 'sync_latest_telegram_message',
                              return_value={'recipients': ['42', '43']}),
            mock.patch.object(monitor, 'send_telegram', side_effect=effects) as send,
            mock.patch.object(monitor.time, 'sleep') as sleep,
            contextlib.redirect_stdout(output),
        ):
            try:
                result = monitor.broadcast_telegram_message('digest', 'checked')
            except RuntimeError as error:
                result = error
        records = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith('{')]
        receipts = [r for r in records if r['event'] == 'telegram_delivery']
        return result, receipts, send, sleep

    def test_timeout_is_not_resent_and_other_recipient_is_sent_once(self):
        result, receipts, send, _ = self.broadcast([
            TimeoutError('https://api.telegram.org/botSECRET/sendMessage'), {'message_id': 11},
        ])
        self.assertIsInstance(result, RuntimeError)
        self.assertEqual([r['status'] for r in receipts], ['unconfirmed', 'accepted'])
        self.assertEqual(receipts[0]['reason'], 'TimeoutError')
        self.assertEqual(receipts[0]['recipient_ref'], hashlib.sha256(b'42').hexdigest()[:16])
        self.assertEqual(send.call_args_list,
                         [mock.call('digest', chat_id='42'), mock.call('digest', chat_id='43')])
        self.assertNotIn('SECRET', json.dumps(receipts))
        self.assertNotIn('chat_id', json.dumps(receipts))

    def test_explicit_rate_limit_retries_only_rejected_recipient(self):
        error = monitor.TelegramDeliveryError(429, 'rate_limited', rejected=True, retry_after=2)
        result, receipts, send, sleep = self.broadcast([error, {'message_id': 10}, {'message_id': 11}])
        self.assertEqual(result['failed'], 0)
        self.assertEqual([r['attempts'] for r in receipts], [2, 1])
        self.assertEqual(send.call_args_list, [mock.call('digest', chat_id='42'),
                                              mock.call('digest', chat_id='42'),
                                              mock.call('digest', chat_id='43')])
        self.assertIn(mock.call(2), sleep.call_args_list)

    def test_blocked_recipient_is_not_retried(self):
        result, receipts, send, _ = self.broadcast([
            monitor.TelegramDeliveryError(403, 'bot_blocked', rejected=True), {'message_id': 11},
        ])
        self.assertIsInstance(result, RuntimeError)
        self.assertEqual(receipts[0]['status'], 'rejected')
        self.assertEqual(receipts[0]['reason'], 'bot_blocked')
        self.assertEqual(send.call_count, 2)

    def test_long_rate_limit_is_recorded_without_blocking_job(self):
        _, receipts, send, sleep = self.broadcast([
            monitor.TelegramDeliveryError(429, 'rate_limited', rejected=True, retry_after=600),
            {'message_id': 11},
        ])
        self.assertEqual(receipts[0]['retry_after'], 600)
        self.assertEqual(send.call_count, 2)
        sleep.assert_called_once_with(0.05)

    def test_repeated_rate_limit_stops_after_three_attempts(self):
        error = monitor.TelegramDeliveryError(429, 'rate_limited', rejected=True, retry_after=1)
        _, receipts, send, _ = self.broadcast([error, error, error, {'message_id': 11}])
        self.assertEqual(receipts[0]['attempts'], 3)
        self.assertEqual(receipts[0]['status'], 'rejected')
        self.assertEqual(send.call_count, 4)

    def test_http_error_body_is_classified_without_exposing_it(self):
        secret = 'SECRET_TOKEN'
        error = urllib.error.HTTPError('https://api.telegram.org/bot'+secret+'/sendMessage',
            403, 'Forbidden', {}, io.BytesIO(json.dumps({
                'ok': False, 'error_code': 403,
                'description': 'Forbidden: bot was blocked by the user ' + secret,
            }).encode()))
        with (mock.patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': secret}, clear=True),
              mock.patch.object(monitor.urllib.request, 'urlopen', side_effect=error)):
            with self.assertRaises(monitor.TelegramDeliveryError) as raised:
                monitor.send_telegram('digest', chat_id='42')
        self.assertTrue(raised.exception.rejected)
        self.assertEqual(raised.exception.reason, 'bot_blocked')
        self.assertNotIn(secret, str(raised.exception))

    def test_gateway_without_telegram_rejection_is_unconfirmed(self):
        error = urllib.error.HTTPError('https://example.invalid', 502, 'gateway', {}, io.BytesIO(b'<html/>'))
        with (mock.patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'secret'}, clear=True),
              mock.patch.object(monitor.urllib.request, 'urlopen', side_effect=error)):
            with self.assertRaises(monitor.TelegramDeliveryError) as raised:
                monitor.send_telegram('digest', chat_id='42')
        self.assertFalse(raised.exception.rejected)
        self.assertEqual(raised.exception.code, 502)

    def test_success_without_message_receipt_is_unconfirmed(self):
        with (mock.patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': 'secret'}, clear=True),
              mock.patch.object(monitor.urllib.request, 'urlopen',
                                return_value=io.BytesIO(b'{"ok": true, "result": {}}'))):
            with self.assertRaises(monitor.TelegramDeliveryError) as raised:
                monitor.send_telegram('digest', chat_id='42')
        self.assertFalse(raised.exception.rejected)
        self.assertEqual(raised.exception.reason, 'missing_message_receipt')


if __name__ == '__main__':
    unittest.main()
