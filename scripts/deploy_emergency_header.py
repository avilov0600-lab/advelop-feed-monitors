"""Guarded formatter/reader rollout. No downloads, broadcasts or subscriber edits."""
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
TENANTS = ('dar', 'ikon', 'asterus', 'rodina', 'plato', 'aston', 'summa',
           'donstroy', 'kortros', 'afi', 'apsis', 'sovcombank')
NEW = {'donstroy', 'kortros', 'afi', 'apsis', 'sovcombank'}
TARGETS = (
    ('concise_reports.py', '/opt/concise_reports.py',
     '5889880d7963639fab4056335b01ce9db16138b4828d0e0b1b7297809907a5e0'),
    ('concise_reports.py', '/opt/advelop-client-onboarding/concise_reports.py',
     '5889880d7963639fab4056335b01ce9db16138b4828d0e0b1b7297809907a5e0'),
    ('site-watch/watch.py', '/opt/advelop-site-watch/watch.py',
     'a8790d766f8661334a59603aa06c82044c3e9d1e0067327096303d41cecff3e0'),
)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def probe(tenant):
    """Use the installed tenant adapter with only the proposed formatter replaced."""
    os.environ['MONITOR_TENANT'] = tenant
    prefix = Path('/opt/advelop-client-onboarding') if tenant in NEW else Path('/opt')
    folder = prefix / ((tenant + '-feed-monitor') if tenant in {'dar', 'ikon'} else 'developer-feed-monitor')
    spec = importlib.util.spec_from_file_location('concise_reports', ROOT / 'concise_reports.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules['concise_reports'] = module
    spec.loader.exec_module(module)
    sys.path[:0] = [str(folder), str(prefix)]
    import monitor
    path = Path('/var/lib/' + tenant + '-feed-monitor/latest.json')
    raw = path.read_bytes()
    report = json.loads(raw)
    message = monitor.format_telegram(report)
    assert path.read_bytes() == raw, 'Formatter modified saved data'
    print(json.dumps({'tenant': tenant, 'header': message.splitlines()[0],
                      'checked_at': report.get('checked_at'),
                      'utf16_length': len(message.encode('utf-16-le')) // 2,
                      'report_sha256': digest(raw)}, ensure_ascii=False))


def main(apply=False):
    assert os.geteuid() == 0, 'Root required for installed source verification'
    prepared = []
    for source, destination, before in TARGETS:
        target = Path(destination)
        assert target.is_file() and not target.is_symlink(), 'Unexpected target: ' + destination
        old, new = target.read_bytes(), (ROOT / source).read_bytes()
        assert digest(old) in {before, digest(new)}, 'Source drift: ' + destination
        ast.parse(new)
        prepared.append((target, old, new))
    for tenant in TENANTS:
        result = subprocess.run([sys.executable, __file__, '--probe', tenant],
                                check=True, text=True, capture_output=True)
        print(result.stdout.strip(), flush=True)
    print('EMERGENCY_HEADER_PREFLIGHT_OK', flush=True)
    if not apply:
        return
    units = ['dar-feed-monitor.service', 'ikon-feed-monitor.service']
    units += ['developer-feed-monitor@' + t + '.service' for t in TENANTS[2:7]]
    units += ['advelop-new-feed-' + t + '.service' for t in TENANTS[7:]]
    for unit in units:
        state = subprocess.check_output(['systemctl', 'show', unit, '-p', 'ActiveState', '--value'], text=True).strip()
        assert state not in {'active', 'activating', 'deactivating'}, 'Scheduled check running: ' + unit
    # Recheck after the previews before writing any source files.
    for target, old, _ in prepared:
        assert target.read_bytes() == old, 'Concurrent source edit: ' + str(target)
    backup = Path('/var/backups/advelop-emergency-header-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))
    backup.mkdir(mode=0o700, exist_ok=False)
    for target, _, _ in prepared:
        path = backup / str(target).lstrip('/')
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, path)
    try:
        for target, _, new in prepared:
            pending = target.with_suffix('.emergency-pending')
            with pending.open('xb') as stream:
                stream.write(new)
            shutil.copymode(target, pending)
            pending.replace(target)
        subprocess.run(['systemctl', 'restart', 'advelop-site-watch.service'], check=True)
        subprocess.run(['systemctl', 'is-active', '--quiet', 'advelop-site-watch.service'], check=True)
    except Exception:
        for target, _, _ in prepared:
            shutil.copy2(backup / str(target).lstrip('/'), target)
        subprocess.run(['systemctl', 'restart', 'advelop-site-watch.service'], check=False)
        raise
    for target, _, new in prepared:
        assert digest(target.read_bytes()) == digest(new)
        print('VERIFIED', str(target), digest(new), flush=True)
    print('EMERGENCY_HEADER_INSTALLED', 'backup=' + str(backup), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--probe', choices=TENANTS)
    args = parser.parse_args()
    probe(args.probe) if args.probe else main(args.apply)
