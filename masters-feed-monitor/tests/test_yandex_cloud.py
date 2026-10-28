import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yandex_cloud import index


def sample_report():
    return {
        "checked_at": "2026-08-19T21:00:00+03:00",
        "master": {"lot_count": 155},
        "site": {"lot_count": 156},
        "feeds": {"cian": {"lot_count": 155}},
    }


class YandexCloudHandlerTests(unittest.TestCase):
    @mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}, clear=False)
    def test_start_webhook_immediately_sends_cached_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            state.mkdir()
            index.monitor.save_latest_telegram_message(
                state / index.LATEST_TELEGRAM_STATE_NAME,
                "последний дайджест",
                "checked",
            )
            event = {
                "httpMethod": "POST",
                "headers": {
                    "X-Telegram-Bot-Api-Secret-Token": index._webhook_secret(
                        "test-token"
                    )
                },
                "body": json.dumps(
                    {
                        "update_id": 7,
                        "message": {
                            "text": "/start",
                            "chat": {"id": 42, "type": "private"},
                        },
                    }
                ),
            }
            with mock.patch.object(index, "STATE_DIR", state):
                response = index.handler(event, None)
                repeated = index.handler(event, None)
                subscribers = json.loads(
                    (state / index.SUBSCRIBERS_DIR_NAME / "42.json").read_text(encoding="utf-8")
                )

        self.assertEqual(response["statusCode"], 200)
        body = json.loads(response["body"])
        self.assertEqual(body["method"], "sendMessage")
        self.assertEqual(body["chat_id"], "42")
        self.assertEqual(
            body["text"],
            "последний дайджест\n\n✅ Автоматические обновления подключены.",
        )
        self.assertEqual(json.loads(repeated["body"]), {"ok": True, "sent": False})
        self.assertTrue(subscribers["active"])

    @mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}, clear=False)
    def test_authorized_http_sync_updates_cached_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            event = {
                "httpMethod": "POST",
                "headers": {"X-Masters-Bot-Token": "test-token"},
                "body": json.dumps(
                    {
                        "action": "sync_latest",
                        "message": "новый дайджест",
                        "checked_at": "checked",
                    }
                ),
            }
            with mock.patch.object(index, "STATE_DIR", state):
                response = index.handler(event, None)
                payload = json.loads(
                    (state / index.LATEST_TELEGRAM_STATE_NAME).read_text()
                )

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(payload["latest_message"], "новый дайджест")
        response_payload = json.loads(response["body"])
        self.assertEqual(response_payload["recipients"], [])
        self.assertEqual(response_payload["subscriber_count"], 0)

    @mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}, clear=False)
    def test_stop_unsubscribes_and_start_reactivates_without_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            state.mkdir()
            index.monitor.save_latest_telegram_message(
                state / index.LATEST_TELEGRAM_STATE_NAME, "дайджест", "checked"
            )
            secret = index._webhook_secret("test-token")

            def event(update_id, command):
                return {"httpMethod": "POST", "headers": {
                    "X-Telegram-Bot-Api-Secret-Token": secret}, "body": json.dumps({
                    "update_id": update_id,
                    "message": {"text": command, "chat": {"id": 42, "type": "private"}},
                })}

            with mock.patch.object(index, "STATE_DIR", state):
                stopped = index.handler(event(8, "/stop"), None)
                repeated_old_start = index.handler(event(7, "/start"), None)
                started = index.handler(event(9, "/start"), None)
                subscribers = index._active_subscribers()

        self.assertIn("отключены", json.loads(stopped["body"])["text"])
        self.assertIn("отключены", json.loads(repeated_old_start["body"])["text"])
        self.assertIn("подключены", json.loads(started["body"])["text"])
        self.assertEqual(subscribers, ["42"])

    @mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "41"})
    def test_two_colleagues_are_saved_and_returned_with_owner_only_to_authorized_sync(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(index, "STATE_DIR", Path(directory)):
            for number in (42, 43):
                event = {"httpMethod": "POST", "headers": {
                    "X-Telegram-Bot-Api-Secret-Token": index._webhook_secret("test-token")},
                    "body": json.dumps({"update_id": number, "message": {
                        "text": "/start", "chat": {"id": number, "type": "private"},
                        "from": {"first_name": "not-stored"},
                    }})}
                started = json.loads(index.handler(event, None)["body"])
                self.assertIn("подключены", started["text"])
            persisted = "".join(
                path.read_text() for path in (Path(directory) / index.SUBSCRIBERS_DIR_NAME).glob("*.json")
            )
            self.assertNotIn("not-stored", persisted)
            sync = {"httpMethod": "POST", "headers": {"X-Masters-Bot-Token": "test-token"},
                "body": json.dumps({"action": "sync_latest", "message": "report", "checked_at": "checked"})}
            result = json.loads(index.handler(sync, None)["body"])
            self.assertEqual(result["recipients"], ["41", "42", "43"])
            sync["headers"] = {"X-Telegram-Bot-Api-Secret-Token": index._webhook_secret("test-token")}
            self.assertEqual(index.handler(sync, None)["statusCode"], 403)

    @mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-token"}, clear=True)
    def test_forged_group_and_missing_id_updates_do_not_register(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(index, "STATE_DIR", Path(directory)):
            update = {"update_id": 1, "message": {"text": "/start", "chat": {"id": 42, "type": "private"}}}
            event = {"httpMethod": "POST", "headers": {}, "body": json.dumps(update)}
            self.assertEqual(index.handler(event, None)["statusCode"], 403)
            event["headers"] = {"X-Telegram-Bot-Api-Secret-Token": index._webhook_secret("test-token")}
            update["message"]["chat"]["type"] = "group"
            event["body"] = json.dumps(update)
            self.assertFalse(json.loads(index.handler(event, None)["body"])["sent"])
            update["message"]["chat"]["type"] = "private"
            del update["update_id"]
            event["body"] = json.dumps(update)
            self.assertFalse(json.loads(index.handler(event, None)["body"])["sent"])
            self.assertFalse((Path(directory) / index.SUBSCRIBERS_DIR_NAME).exists())

    @mock.patch.dict(os.environ, {"TELEGRAM_CHAT_ID": "42"}, clear=True)
    def test_owner_can_stop_and_restart_and_registry_survives_function_restart(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(index, "STATE_DIR", Path(directory)):
            self.assertEqual(index._active_subscribers(), ["42"])
            index._set_subscription("42", active=False, update_id=7)
            self.assertEqual(index._active_subscribers(), [])
            index._set_subscription("42", active=True, update_id=8)
            self.assertEqual(index._active_subscribers(), ["42"])

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_corrupt_registry_is_not_silently_overwritten(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(index, "STATE_DIR", Path(directory)):
            path = Path(directory) / index.SUBSCRIBERS_DIR_NAME / "42.json"
            path.parent.mkdir()
            path.write_text("{broken")
            with self.assertRaisesRegex(RuntimeError, "damaged"):
                index._set_subscription("42", active=True, update_id=7)
            self.assertEqual(path.read_text(), "{broken")

    @mock.patch.dict(
        os.environ,
        {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "42"},
        clear=False,
    )
    def test_digest_trigger_sends_even_when_alert_is_suppressed(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache"
            state = Path(directory) / "state"
            with (
                mock.patch.object(index, "CACHE_DIR", cache),
                mock.patch.object(index, "STATE_DIR", state),
                mock.patch.object(index.monitor, "download_sources"),
                mock.patch.object(index.monitor, "analyze_directory", return_value=sample_report()),
                mock.patch.object(index.monitor, "format_telegram", return_value="digest"),
                mock.patch.object(index.monitor, "should_notify", return_value=False),
                mock.patch.object(index.monitor, "send_telegram") as send,
            ):
                response = index.handler(
                    {
                        "messages": [
                            {"details": {"payload": json.dumps({"mode": "digest"})}}
                        ]
                    },
                    None,
                )
        send.assert_called_once_with("digest", chat_id="42")
        self.assertEqual(json.loads(response["body"])["reason"], "digest")

    def test_recognizes_direct_and_timer_digest_events(self):
        self.assertTrue(index._is_digest_event({"mode": "digest"}))
        self.assertTrue(
            index._is_digest_event(
                {"messages": [{"details": {"payload": '{"mode":"digest"}'}}]}
            )
        )
        self.assertFalse(index._is_digest_event({"messages": []}))

    def test_extracts_event_mode(self):
        self.assertEqual(index._event_mode({"mode": "diagnose"}), "diagnose")
        self.assertEqual(
            index._event_mode(
                {"messages": [{"details": {"payload": '{"mode":"monitor"}'}}]}
            ),
            "monitor",
        )


if __name__ == "__main__":
    unittest.main()
