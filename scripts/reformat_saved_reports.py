"""Operational formatter replay: no source downloads and no writes to report JSON."""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys

TENANTS = ('dar','ikon','asterus','rodina','plato','aston','summa',
           'donstroy','kortros','afi','apsis','sovcombank')
NEW = {'donstroy','kortros','afi','apsis','sovcombank'}


def replay(tenant, send=False, sync=False, owner_preview=False):
    assert tenant in TENANTS
    os.environ['MONITOR_TENANT'] = tenant
    prefix = Path('/opt/advelop-client-onboarding') if tenant in NEW else Path('/opt')
    folder = prefix/((tenant+'-feed-monitor') if tenant in {'dar','ikon'} else 'developer-feed-monitor')
    sys.path[:0] = [str(folder), str(prefix)]
    m = importlib.import_module('monitor')
    if tenant not in {'dar','ikon'}:
        importlib.import_module('run')  # Preserve the existing local registry transport.
    path = Path('/var/lib/'+tenant+'-feed-monitor/latest.json')
    original = path.read_bytes()
    report = json.loads(original)
    old = m.format_detailed_telegram(report)
    message = m.format_telegram(report)
    assert message and len(message.encode('utf-16-le'))//2 <= 4096
    if send:
        m.broadcast_telegram(message, report['checked_at'])
    elif sync:
        result = m._telegram_state_request({'action': 'sync_latest', 'message': message,
                                            'checked_at': report['checked_at'], 'parse_mode': 'HTML'})
        assert result.get('stored') is True, 'Newer cached report exists; replay skipped'
    if owner_preview:
        owner = os.environ.get('TELEGRAM_CHAT_ID')
        assert owner, 'Owner recipient is not configured'
        m.send_telegram(message, chat_id=owner)
    assert path.read_bytes() == original, 'Saved feed results changed during presentation replay'
    print('CONCISE_REPORT', tenant, 'old='+str(len(old)), 'new='+str(len(message)),
          'saved_json_unchanged=True', 'sha256='+hashlib.sha256(original).hexdigest(), flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tenant', choices=TENANTS)
    parser.add_argument('--send', action='store_true')
    parser.add_argument('--sync', action='store_true')
    parser.add_argument('--owner-preview', action='store_true')
    args=parser.parse_args()
    replay(args.tenant,args.send,args.sync,args.owner_preview)
