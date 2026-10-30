"""Prepare a formatter-only patch against the verified October 28 production sources."""
import ast
import base64
import hashlib
import json
from pathlib import Path
import sys
import zlib
from prepare_site_availability_deployment import original, render, update

ROOT = Path(__file__).resolve().parents[1]


def build_plan():
    old_root = render(original('b506c6409e4a014b99340ff4d5a66e04c0d6aee0', 'publication_control.py'))
    old_isolated = original('84d87aa', 'publication_control.py')
    verified = json.loads((ROOT/'verification/site-availability-live-verification-20261028.json').read_text())
    expected = {row['path']: row['sha256'] for row in verified['installed_sources']}
    footer = '''    m.format_detailed_telegram=render
    from concise_reports import render_report
    m.format_telegram=lambda report: render_report(
        report, client_name, m.PROJECTS, m.FEED_LABELS,
        field_labels=getattr(m, 'FIELD_LABELS', {}), data=getattr(m, 'DATA', {}))
'''
    updates = []
    for prefix, source in (('/opt', old_root), ('/opt/advelop-client-onboarding', old_isolated)):
        module = (ROOT/'concise_reports.py').read_text()
        ast.parse(module)
        updates.append(update(prefix+'/concise_reports.py', None, module))
        target = prefix+'/publication_control.py'
        assert hashlib.sha256(source.encode()).hexdigest() == expected[target]
        assert source.count('    m.format_telegram=render\n') == 1
        updated = source.replace('    m.format_telegram=render\n', footer)
        updates.append(update(target, source, updated))
    return {'updates': updates, 'scope': 'presentation only; no source requests, schedule or subscriber changes'}


if __name__ == '__main__':
    target = Path(sys.argv[1]); target.mkdir(mode=0o700, parents=True, exist_ok=True)
    raw = json.dumps(build_plan(), ensure_ascii=False).encode()
    (target/'plan.json').write_bytes(raw)
    print('PLAN', len(raw), hashlib.sha256(raw).hexdigest())
