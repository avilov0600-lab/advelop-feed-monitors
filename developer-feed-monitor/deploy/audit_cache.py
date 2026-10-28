import json
import os
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import monitor

cache=Path(sys.argv[1])
if '--download' in sys.argv:monitor.download_sources(cache)
report=monitor.analyze_directory(cache)
summary={'client':report['client'],'status':report['overall_status'],'projects':{},'api':{}}
for category in report['categories'].values():
    for key,p in category['projects'].items():
        summary['projects'][key]={'master':p['master'].get('lot_count'),
            'master_status':p['master'].get('status'),
            'feeds':{s:{'count':f.get('lot_count'),'status':f.get('status'),'reason':f.get('reason')} for s,f in p['feeds'].items()},
            'mismatches':{s:{'missing':len(c.get('missing_from_feed',[])),'extra':len(c.get('extra_in_feed',[])),
                'fields':c.get('field_mismatch_counts')} for s,c in p['master_vs_feeds'].items()},
            'marketing':{s:f.get('marketing_issues') for s,f in p['feeds'].items() if f.get('marketing_issues')}}
    summary['api']=category['api_checks']
print(json.dumps(summary,ensure_ascii=False))
