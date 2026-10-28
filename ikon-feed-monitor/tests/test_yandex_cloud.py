import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import monitor
from yandex_cloud import index


class WebhookTests(unittest.TestCase):
    @mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"})
    def test_sync_and_start_replay_without_network(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(index, "STATE_DIR", Path(directory)):
            sync = index.handler(
                {
                    "httpMethod": "POST",
                    "headers": {"X-IKON-Bot-Token": "test-token"},
                    "body": json.dumps({"action": "sync_latest", "message": "🟢 IKON — отчёт", "checked_at": "2026-09-03T12:46:29+03:00"}),
                },
                None,
            )
            event = {
                "httpMethod": "POST",
                "headers": {"X-Telegram-Bot-Api-Secret-Token": index.webhook_secret("test-token")},
                "body": json.dumps({"update_id": 1, "message": {"text": "/start", "chat": {"id": 42, "type": "private"}}}),
            }
            reply = index.handler(event, None)
            repeated = index.handler(event, None)
        self.assertEqual(sync["statusCode"], 200)
        self.assertEqual(json.loads(reply["body"])["text"], "🟢 IKON — отчёт\n\n✅ Автоматические обновления подключены.")
        self.assertEqual(json.loads(reply["body"])["method"], "sendMessage")
        self.assertFalse(json.loads(repeated["body"])["sent"])

    @mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"})
    def test_old_report_cannot_replace_newer(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(index, "STATE_DIR", Path(directory)):
            def sync(message, timestamp):
                return index.handler({"httpMethod": "POST", "headers": {"X-IKON-Bot-Token": "test-token"},
                    "body": json.dumps({"action": "sync_latest", "message": message, "checked_at": timestamp})}, None)
            self.assertEqual(sync("new", "2026-09-03T13:00:00+03:00")["statusCode"], 200)
            older = sync("old", "2026-09-03T09:46:29Z")
            state = monitor._read_json(Path(directory) / index.LATEST_STATE_NAME)
            self.assertFalse(json.loads(older["body"])["stored"])
            self.assertEqual(state["latest_message"], "new")

    @mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"})
    def test_sync_requires_timezone_aware_timestamp(self):
        for timestamp in (None, "bad", "2026-09-03T10:00:00"):
            response = index.handler({"httpMethod": "POST", "headers": {"X-IKON-Bot-Token": "test-token"},
                "body": json.dumps({"action": "sync_latest", "message": "report", "checked_at": timestamp})}, None)
            self.assertEqual(response["statusCode"], 400)

    @mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"})
    def test_wrong_sync_token_is_forbidden(self):
        response = index.handler(
            {"httpMethod": "POST", "headers": {"X-IKON-Bot-Token": "wrong"}, "body": json.dumps({"action": "sync_latest", "message": "x"})},
            None,
        )
        self.assertEqual(response["statusCode"], 403)

    @mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"})
    def test_wrong_telegram_secret_is_forbidden(self):
        response = index.handler({"httpMethod": "POST", "headers": {}, "body": "{}"}, None)
        self.assertEqual(response["statusCode"], 403)

    @mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"})
    def test_html_mode_survives_sync_and_webhook_reply(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(index, "STATE_DIR", Path(directory)):
            sync = index.handler({"httpMethod": "POST", "headers": {"X-IKON-Bot-Token": "test-token"},
                "body": json.dumps({"action": "sync_latest", "message": '<b>ЖИЛАЯ</b>\n\n<i>Эталон: 1</i>',
                    "checked_at": '2026-09-03T13:00:00+03:00', "parse_mode": 'HTML'})}, None)
            reply = index.handler({"httpMethod": "POST", "headers": {
                "X-Telegram-Bot-Api-Secret-Token": index.webhook_secret('test-token')},
                "body": json.dumps({"update_id": 1, "message": {"text": '/start', "chat": {"id": 42, "type": 'private'}}})}, None)
        self.assertEqual(sync['statusCode'], 200)
        self.assertEqual(json.loads(reply['body'])['parse_mode'], 'HTML')

    @mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"})
    def test_unknown_parse_mode_is_rejected(self):
        reply = index.handler({"httpMethod": 'POST', "headers": {"X-IKON-Bot-Token": 'test-token'},
            "body": json.dumps({"action": 'sync_latest', "message": 'report', 'parse_mode': 'unknown'})}, None)
        self.assertEqual(reply['statusCode'], 400)


if __name__ == "__main__":
    unittest.main()
