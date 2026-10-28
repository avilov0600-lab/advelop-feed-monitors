"""Read-only deployment status; omit secrets, source URLs and chat identifiers."""
import json
import sqlite3
from pathlib import Path


for tenant in ('asterus', 'rodina', 'plato', 'aston'):
    root = Path('/var/lib') / (tenant + '-feed-monitor')
    result = {'tenant': tenant}
    report_path = root / 'latest.json'
    if report_path.exists():
        report = json.loads(report_path.read_text())
        result.update(checked_at=report.get('checked_at'), status=report.get('overall_status'))
        # Counts are safe to inspect; raw URLs and HTTP exception details are not.
        result['report_sections'] = list(report.keys())
    database = root / 'subscribers.sqlite3'
    if database.exists():
        with sqlite3.connect('file:' + str(database) + '?mode=ro', uri=True) as connection:
            result['active_subscribers'] = connection.execute(
                'SELECT COUNT(*) FROM subscribers WHERE tenant=? AND active=1', (tenant,)
            ).fetchone()[0]
            latest = connection.execute(
                'SELECT checked_at, length(message) FROM latest WHERE tenant=?', (tenant,)
            ).fetchone()
            result['saved_message'] = {'checked_at': latest[0], 'length': latest[1]} if latest else None
    print(json.dumps(result, ensure_ascii=False))
