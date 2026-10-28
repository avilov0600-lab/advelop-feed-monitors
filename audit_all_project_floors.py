"""Read-only, aggregated audit of isolated Timeweb project caches. No API calls.

Run on Timeweb after deployment. Never print individual lot IDs or credentials.
Source timestamps remain separate from the time this audit is executed.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

CHILD = r'''
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import monitor as m
cache=Path(sys.argv[2])
report=m.analyze_directory(cache)
message=m.format_telegram(report)
previous=m._read_json(cache.parent/'latest.json')
for category,value in report['categories'].items():
 for project,result in value['projects'].items():
  sources={}
  for source,comparison in result['master_vs_feeds'].items():
   state=report.get('source_status',{}).get('feeds',{}).get(category,{}).get(project,{}).get(source,{})
   invalid=None
   if state.get('status')=='OK':
    lots,_=m.FEED_PARSERS[source](m._feed_path(cache,category,project,source),project=project,category=category)
    invalid=sum(l.floor is not None and l.floors_total is not None and l.floor>l.floors_total for l in lots)
   sources[source]={'status':comparison.get('status'),
    'height_groups':comparison.get('floors_total_groups',[]),
    'height_coverage':comparison.get('floors_total_coverage'),
    'apartment_floor_errors':comparison.get('field_mismatch_counts',{}).get('floor',0),
    'floor_above_building':invalid,'source_checked_at':state.get('checked_at')}
  print(json.dumps({'tenant':sys.argv[3],'project':project,'name':m.PROJECTS[project],
   'master_status':result['master'].get('status'),'master_count':result['master'].get('lot_count'),
   'sources':sources,'cross_height_groups':result.get('cross_feed',{}).get('floors_total_groups',[]),
   'telegram_length':len(message),'previous_report_checked_at':previous.get('checked_at')},ensure_ascii=False))
'''

if __name__=='__main__':
    for tenant in ('dar','ikon','asterus','rodina','plato','aston','summa'):
        app='/opt/'+(tenant+'-feed-monitor' if tenant in {'dar','ikon'} else 'developer-feed-monitor')
        subprocess.run([sys.executable,'-c',CHILD,app,'/var/lib/'+tenant+'-feed-monitor/cache',tenant],
            env={**os.environ,'MONITOR_TENANT':tenant,'PYTHONDONTWRITEBYTECODE':'1'},check=True)
