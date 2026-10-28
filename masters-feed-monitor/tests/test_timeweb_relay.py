import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "timeweb_relay" / "relay.py"
SPEC = importlib.util.spec_from_file_location("timeweb_relay", MODULE_PATH)
relay = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(relay)

IMPORT_SPEC = importlib.util.spec_from_file_location(
    "import_yandex_state", MODULE_PATH.with_name("import_yandex_state.py")
)
import_yandex_state = importlib.util.module_from_spec(IMPORT_SPEC)
assert IMPORT_SPEC.loader is not None
import sys
sys.modules["relay"] = relay
IMPORT_SPEC.loader.exec_module(import_yandex_state)


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.calls = []

        def telegram_request(token, method, parameters):
            self.calls.append((token, method, parameters))
            return {"ok": True, "result": True}

        self.store = relay.RelayStore(Path(self.temp.name) / "relay.sqlite3")
        self.secrets = {"masters": "m" * 48, "ikon": "i" * 48}
        self.app = relay.RelayApplication(
            self.store,
            "https://app.topsku.pro/advelop-telegram",
            self.secrets,
            telegram_request=telegram_request,
        )
        self.masters_token = "1234567890:" + "A" * 35
        self.ikon_token = "9876543210:" + "B" * 35

    def endpoint(self, tenant):
        return f"/advelop-telegram/v1/{tenant}/{self.secrets[tenant]}"

    def sync(self, tenant, token, **overrides):
        payload = {
            "action": "sync_latest",
            "message": "report",
            "checked_at": "2026-10-12T12:00:00+03:00",
            "owner_chat_id": "10001",
            **overrides,
        }
        header = "X-Masters-Bot-Token" if tenant == "masters" else "X-IKON-Bot-Token"
        return self.app.handle(self.endpoint(tenant), {header: token}, payload)

    def test_first_sync_configures_webhook_and_registers_owner(self):
        status, payload = self.sync("masters", self.masters_token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["recipients"], ["10001"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], "setWebhook")
        self.assertNotIn(self.masters_token, self.store.path.read_bytes().decode("latin1"))

    def test_ikon_recipients_keep_revisions_and_html(self):
        status, payload = self.sync("ikon", self.ikon_token, parse_mode="HTML")
        self.assertEqual(status, 200)
        self.assertEqual(payload["recipients"], [{"chat_id": "10001", "revision": -1}])
        latest = self.store.latest("ikon")
        self.assertEqual(latest["parse_mode"], "HTML")

    def test_start_subscribes_and_replays_latest(self):
        self.sync("masters", self.masters_token)
        secret_header = relay._token_hash(self.masters_token)
        status, payload = self.app.handle(
            self.endpoint("masters"),
            {"X-Telegram-Bot-Api-Secret-Token": secret_header},
            {
                "update_id": 55,
                "message": {
                    "text": "/start",
                    "chat": {"id": 20002, "type": "private"},
                },
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["method"], "sendMessage")
        self.assertIn("report", payload["text"])
        self.assertEqual(len(self.store.subscribers("masters", active_only=True)), 2)

    def test_stop_disables_only_target_subscriber(self):
        self.sync("ikon", self.ikon_token)
        secret_header = relay._token_hash(self.ikon_token)
        self.app.handle(
            self.endpoint("ikon"),
            {"X-Telegram-Bot-Api-Secret-Token": secret_header},
            {
                "update_id": 77,
                "message": {"text": "/stop", "chat": {"id": 10001, "type": "private"}},
            },
        )
        self.assertEqual(self.store.subscribers("ikon", active_only=True), [])

    def test_wrong_path_or_token_is_rejected(self):
        status, _ = self.app.handle(
            "/advelop-telegram/v1/masters/wrong",
            {"X-Masters-Bot-Token": self.masters_token},
            {"action": "subscription_status"},
        )
        self.assertEqual(status, 404)
        self.sync("masters", self.masters_token)
        status, _ = self.sync("masters", "1234567890:" + "Z" * 35)
        self.assertEqual(status, 403)

    def test_older_report_does_not_replace_latest(self):
        self.sync("ikon", self.ikon_token)
        status, payload = self.sync(
            "ikon", self.ikon_token, checked_at="2026-10-11T12:00:00+03:00", message="old"
        )
        self.assertEqual(status, 200)
        self.assertFalse(payload["stored"])
        self.assertEqual(self.store.latest("ikon")["message"], "report")

    def test_yandex_state_import_preserves_subscribers_and_latest_message(self):
        source = Path(self.temp.name) / "old-masters"
        source.mkdir()
        (source / "latest-telegram.json").write_text(
            json.dumps({"latest_message": "cached", "latest_checked_at": "checked"}),
            encoding="utf-8",
        )
        (source / "12345.json").write_text(
            json.dumps({"active": True, "last_update_id": 9, "updated_at": 1}),
            encoding="utf-8",
        )
        result = import_yandex_state.import_tenant(self.store, "masters", source)
        self.assertEqual(result, {"subscribers": 1, "latest": True})
        self.assertEqual(self.store.latest("masters")["message"], "cached")
        self.assertEqual(
            self.store.subscribers("masters", active_only=True)[0]["chat_id"], "12345"
        )


if __name__ == "__main__":
    unittest.main()
