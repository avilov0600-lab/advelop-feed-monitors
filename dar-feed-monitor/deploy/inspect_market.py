"""Read-only pagination diagnostic. Never emits credentials or account data."""
import argparse
import collections
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import monitor
env=dict(line.split('=',1) for line in Path('/etc/dar-feed-monitor.env').read_text().splitlines() if '=' in line)
auth=monitor._request_json(monitor.AVITO_API_BASE+'/token',data={
    'grant_type':'client_credentials','client_id':env['AVITO_CLIENT_ID_RESIDENTIAL'],
    'client_secret':env['AVITO_CLIENT_SECRET_RESIDENTIAL']})
parser=argparse.ArgumentParser()
parser.add_argument('--per-page',type=int,default=100)
parser.add_argument('--pages',type=int,default=3)
parser.add_argument('--start-page',type=int,default=1)
parser.add_argument('--delay',type=float,default=0)
args=parser.parse_args()
seen=set()
for page in range(args.start_page,args.start_page+args.pages):
    if page > args.start_page and args.delay:
        time.sleep(args.delay)
    query=urllib.parse.urlencode({'status':'active','page':page,'per_page':args.per_page})
    result=monitor._request_json(monitor.AVITO_API_BASE+'/core/v1/items?'+query,token=auth['access_token'])
    rows=result.get('resources') or result.get('items') or []
    ids={str(row.get('id')) for row in rows}
    # IDs are deliberately not printed; count only is enough for pagination diagnosis.
    print(json.dumps({'page':page,'count':len(rows),'new_ids':len(ids-seen),
        'root_keys':list(result),'metadata':{key:result[key] for key in ('meta','pagination','total','count') if key in result},
        'item_keys':list(rows[0]) if rows else [],'statuses':dict(collections.Counter(row.get('status') for row in rows))},ensure_ascii=False))
    seen |= ids
print(json.dumps({'unique_ids_total':len(seen)},ensure_ascii=False))
