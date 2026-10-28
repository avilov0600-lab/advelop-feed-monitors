"""Root-only staged deployment. Credentials never leave their existing VPS files."""
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT = Path('/opt/advelop-command-intake')
STAGE = Path(__file__).resolve().parents[1]
BACKUP = Path('/var/backups/advelop-intake-20261019')
SHARED_DB = '/var/lib/advelop-telegram-relay/relay.sqlite3'
CLIENTS = {
    'dar': {'username': 'advelop_dar_bot', 'user': 'dar-relay',
            'database': '/var/lib/dar-telegram-relay/relay.sqlite3',
            'module': '/opt/dar-feed-monitor/deploy/relay.py', 'secret': 'DAR_PATH_SECRET',
            'envs': ['/etc/dar-telegram-relay.env', '/etc/dar-telegram-poller.env'],
            'old_unit': 'dar-telegram-poller.service'},
    'ikon': {'username': 'advelop_ikon_bot', 'user': 'advelop-relay',
             'database': SHARED_DB, 'module': '/opt/advelop-telegram-relay/relay.py',
             'secret': 'IKON_PATH_SECRET', 'envs': ['/etc/advelop-telegram-relay.env', '/etc/ikon-feed-monitor.env']},
    'masters': {'username': 'advelopmastersbot', 'user': 'advelop-relay',
                'database': SHARED_DB, 'module': '/opt/advelop-telegram-relay/relay.py',
                'secret': 'MASTERS_PATH_SECRET', 'envs': ['/etc/advelop-telegram-relay.env'],
                'token_file': '/var/lib/advelop-telegram-relay/intake-tokens/masters.token'},
}
for tenant, username in [('asterus', 'advelop_asterus_bot'), ('aston', 'advelop_aston_bot'),
                         ('rodina', 'advelop_rodina_bot'), ('plato', 'advelop_plato_kutuzov_bot'),
                         ('summa', 'advelop_summa_bot')]:
    CLIENTS[tenant] = {'username': username, 'user': tenant + '-monitor',
        'database': '/var/lib/' + tenant + '-feed-monitor/subscribers.sqlite3',
        'module': '/opt/developer-feed-monitor/deploy/relay.py', 'secret': 'RELAY_PATH_SECRET',
        'envs': ['/etc/' + tenant + '-feed-monitor.env'],
        'old_unit': 'developer-telegram-poller@' + tenant + '.service'}


def environment(paths):
    values = {}
    for path in paths:
        for line in Path(path).read_text().splitlines():
            if line.strip() and not line.lstrip().startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                parsed = shlex.split(value, comments=False)
                values[key] = parsed[0] if parsed else ''
    return values


def token_for(client):
    values = environment(client['envs'])
    if values.get('TELEGRAM_BOT_TOKEN'):
        return values['TELEGRAM_BOT_TOKEN']
    path = Path(client.get('token_file', '/nonexistent'))
    return path.read_text().strip() if path.exists() else None


def load_transport():
    sys.path.insert(0, str(STAGE if (STAGE / 'transport.py').exists() else ROOT))
    from transport import Telegram
    return Telegram


def preflight(include_masters=False):
    Telegram = load_transport()
    for tenant, client in CLIENTS.items():
        token = token_for(client)
        if token is None and tenant == 'masters' and not include_masters:
            print(json.dumps({'preflight': tenant, 'status': 'awaiting_authenticated_sync'}), flush=True)
            continue
        if not token:
            raise RuntimeError('Missing credential: ' + tenant)
        identity = Telegram(token).call('getMe')
        if identity.get('username', '').casefold() != client['username'].casefold():
            raise RuntimeError('Identity mismatch: ' + tenant)
        import hashlib
        with sqlite3.connect('file:' + client['database'] + '?mode=ro', uri=True) as con:
            row = con.execute('SELECT token_hash FROM credentials WHERE tenant=?', (tenant,)).fetchone()
        if not row or row[0] != hashlib.sha256(token.encode()).hexdigest():
            raise RuntimeError('Registry mismatch: ' + tenant)
        print(json.dumps({'preflight': tenant, 'status': 'identity_and_registry_verified'}), flush=True)


def backup_file(path):
    path = Path(path)
    target = BACKUP / str(path).lstrip('/')
    if path.exists() and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(path, target)


def unit(tenant, client):
    lines = ['[Unit]', 'Description=ADVelop ' + tenant + ' durable Telegram intake',
             'After=network-online.target', 'Wants=network-online.target', '', '[Service]',
             'Type=simple', 'User=' + client['user'], 'Group=' + client['user'],
             'WorkingDirectory=' + str(Path(client['database']).parent)]
    lines += ['EnvironmentFile=' + p for p in client['envs']]
    variables = {'INTAKE_TENANT': tenant, 'INTAKE_USERNAME': client['username'],
        'INTAKE_RELAY_MODULE': client['module'], 'INTAKE_DATABASE': client['database'],
        'INTAKE_SECRET_ENV': client['secret'], 'PYTHONDONTWRITEBYTECODE': '1',
        'RELAY_DELIVERY_MODE': 'polling'}
    if client.get('token_file'):
        variables['INTAKE_TOKEN_FILE'] = client['token_file']
    lines += ['Environment=' + k + '=' + v for k, v in variables.items()]
    lines += ['ExecStart=/usr/bin/python3 /opt/advelop-command-intake/intake.py',
              'Restart=always', 'RestartSec=5', 'WatchdogSec=180', 'NotifyAccess=main',
              'TimeoutStopSec=10', 'UMask=0077', 'NoNewPrivileges=yes',
              'ProtectSystem=strict', 'ProtectHome=yes', 'PrivateTmp=yes',
              'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6',
              'ReadWritePaths=' + str(Path(client['database']).parent),
              '', '[Install]', 'WantedBy=multi-user.target', '']
    return '\n'.join(lines)


def prepare():
    preflight()
    BACKUP.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(BACKUP, 0o700)
    for database in sorted(set(c['database'] for c in CLIENTS.values())):
        target = BACKUP / Path(database).parent.name / Path(database).name
        target.parent.mkdir(mode=0o700, exist_ok=True)
        if not target.exists():
            with sqlite3.connect('file:' + database + '?mode=ro', uri=True) as source, sqlite3.connect(target) as dest:
                source.backup(dest)
            os.chmod(target, 0o600)
    backup_file('/opt/advelop-telegram-relay/relay.py')
    backup_file('/etc/advelop-telegram-relay.env')
    for name in ('intake.py', 'transport.py'):
        backup_file(ROOT / name)
        ROOT.mkdir(mode=0o755, exist_ok=True)
        shutil.copy2(STAGE / name, ROOT / name)
        os.chmod(ROOT / name, 0o644)
    paths = []
    for tenant, client in CLIENTS.items():
        path = Path('/etc/systemd/system/advelop-intake-' + tenant + '.service')
        backup_file(path)
        path.write_text(unit(tenant, client))
        os.chmod(path, 0o644)
        paths.append(str(path))
    subprocess.run(['systemd-analyze', 'verify', *paths], check=True)
    print('Prepared eight verified service definitions; no running intake changed.', flush=True)


def activate():
    # Identity preflight is deliberately repeated immediately before rollout.
    preflight()
    shutil.copy2(STAGE / 'deploy/shared_relay.py', '/opt/advelop-telegram-relay/relay.py')
    os.chmod('/opt/advelop-telegram-relay/relay.py', 0o644)
    path = Path('/etc/advelop-telegram-relay.env')
    content = [line for line in path.read_text().splitlines()
               if not line.startswith(('RELAY_DELIVERY_MODE=', 'RELAY_CAPTURE_MASTERS_TOKEN='))]
    content += ['RELAY_DELIVERY_MODE=polling', 'RELAY_CAPTURE_MASTERS_TOKEN=1']
    temporary = path.with_suffix('.env.intake-new')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        stream.write('\n'.join(content) + '\n')
    os.replace(temporary, path)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'restart', 'advelop-telegram-relay.service'], check=True)
    for tenant, client in CLIENTS.items():
        # Existing subscriptions, report, token hash and alert state stay in place.
        os.chmod(Path(client['database']).parent, 0o700)
        if client.get('old_unit'):
            subprocess.run(['systemctl', 'disable', '--now', client['old_unit']], check=True)
        if tenant != 'masters' or token_for(client):
            subprocess.run(['systemctl', 'enable', '--now', 'advelop-intake-' + tenant + '.service'], check=True)
    print('Seven intakes activated; MASTERS starts after its authenticated GitHub sync.', flush=True)


def status():
    Telegram = load_transport()
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    for tenant, client in CLIENTS.items():
        result = {'tenant': tenant}
        result['service'] = subprocess.run(['systemctl', 'is-active', 'advelop-intake-' + tenant + '.service'], capture_output=True, text=True).stdout.strip()
        with sqlite3.connect('file:' + client['database'] + '?mode=ro', uri=True) as con:
            result['subscribers'] = con.execute('SELECT active,count(*) FROM subscribers WHERE tenant=? GROUP BY active', (tenant,)).fetchall()
            result['latest'] = con.execute('SELECT checked_at,length(message) FROM latest WHERE tenant=?', (tenant,)).fetchone()
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='intake_health'").fetchone():
                row = con.execute('SELECT poll_ok_at,command_at,reply_ok_at,last_error FROM intake_health WHERE tenant=?', (tenant,)).fetchone()
                if row:
                    result['health'] = {'poll_age_seconds': round(max(0, time.time() - row[0]), 1) if row[0] else None,
                                        'last_command_unix': row[1], 'last_reply_unix': row[2], 'last_error': row[3]}
                result['outbox'] = con.execute('SELECT state,count(*) FROM intake_outbox WHERE tenant=? GROUP BY state', (tenant,)).fetchall()
        token = token_for(client)
        if token:
            try:
                info = Telegram(token).call('getWebhookInfo')
                result['webhook_enabled'] = bool(info.get('url'))
                result['telegram_pending'] = info.get('pending_update_count')
            except Exception as error:
                result['telegram_error_class'] = type(error).__name__
        print(json.dumps(result, ensure_ascii=False), flush=True)


def patch_broadcast():
    # The five old scheduled senders forced the same broken IPv6 path. Only
    # replace their Telegram-opening adapter, never feed/API/business logic.
    old = '''def _telegram_urlopen(request, *, timeout):
    if os.environ.get("TELEGRAM_IP_FAMILY") == "ipv6":
        return urllib.request.build_opener(_IPv6HTTPSHandler()).open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)
'''
    new = '''def _telegram_urlopen(request, *, timeout):
    # Shared IPv4-first transport; subscriber intake and scheduled delivery use
    # the same healthy route, with validated TLS and IPv6 fallback.
    import importlib.util
    module_name = "advelop_intake_transport"
    if module_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(module_name, "/opt/advelop-command-intake/transport.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[module_name] = module
    return sys.modules[module_name].urlopen(request, timeout=timeout)
'''
    targets = [Path('/opt') / p / 'monitor.py' for p in
               ('dar-feed-monitor', 'developer-feed-monitor', 'ikon-feed-monitor')]
    sources = {}
    import ast
    for path in targets:
        source = path.read_text()
        if new in source:
            continue
        if source.count(old) != 1:
            raise RuntimeError('Unexpected Telegram adapter: ' + path.parent.name)
        updated = source.replace(old, new, 1)
        ast.parse(updated)
        sources[path] = updated
    for path, source in sources.items():
        backup_file(path)
        temporary = path.with_suffix('.py.intake-new')
        temporary.write_text(source)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        print('Scheduled Telegram transport updated: ' + path.parent.name, flush=True)


def finalize():
    # Capture was a one-time server-side bootstrap, not an ongoing token sink.
    if not token_for(CLIENTS['masters']):
        raise RuntimeError('MASTERS bootstrap is incomplete')
    path = Path('/etc/advelop-telegram-relay.env')
    source = path.read_text()
    updated = source.replace('RELAY_CAPTURE_MASTERS_TOKEN=1', 'RELAY_CAPTURE_MASTERS_TOKEN=0')
    if updated != source:
        temporary = path.with_suffix('.env.intake-final')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(updated)
        os.replace(temporary, path)
        subprocess.run(['systemctl', 'restart', 'advelop-telegram-relay.service'], check=True)
    # Guard against a future legacy provisioning script starting a second
    # getUpdates consumer while the replacement is already installed.
    for client in CLIENTS.values():
        old_unit = client.get('old_unit')
        if not old_unit:
            continue
        directory = Path('/etc/systemd/system') / (old_unit + '.d')
        directory.mkdir(exist_ok=True)
        path = directory / '80-replaced-by-durable-intake.conf'
        backup_file(path)
        path.write_text('[Unit]\nConditionPathExists=!/opt/advelop-command-intake/intake.py\n')
        os.chmod(path, 0o644)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    print('One-time credential capture disabled; duplicate legacy consumers guarded.', flush=True)


if __name__ == '__main__':
    try:
        action = sys.argv[1]
        if action == 'prepare':
            prepare()
        elif action == 'activate':
            activate()
        elif action == 'activate-masters':
            preflight(include_masters=True)
            subprocess.run(['systemctl', 'enable', '--now', 'advelop-intake-masters.service'], check=True)
        elif action == 'status':
            status()
        elif action == 'patch-broadcast':
            patch_broadcast()
        elif action == 'finalize':
            finalize()
        else:
            raise ValueError('Unsupported action')
    except Exception as error:
        print(json.dumps({'deployment_error_class': type(error).__name__}), flush=True)
        raise SystemExit(1) from None
