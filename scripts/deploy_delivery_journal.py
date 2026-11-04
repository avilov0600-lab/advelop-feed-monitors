"""Guarded VPS report-delivery rollout; dry-run by default, never sends reports."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from telegram_delivery import Ledger, report_identity
from delivery_admin import TENANTS, paths

NEW = {'donstroy', 'kortros', 'afi', 'apsis', 'sovcombank'}
FIXED = (
    ('telegram_delivery.py', '/opt/telegram_delivery.py', None),
    ('telegram_delivery.py', '/opt/advelop-client-onboarding/telegram_delivery.py', None),
    ('scripts/delivery_admin.py', '/opt/advelop-command-intake/delivery_admin.py', None),
    ('advelop-telegram-relay/relay.py', '/opt/advelop-telegram-relay/relay.py',
     'b9da4e3d53684929f4439a4130addd50dff302d08f9e94648ae8156cf0b1a546'),
    ('telegram-intake/transport.py', '/opt/advelop-command-intake/transport.py',
     '77e0fdd982f3d0a8da1dd6d0c6ec1f1216a384e6b155ec594dcf06829059a90c'),
)
MONITORS = (
    ('dar', '/opt/dar-feed-monitor/monitor.py'),
    ('ikon', '/opt/ikon-feed-monitor/monitor.py'),
    ('developer', '/opt/developer-feed-monitor/monitor.py'),
    ('developer', '/opt/advelop-client-onboarding/developer-feed-monitor/monitor.py'),
)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def unit(tenant):
    if tenant == 'masters':
        return 'advelop-telegram-relay.service'
    if tenant in NEW:
        return 'advelop-new-feed-' + tenant + '.service'
    if tenant in {'dar', 'ikon'}:
        return tenant + '-feed-monitor.service'
    return 'developer-feed-monitor@' + tenant + '.service'


def prop(service, name):
    return subprocess.check_output(['systemctl', 'show', service, '-p', name, '--value'], text=True).strip()


def hook(source, tenant):
    """Keep each live engine, including differences between the two app trees."""
    addition = ("from telegram_delivery import install_monitor as _install_delivery_journal\n"
                f"_install_delivery_journal(sys.modules[__name__], '{tenant}')\n\n")
    marker = 'if __name__ == "__main__":\n    sys.exit(main())'
    if addition in source:
        return source
    if ('_install_delivery_journal' in source or source.count(marker) != 1
            or 'def broadcast_telegram(' not in source
            or 'str(Path(__file__).resolve().parents[1])' not in source):
        raise ValueError('Unrecognized monitor layout; live review required')
    result = source.replace(marker, addition + marker)
    ast.parse(result)
    return result


def cached(tenant):
    ledger, registry = paths(tenant)
    with sqlite3.connect(registry.as_uri() + '?mode=ro', uri=True, timeout=10) as con:
        row = con.execute('SELECT message,checked_at,parse_mode FROM latest WHERE tenant=?', (tenant,)).fetchone()
        recipients = [str(r[0]) for r in con.execute(
            'SELECT chat_id FROM subscribers WHERE tenant=? AND active=1', (tenant,))]
    if not row:
        raise ValueError('No cached report: ' + tenant)
    report_identity(tenant, row[0], row[1], row[2])
    expected = None if tenant == 'masters' else 'HTML'
    if row[2] != expected:
        raise ValueError('Cached parse mode differs from monitor: ' + tenant)
    if not ledger.parent.is_dir() or ledger.is_symlink() or registry.is_symlink():
        raise ValueError('Unexpected state path: ' + tenant)
    owner = pwd.getpwnam(prop(unit(tenant), 'User') or 'root')
    if ledger.exists() and ledger.stat().st_uid != owner.pw_uid:
        raise ValueError('Existing journal owner differs: ' + tenant)
    return ledger, row, recipients, owner


def quiescent():
    for tenant in TENANTS:
        if tenant != 'masters' and prop(unit(tenant), 'ActiveState') in {'active', 'activating', 'deactivating'}:
            raise ValueError('Scheduled report is running: ' + tenant)


def atomic_write(path, raw, uid=0, gid=0, mode=0o644):
    pending = path.with_name(path.name + '.delivery-pending')
    created = False
    try:
        with pending.open('xb') as stream:
            created = True
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(pending, uid, gid)
        os.chmod(pending, mode)
        pending.replace(path)
    finally:
        if created and pending.exists():
            pending.unlink()


def probe(tenant):
    """No credentials, dashboard reads, network requests or broadcasts."""
    os.environ['MONITOR_TENANT'] = tenant
    os.environ['TELEGRAM_DELIVERY_JOURNAL'] = str(paths(tenant)[0])
    prefix = Path('/opt/advelop-client-onboarding') if tenant in NEW else Path('/opt')
    folder = prefix / (tenant + '-feed-monitor' if tenant in {'dar', 'ikon'} else 'developer-feed-monitor')
    # The installed module must win over the staging directory.
    sys.path[:0] = [str(folder), str(prefix)]
    sys.modules.pop('telegram_delivery', None)
    import monitor
    assert monitor.DELIVERY_JOURNAL_ENABLED
    from telegram_delivery import Ledger as InstalledLedger
    report, rows = InstalledLedger(paths(tenant)[0]).status(tenant)
    assert report is not None
    print(json.dumps({'tenant': tenant, 'adapter_enabled': True, 'recorded_recipients': len(rows)}))


def main(apply=False):
    if os.geteuid() != 0:
        raise ValueError('Run preflight/apply as the server administrator')
    prepared = []
    for source, destination, expected in FIXED:
        path, new = Path(destination), (ROOT / source).read_bytes()
        if path.is_symlink() or not path.parent.is_dir():
            raise ValueError('Unexpected application path: ' + destination)
        old = path.read_bytes() if path.exists() else None
        if old is not None and digest(old) not in {expected, digest(new)}:
            raise ValueError('Live source drift: ' + destination)
        if old is None and expected:
            raise ValueError('Missing installed component: ' + destination)
        ast.parse(new)
        prepared.append((path, old, new))
    for tenant, destination in MONITORS:
        path = Path(destination)
        if path.is_symlink():
            raise ValueError('Unexpected monitor path')
        old = path.read_bytes()
        prepared.append((path, old, hook(old.decode(), tenant).encode()))
    for tenant in TENANTS:
        ledger, row, recipients, owner = cached(tenant)
        print(json.dumps({'tenant': tenant, 'checked_at': row[1], 'subscribers': len(recipients),
                          'service_user': owner.pw_name}), flush=True)
        if tenant == 'masters':
            continue  # GitHub is enabled separately, only after this endpoint is live.
        dropin = Path('/etc/systemd/system') / (unit(tenant) + '.d/delivery-journal.conf')
        new = ('[Service]\nEnvironment=TELEGRAM_DELIVERY_JOURNAL=' + str(ledger) + '\n').encode()
        old = dropin.read_bytes() if dropin.exists() else None
        if dropin.is_symlink() or (old is not None and old != new):
            raise ValueError('Existing drop-in must be reviewed: ' + tenant)
        prepared.append((dropin, old, new))
    print('DELIVERY_JOURNAL_PREFLIGHT_OK', flush=True)
    if not apply:
        return
    quiescent()
    for path, old, _ in prepared:
        if (path.read_bytes() if path.exists() else None) != old:
            raise ValueError('Concurrent edit: ' + str(path))
    backup = Path('/var/backups/advelop-delivery-journal-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))
    backup.mkdir(mode=0o700, exist_ok=False)
    manifest = []
    for path, old, new in prepared:
        stat = path.stat() if old is not None else None
        entry = {'path': str(path), 'existed': old is not None, 'new_sha256': digest(new),
                 'uid': stat.st_uid if stat else 0, 'gid': stat.st_gid if stat else 0,
                 'mode': stat.st_mode & 0o777 if stat else 0o644}
        manifest.append(entry)
        if old is not None:
            saved = backup / str(path).lstrip('/')
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, saved)
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    # Read existing registries only. The baseline cannot prove past delivery;
    # quarantine old cached reports rather than initiating any old resend.
    for tenant in TENANTS:
        ledger, row, recipients, owner = cached(tenant)
        Ledger(ledger).seed_previous(tenant, row[0], row[1], recipients, row[2])
        os.chown(ledger, owner.pw_uid, owner.pw_gid)
        os.chmod(ledger, 0o600)
    changed = []
    try:
        quiescent()
        for (path, old, new), entry in zip(prepared, manifest):
            if (path.read_bytes() if path.exists() else None) != old:
                raise ValueError('Concurrent edit: ' + str(path))
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, new, entry['uid'], entry['gid'], entry['mode'])
            changed.append(entry)
        for tenant in TENANTS:
            if tenant == 'masters':
                continue
            owner = pwd.getpwnam(prop(unit(tenant), 'User') or 'root')
            subprocess.run(['runuser', '-u', owner.pw_name, '--', sys.executable,
                            __file__, '--probe', tenant], check=True)
        subprocess.run(['systemctl', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', 'restart', unit('masters')], check=True)
        subprocess.run(['systemctl', 'is-active', '--quiet', unit('masters')], check=True)
    except Exception:
        for entry in reversed(changed):
            path = Path(entry['path'])
            if entry['existed']:
                atomic_write(path, (backup / str(path).lstrip('/')).read_bytes(),
                             entry['uid'], entry['gid'], entry['mode'])
            elif path.exists():
                path.unlink()
        subprocess.run(['systemctl', 'daemon-reload'], check=False)
        subprocess.run(['systemctl', 'restart', unit('masters')], check=False)
        raise
    for path, _, new in prepared:
        assert digest(path.read_bytes()) == digest(new)
    print('DELIVERY_JOURNAL_VPS_INSTALLED backup=' + str(backup), flush=True)
    print('MASTERS GitHub still needs its separately verified remote-journal activation.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--probe', choices=tuple(t for t in TENANTS if t != 'masters'))
    args = parser.parse_args()
    try:
        probe(args.probe) if args.probe else main(args.apply)
    except Exception as error:
        # No credential-bearing tracebacks in a shared console recording.
        print('Delivery rollout stopped: ' + type(error).__name__ + ': ' + str(error), file=sys.stderr)
        raise SystemExit(1)
