import re
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
MOSCOW = ZoneInfo("Europe/Moscow")


def moscow_times(minute, hours):
    if "-" in hours and "/" in hours:
        interval, step = hours.split("/")
        start, end = interval.split("-")
        hours = ",".join(str(hour) for hour in range(int(start), int(end) + 1, int(step)))
    return [
        datetime(2026, 8, 31, int(hour), int(minute), tzinfo=timezone.utc)
        .astimezone(MOSCOW)
        .strftime("%H:%M")
        for hour in hours.split(",")
    ]


class ScheduleTests(unittest.TestCase):
    def test_github_runs_four_exact_moscow_slots(self):
        workflow = (ROOT / ".github/workflows/monitor.yml").read_text()
        schedules = re.findall(r'^\s+- cron: "([^"]+)"$', workflow, re.M)
        self.assertEqual(len(schedules), 2)
        times = []
        for schedule in schedules:
            minute, hours, day, month, weekday = schedule.split()
            self.assertEqual((day, month, weekday), ("*", "*", "*"))
            times.extend(moscow_times(minute, hours))
        self.assertEqual(sorted(times), ["09:00", "13:00", "15:30", "18:30"])

    def test_github_workflow_has_resource_guards(self):
        workflow = (ROOT / ".github/workflows/monitor.yml").read_text()
        self.assertIn("timeout-minutes: 3", workflow)
        self.assertIn("fetch-depth: 1", workflow)
        self.assertIn("retention-days: 1", workflow)
        self.assertIn("group: masters-monitor-schedule", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertNotRegex(workflow, r"(?m)^\s+(?:push|pull_request):")
        self.assertNotIn("api.github.com", workflow)

    def test_cloud_alternative_has_same_four_digest_times(self):
        deploy = (ROOT / "yandex_cloud/deploy.sh").read_text()
        schedules = re.findall(
            r'^(?:MONITOR|DIGEST)_CRON="\$\{[^}]+:-([^}]+)\}"$', deploy, re.M
        )
        self.assertEqual(len(schedules), 2)
        times = []
        for schedule in schedules:
            minute, hours, day, month, weekday, year = schedule.split()
            self.assertEqual((day, month, weekday, year), ("?", "*", "*", "*"))
            times.extend(moscow_times(minute, hours))
        self.assertEqual(sorted(times), ["09:00", "13:00", "15:30", "18:30"])
        calls = re.findall(r'^upsert_timer .+$', deploy, re.M)
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertTrue(call.endswith("'{\"mode\":\"digest\"}'"), call)


if __name__ == "__main__":
    unittest.main()
