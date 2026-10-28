import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import monitor
from yandex_cloud import index


@mock.patch.dict(os.environ, {
    "TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "41",
    "TELEGRAM_LATEST_MESSAGE_URL": "https://state.example",
}, clear=True)
class SubscriptionTests(unittest.TestCase):
    def start(self, chat_id, update_id=1, **changes):
        update = {"update_id": update_id, "message": {"text": "/start",
            "chat": {"id": chat_id, "type": "private"},
            "from": {"first_name": "Private name not to be stored"}}}
        update.update(changes)
        return index.handler({"httpMethod": "POST", "headers": {
            "X-Telegram-Bot-Api-Secret-Token": index.webhook_secret("test-token")},
            "body": json.dumps(update)}, None)

    def state_request(self, payload):
        result = index.handler({"httpMethod": "POST",
            "headers": {"X-IKON-Bot-Token": "test-token"},
            "body": json.dumps(payload)}, None)
        self.assertEqual(result["statusCode"], 200)
        return json.loads(result["body"])

    def test_each_start_persists_subscription_and_broadcast_includes_owner_once(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(index, "STATE_DIR", Path(directory)):
            for chat_id in (42, 43):
                reply = self.start(chat_id)
                self.assertIn("подключены", json.loads(reply["body"])["text"])
                self.start(chat_id)  # Telegram retries must not create duplicates.
            paths = list((Path(directory) / index.SUBSCRIBERS_DIR_NAME).glob("*.json"))
            self.assertEqual(len(paths), 2)
            self.assertTrue(all("Private name" not in path.read_text() for path in paths))
            with mock.patch.object(monitor, "_telegram_state_request", side_effect=self.state_request), \
                    mock.patch.object(monitor, "send_telegram") as send, mock.patch("time.sleep"):
                result = monitor.broadcast_telegram("<b>Отчёт</b>", "2026-09-03T19:00:00+03:00")
            self.assertEqual(result["delivered"], 3)
            self.assertEqual(send.call_args_list, [mock.call("<b>Отчёт</b>", chat_id=str(chat_id))
                                                 for chat_id in (41, 42, 43)])
            # A fresh read from disk (no in-memory registry) still sees all three.
            self.assertEqual(len(index._recipients()), 3)

    def test_throttled_start_still_resubscribes_without_another_reply(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(index, "STATE_DIR", Path(directory)):
            self.start(42, 1)
            self.state_request({"action": "mark_unreachable", "recipients": [{"chat_id": "42", "revision": 1}]})
            self.assertEqual(index._recipients(), [])
            repeated = self.start(42, 2)
            self.assertFalse(json.loads(repeated["body"])["sent"])
            self.assertEqual(index._recipients(), [{"chat_id": "42", "revision": 2}])

    def test_blocked_chat_does_not_prevent_other_deliveries_or_reappear_as_owner(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(index, "STATE_DIR", Path(directory)):
            self.start(42)
            blocked = urllib.error.HTTPError("https://example.test", 403, "Forbidden", {}, None)
            with mock.patch.object(monitor, "_telegram_state_request", side_effect=self.state_request), \
                    mock.patch.object(monitor, "send_telegram", side_effect=[blocked, {}]) as send, \
                    mock.patch("time.sleep"):
                summary = monitor.broadcast_telegram("report", "2026-09-03T19:00:00+03:00")
            self.assertEqual(send.call_count, 2)
            self.assertEqual(summary, {"subscribers": 2, "delivered": 1, "blocked": 1, "failed": 0})
            self.assertEqual(index._recipients(), [{"chat_id": "42", "revision": 1}])
            self.state_request({"action": "sync_latest", "message": "new", "checked_at": "2026-09-03T20:00:00+03:00",
                                "owner_chat_id": "41"})
            self.assertEqual(index._recipients(), [{"chat_id": "42", "revision": 1}])

    def test_late_block_error_does_not_disable_a_newer_start(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(index, "STATE_DIR", Path(directory)):
            self.start(42, 1)
            self.start(42, 2)
            result = self.state_request({"action": "mark_unreachable", "recipients": [{"chat_id": "42", "revision": 1}]})
            self.assertEqual(result["disabled"], 0)
            self.assertEqual(len(index._recipients()), 1)

    def test_network_failure_is_reported_after_attempting_other_recipients(self):
        with mock.patch.object(monitor, "sync_latest_message", return_value={"ok": True, "stored": True,
                "recipients": [{"chat_id": "41", "revision": 1}, {"chat_id": "42", "revision": 1}]}), \
                mock.patch.object(monitor, "send_telegram", side_effect=[urllib.error.URLError("offline"), {}]) as send, \
                mock.patch("time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "1 подписчикам"):
                monitor.broadcast_telegram("report", "2026-09-03T19:00:00+03:00")
            self.assertEqual(send.call_count, 2)

    def test_old_report_is_not_broadcast(self):
        with mock.patch.object(monitor, "sync_latest_message", return_value={"ok": True, "stored": False, "reason": "older_report"}), \
                mock.patch.object(monitor, "send_telegram") as send:
            result = monitor.broadcast_telegram("old", "2026-09-03T19:00:00+03:00")
            self.assertTrue(result["skipped_older_report"])
            send.assert_not_called()

    def test_registry_errors_fall_back_to_owner(self):
        for response in ({"ok": True}, {"recipients": [{"chat_id": "../bad", "revision": 1}]}):
            with mock.patch.object(monitor, "sync_latest_message", return_value=response), \
                    mock.patch.object(monitor, "send_telegram") as send, \
                    mock.patch.dict("os.environ", {"TELEGRAM_CHAT_ID": "41"}):
                result = monitor.broadcast_telegram("report", "2026-09-03T19:00:00+03:00")
                send.assert_called_once_with("report", chat_id="41")
                self.assertEqual(result["delivered"], 1)

    def test_groups_malformed_updates_and_other_bot_commands_do_not_subscribe(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(index, "STATE_DIR", Path(directory)):
            for update in (
                {"message": {"text": "/start", "chat": {"id": -1, "type": "group"}}},
                {"update_id": None},
                {"message": {"text": "/start@advelopmastersbot", "chat": {"id": 42, "type": "private"}}},
            ):
                reply = self.start(42, **update)
                self.assertFalse(json.loads(reply["body"])["sent"])
            self.assertEqual(index._recipients(), [])

    def test_subscriber_admin_actions_require_the_raw_bot_token(self):
        for action in ("subscription_status", "mark_unreachable"):
            reply = index.handler({"httpMethod": "POST", "headers": {
                "X-Telegram-Bot-Api-Secret-Token": index.webhook_secret("test-token")},
                "body": json.dumps({"action": action})}, None)
            self.assertEqual(reply["statusCode"], 403)


if __name__ == "__main__":
    unittest.main()
