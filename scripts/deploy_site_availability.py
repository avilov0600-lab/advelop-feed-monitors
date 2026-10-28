"""Root-only atomic source deployment; subscriptions, secrets and timers are untouched."""
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

BACKUP = Path('/var/backups/advelop-site-availability-20261028')
ALLOWED = {
    '/opt/site_availability.py', '/opt/site_availability_integration.py', '/opt/publication_control.py',
    '/opt/dar-feed-monitor/monitor.py', '/opt/ikon-feed-monitor/monitor.py',
    '/opt/developer-feed-monitor/monitor.py',
    '/opt/advelop-client-onboarding/site_availability.py',
    '/opt/advelop-client-onboarding/site_availability_integration.py',
    '/opt/advelop-client-onboarding/publication_control.py',
    '/opt/advelop-client-onboarding/developer-feed-monitor/monitor.py',
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def prepare(plan):
    updates = plan.get('updates', [])
    if len(updates) != len(ALLOWED) or {row['path'] for row in updates} != ALLOWED:
        raise ValueError('Unexpected deployment scope')
    prepared = []
    for row in updates:
        path = Path(row['path'])
        if path.is_symlink() or not path.parent.is_dir():
            raise ValueError('Unexpected source path')
        if row['before_sha256'] is None:
            if path.exists():
                raise ValueError('New module already exists; review before updating')
            text = row['content']
        else:
            data = path.read_bytes()
            if digest(data) != row['before_sha256']:
                raise ValueError('Source drift: ' + str(path))
            text = data.decode()
            for start, end, replacement in reversed(row['edits']):
                text = text[:start] + replacement + text[end:]
        if digest(text.encode()) != row['after_sha256']:
            raise ValueError('Invalid prepared source')
        ast.parse(text, filename=str(path))
        prepared.append((row, text))
    return prepared


def main(plan_path, verify_only=False):
    if os.geteuid() != 0:
        raise ValueError('Root required')
    plan = json.loads(Path(plan_path).read_text())
    prepared = prepare(plan)
    if verify_only:
        print('AVAILABILITY_PREFLIGHT_OK', len(prepared), 'source files; no changes')
        return
    jobs = ['dar-feed-monitor.service', 'ikon-feed-monitor.service']
    jobs += ['developer-feed-monitor@' + tenant + '.service' for tenant in
             ('asterus', 'rodina', 'plato', 'aston', 'summa')]
    jobs += ['advelop-new-feed-' + tenant + '.service' for tenant in
             ('donstroy', 'kortros', 'afi', 'apsis', 'sovcombank')]
    for job in jobs:
        state = subprocess.run(['systemctl', 'show', job, '--property=ActiveState', '--value'],
                               capture_output=True, text=True, check=True).stdout.strip()
        if state in {'active', 'activating', 'deactivating'}:
            raise ValueError('Scheduled source check is running; retry after it finishes')
    BACKUP.mkdir(mode=0o700, parents=True, exist_ok=False)
    for row, text in prepared:
        if row['before_sha256'] is not None:
            target = BACKUP / row['path'].lstrip('/')
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            shutil.copy2(row['path'], target)
    (BACKUP / 'manifest.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    installed = []
    try:
        for row, text in prepared:
            path = Path(row['path'])
            temporary = path.with_suffix('.availability-new')
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(descriptor, 'w') as stream:
                stream.write(text)
            os.replace(temporary, path)
            installed.append(row)
        for row in installed:
            if digest(Path(row['path']).read_bytes()) != row['after_sha256']:
                raise ValueError('Post-deployment checksum mismatch')
    except Exception:
        for row in reversed(installed):
            path = Path(row['path'])
            if row['before_sha256'] is None:
                path.unlink()
            else:
                shutil.copy2(BACKUP / row['path'].lstrip('/'), path)
        raise
    print('AVAILABILITY_DEPLOYED', len(installed), 'verified source files; no service restart or schedule change')


if __name__ == '__main__':
    try:
        main(sys.argv[1], verify_only='--verify' in sys.argv[2:])
    except Exception as error:
        raise SystemExit('Availability deployment stopped: ' + str(error)) from None
