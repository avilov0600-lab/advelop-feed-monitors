import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
with patch.object(sys, 'path', [str(ROOT / 'scripts'), *sys.path]):
    spec = importlib.util.spec_from_file_location('delivery_deploy_test', ROOT / 'scripts/deploy_delivery_journal.py')
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)


class DeploymentTests(unittest.TestCase):
    def test_hook_preserves_each_engine_and_is_idempotent(self):
        for tenant, path in [('dar','dar-feed-monitor'), ('ikon','ikon-feed-monitor'),
                             ('developer','developer-feed-monitor')]:
            source = (ROOT / path / 'monitor.py').read_text()
            addition = ("from telegram_delivery import install_monitor as _install_delivery_journal\n"
                        f"_install_delivery_journal(sys.modules[__name__], '{tenant}')\n\n")
            self.assertEqual(deploy.hook(source.replace(addition, ''), tenant), source)
            self.assertEqual(deploy.hook(source, tenant), source)

    def test_unknown_layout_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'Unrecognized'):
            deploy.hook('print("some other application")', 'dar')

    def test_existing_pending_file_is_not_removed_or_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'module.py'
            pending = target.with_name('module.py.delivery-pending')
            target.write_text('old')
            pending.write_text('another deployment')
            with self.assertRaises(FileExistsError):
                deploy.atomic_write(target, b'new')
            self.assertEqual(target.read_text(), 'old')
            self.assertEqual(pending.read_text(), 'another deployment')


if __name__ == '__main__':
    unittest.main()
