import datetime as dt
import re
import unittest
from pathlib import Path
import schedule_policy

ROOT = Path(__file__).resolve().parents[1]

class WorkflowTests(unittest.TestCase):
    def test_github_has_no_automatic_schedule_after_timeweb_migration(self):
        workflow=(ROOT/'.github/workflows/monitor.yml').read_text()
        crons=re.findall(r'^\s+- cron: "([^"]+)"$',workflow,re.M)
        self.assertEqual(crons, [])
        self.assertNotRegex(workflow, r'(?m)^  schedule:')
        self.assertIn('workflow_dispatch:', workflow)

    def test_resource_guards_unchanged(self):
        text=(ROOT/'.github/workflows/monitor.yml').read_text()
        for expected in ('timeout-minutes: 8','fetch-depth: 1','retention-days: 1','group: ikon-monitor-schedule','cancel-in-progress: false'):
            self.assertIn(expected,text)
        self.assertNotRegex(text,r'(?m)^\s+(?:push|pull_request):')

    def test_no_github_api_or_commercial_secrets(self):
        self.assertNotIn('api.github.com',(ROOT/'monitor.py').read_text())
        text=(ROOT/'.github/workflows/monitor.yml').read_text()
        self.assertNotIn('COMMERCIAL',text)
        for secret in ('CIAN_API_KEY_RESIDENTIAL','AVITO_CLIENT_ID_RESIDENTIAL','AVITO_CLIENT_SECRET_RESIDENTIAL'):
            self.assertIn(secret,text)

    def test_current_schedules_are_permanent(self):
        now=dt.datetime(2026,10,12,tzinfo=dt.timezone.utc)
        for cron in schedule_policy.NORMAL_CRONS:
            self.assertTrue(schedule_policy.should_run('schedule',cron,'',now))
        self.assertTrue(schedule_policy.should_run('workflow_dispatch','','',now))

    def test_legacy_and_unknown_schedules_rejected(self):
        now=dt.datetime.now(dt.timezone.utc)
        for cron in ('0 6,10,14 * * *','0 4,8,12,16,18,20 * * *','30 20 * * *','0 1-23/2 * * *',''):
            self.assertFalse(schedule_policy.should_run('schedule',cron,now.isoformat(),now))
        self.assertFalse(schedule_policy.should_run('push','','',now))

if __name__=='__main__': unittest.main()
