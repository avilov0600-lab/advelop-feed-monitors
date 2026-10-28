"""Four permanent daily digests. Legacy intensive schedules are rejected."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path


NORMAL_CRONS = {"0 6,10 * * *", "30 12,15 * * *"}


def should_run(event_name: str, schedule: str, started_at: str, now: dt.datetime) -> bool:
    if event_name == "workflow_dispatch":
        return True
    if event_name != "schedule":
        return False
    return schedule in NORMAL_CRONS


def main() -> None:
    allowed = should_run(
        os.environ.get("EVENT_NAME", ""),
        os.environ.get("EVENT_SCHEDULE", ""),
        os.environ.get("IKON_INTENSIVE_STARTED_AT", ""),
        dt.datetime.now(dt.timezone.utc),
    )
    value = f"run={'true' if allowed else 'false'}\n"
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write(value)
    print(value.strip())


if __name__ == "__main__":
    main()
