import contextlib
import importlib.util
import io
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import telegram_delivery as delivery

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('delivery_admin_tests_target', ROOT / 'scripts/delivery_admin.py')
admin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admin)

STAMP = '2026-11-04T09:00:00+03:00'


class AdminTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.ledger = delivery.Ledger(Path(temp.name) / 'delivery.sqlite3')
        self.registry = Path(temp.name) / 'registry.sqlite3'
        with sqlite3.connect(self.registry) as con:
            con.executescript('CREATE TABLE subscribers(tenant,chat_id,active); CREATE TABLE latest(tenant,checked_at);')
            con.executemany('INSERT INTO subscribers VALUES(?,?,1)', [('dar','101'),('dar','102')])
            con.execute('INSERT INTO latest VALUES(?,?)', ('dar',STAMP))
        self.key = None
        for chat, receipt in [('101', {'state':'accepted','reason':'accepted','message_id':8}),
                              ('102', {'state':'rejected','reason':'chat_not_found','code':400})]:
            claim = self.ledger.claim('dar', 'report', STAMP, chat)
            self.key = claim['report_key']
            self.ledger.finish('dar', self.key, chat, claim['claim'], receipt)
        self.sender = Mock(return_value={'state':'accepted','reason':'accepted','message_id':9})
        for context in (patch.object(admin, 'paths', return_value=(self.ledger.path,self.registry)),
                        patch.object(admin.os, 'geteuid', return_value=0),
                        patch.object(admin, 'token_for', return_value='PRIVATE_TOKEN'),
                        patch.object(admin, 'send_once', self.sender),
                        patch.dict(sys.modules, {'transport':types.SimpleNamespace(urlopen=Mock())})):
            context.start()
            self.addCleanup(context.stop)

    def run_cli(self, *args):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['delivery_admin.py', 'dar', *args]), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            admin.main()
        return output.getvalue()

    def test_preview_is_read_only_and_does_not_show_chat_ids(self):
        output = self.run_cli('status')
        self.sender.assert_not_called()
        self.assertNotIn('chat_id', output)
        self.assertNotIn('PRIVATE_TOKEN', output)
        self.run_cli('retry', '--report', self.key, '--recipient', delivery.recipient_ref('102'))
        self.sender.assert_not_called()

    def test_targeted_retry_leaves_successful_subscriber_alone(self):
        self.run_cli('retry', '--report', self.key, '--recipient', delivery.recipient_ref('102'), '--send')
        self.sender.assert_called_once()
        self.assertEqual(self.sender.call_args.args[1], '102')
        self.assertTrue(all(row['state'] == 'accepted' for row in self.ledger.status('dar')[1]))

    def test_successful_inactive_old_and_unspecific_retries_are_refused(self):
        for args in [('retry','--send'),
                     ('retry','--send','--report',self.key,'--recipient',delivery.recipient_ref('101'))]:
            with self.assertRaises(SystemExit):
                self.run_cli(*args)
        with sqlite3.connect(self.registry) as con:
            con.execute('UPDATE subscribers SET active=0 WHERE chat_id=?', ('102',))
        with self.assertRaises(SystemExit):
            self.run_cli('retry','--send','--report',self.key,'--recipient',delivery.recipient_ref('102'))
        with sqlite3.connect(self.registry) as con:
            con.execute('UPDATE subscribers SET active=1')
            con.execute('UPDATE latest SET checked_at=?', ('2026-11-04T13:00:00+03:00',))
        with self.assertRaises(SystemExit):
            self.run_cli('retry','--send','--report',self.key,'--recipient',delivery.recipient_ref('102'))
        self.sender.assert_not_called()

    def test_unconfirmed_requires_personal_confirmation_and_quiet_period(self):
        with self.ledger.connect() as con:
            con.execute("UPDATE deliveries SET state='unconfirmed',reason='network' WHERE chat_id='102'")
        args = ('retry','--send','--report',self.key,'--recipient',delivery.recipient_ref('102'))
        for flags in [(), ('--confirmed-missing',)]:
            with self.assertRaises(SystemExit):
                self.run_cli(*args, *flags)
        self.sender.assert_not_called()
        with self.ledger.connect() as con:
            con.execute("UPDATE deliveries SET updated_at=updated_at-601 WHERE chat_id='102'")
        self.run_cli(*args, '--confirmed-missing')
        self.sender.assert_called_once()
        with self.ledger.connect() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM delivery_events WHERE reason='operator_confirmed_missing'").fetchone()[0], 1)

    def test_diagnostics_are_not_available_to_non_root(self):
        with patch.object(admin.os, 'geteuid', return_value=1000), self.assertRaises(SystemExit):
            self.run_cli('status','--names')
        self.sender.assert_not_called()


if __name__ == '__main__':
    unittest.main()
