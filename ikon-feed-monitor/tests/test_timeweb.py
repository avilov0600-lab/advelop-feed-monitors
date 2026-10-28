import re
import sys
import unittest
from pathlib import Path
from unittest import mock
import tempfile
import monitor

ROOT = Path(__file__).resolve().parents[1]


class TimewebTests(unittest.TestCase):
    def test_exact_four_moscow_slots(self):
        text = (ROOT / "deploy/ikon-feed-monitor.timer").read_text()
        slots = []
        for hours, minute in re.findall(r"OnCalendar=\*-\*-\* ([\d,]+):(\d\d):00 Europe/Moscow", text):
            slots.extend(f"{int(hour):02d}:{minute}" for hour in hours.split(","))
        self.assertEqual(sorted(slots), ["09:00", "13:00", "15:30", "18:30"])
        self.assertIn("Persistent=true", text)

    def test_isolation_ipv4_and_broadcast(self):
        text = (ROOT / "deploy/ikon-feed-monitor.service").read_text()
        for required in ("User=ikon-monitor", "EnvironmentFile=/etc/ikon-feed-monitor.env",
                         "TimeoutStartSec=8min", "ProtectSystem=strict", "UMask=0077",
                         "StateDirectoryMode=0700", "Environment=TELEGRAM_IP_FAMILY=ipv6",
                         "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", "--send-telegram --force-notify"):
            self.assertIn(required, text)
        self.assertNotIn("/opt/dar-", text)

    def test_installer_keeps_existing_registry_and_secrets_off_stdout(self):
        text = (ROOT / "deploy/install.py").read_text()
        self.assertIn("SELECT token_hash FROM credentials WHERE tenant='ikon'", text)
        self.assertIn("os.O_EXCL", text)
        self.assertIn("0o600", text)
        self.assertNotIn("setWebhook", text)
        self.assertNotIn("DROP TABLE", text)
        self.assertIn('urlsplit(relay["RELAY_PUBLIC_BASE"]).path', text)

    def test_one_time_secret_transfer_is_not_left_enabled(self):
        self.assertFalse((ROOT / ".github/workflows/timeweb-migration.yml").exists())
        self.assertFalse((ROOT / "deploy/migration-recipient.pem").exists())

    def test_ipv4_connect_preserves_hostname_for_tls(self):
        with mock.patch.object(monitor.socket, "gethostbyname", return_value="192.0.2.4") as dns, \
                mock.patch.object(monitor.socket, "create_connection") as connect:
            monitor._connect_ipv4(("feed.example", 443), 20)
            dns.assert_called_once_with("feed.example")
            connect.assert_called_once_with(("192.0.2.4", 443), 20)
        connection = monitor._IPv4HTTPSConnection("feed.example")
        self.assertEqual(connection.host, "feed.example")
        self.assertIs(connection._create_connection, monitor._connect_ipv4)

    def test_only_client_xml_uses_ipv4_opener(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(monitor.urllib.request, "build_opener") as builder, \
                mock.patch.object(monitor.urllib.request, "urlopen") as normal:
            for response in (builder.return_value.open.return_value.__enter__.return_value,
                             normal.return_value.__enter__.return_value):
                response.read.return_value = b"<xml/>"
                response.status = 200
            path = Path(directory) / "feed.xml"
            monitor._download("https://feed.example", path, ipv4_only=True)
            builder.return_value.open.assert_called_once()
            normal.assert_not_called()
            monitor._download("https://other.example", path)
            normal.assert_called_once()

    def test_telegram_uses_shared_transport_without_changing_other_downloads(self):
        backend = mock.Mock()
        with mock.patch.dict(sys.modules, {"advelop_intake_transport": backend}), \
                mock.patch.dict(monitor.os.environ, {"TELEGRAM_IP_FAMILY": "ipv6"}), \
                mock.patch.object(monitor.urllib.request, "urlopen") as normal:
            monitor._telegram_urlopen("https://api.telegram.org", timeout=8)
            backend.urlopen.assert_called_once_with("https://api.telegram.org", timeout=8)
            normal.assert_not_called()

