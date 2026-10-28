"""Install only the five October clients in a separate application tree.

Run as root from the staged repository: python3 .../onboard_clients.py BUNDLE.json.
The private 0600 bundle maps tenant IDs to environment values. No legacy poller,
old application files, old subscriber database or old schedule is modified.
"""
import datetime as dt
import html
import importlib.util
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import subprocess
import sys

NEW_TENANTS = ('donstroy', 'kortros', 'afi', 'apsis', 'sovcombank')
REPOSITORY = Path(__file__).resolve().parents[2]
DESTINATION = Path('/opt/advelop-client-onboarding')
ENGINE = DESTINATION / 'developer-feed-monitor'
TRANSPORT = Path('/opt/advelop-command-intake/transport.py')


def source_files():
    names = ['publication_control.py', 'floor_control.py', 'website_catalogues.py']
    names += ['developer-feed-monitor/' + n for n in (
        'clients.json', 'config.py', 'monitor.py', 'run.py', 'deploy/relay.py',
        'deploy/configure_bot.py', 'deploy/resend_latest.py')]
    return names


def units(tenant, username):
    state = '/var/lib/' + tenant + '-feed-monitor'
    user = tenant + '-monitor'
    common = ['User=' + user, 'Group=' + user, 'WorkingDirectory=' + state,
              'EnvironmentFile=/etc/' + tenant + '-feed-monitor.env',
              'Environment=PYTHONDONTWRITEBYTECODE=1', 'UMask=0077',
              'NoNewPrivileges=yes', 'ProtectSystem=strict', 'ProtectHome=yes',
              'PrivateTmp=yes', 'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6',
              'ReadWritePaths=' + state]
    monitor = ['[Unit]', 'Description=ADVelop ' + tenant + ' feed monitor',
               'After=network-online.target', 'Wants=network-online.target', '',
               '[Service]', 'Type=oneshot', *common,
               'ExecStart=/usr/bin/python3 ' + str(ENGINE / 'run.py') +
               ' --cache-dir ' + state + '/cache --output ' + state +
               '/latest.json --state-file ' + state + '/state.json --send-telegram --force-notify',
               'TimeoutStartSec=15min', '']
    timer = ['[Unit]', 'Description=ADVelop ' + tenant + ' four daily reports', '',
             '[Timer]', 'OnCalendar=*-*-* 09,13:00:00 Europe/Moscow',
             'OnCalendar=*-*-* 15,18:30:00 Europe/Moscow', 'Persistent=true',
             'AccuracySec=1min', 'Unit=advelop-new-feed-' + tenant + '.service', '',
             '[Install]', 'WantedBy=timers.target', '']
    intake = ['[Unit]', 'Description=ADVelop ' + tenant + ' durable subscriber intake',
              'After=network-online.target', 'Wants=network-online.target', '',
              '[Service]', 'Type=simple', *common,
              'Environment=INTAKE_TENANT=' + tenant,
              'Environment=INTAKE_USERNAME=' + username,
              'Environment=INTAKE_DATABASE=' + state + '/subscribers.sqlite3',
              'Environment=INTAKE_RELAY_MODULE=' + str(ENGINE / 'deploy/relay.py'),
              'Environment=INTAKE_SECRET_ENV=RELAY_PATH_SECRET',
              'Environment=RELAY_DELIVERY_MODE=polling',
              'ExecStart=/usr/bin/python3 /opt/advelop-command-intake/intake.py',
              'Restart=always', 'RestartSec=5', 'WatchdogSec=180',
              'NotifyAccess=main', 'TimeoutStopSec=10', '',
              '[Install]', 'WantedBy=multi-user.target', '']
    return {'advelop-new-feed-' + tenant + '.service': '\n'.join(monitor),
            'advelop-new-feed-' + tenant + '.timer': '\n'.join(timer),
            'advelop-intake-' + tenant + '.service': '\n'.join(intake)}


def validate_bundle(bundle, clients):
    if set(bundle) != set(NEW_TENANTS):
        raise ValueError('Bundle must contain exactly the five new clients')
    tokens = set()
    for tenant, values in bundle.items():
        if not isinstance(values, dict) or not values.get('TELEGRAM_BOT_TOKEN'):
            raise ValueError('Bot credential missing: ' + tenant)
        if values['TELEGRAM_BOT_TOKEN'] in tokens:
            raise ValueError('A bot token was reused between tenants')
        tokens.add(values['TELEGRAM_BOT_TOKEN'])
        allowed = {'TELEGRAM_BOT_TOKEN'}
        for account in clients[tenant]['accounts'].values():
            if account.get('enabled') is not False:
                allowed.update(account['secrets'])
        if set(values) - allowed:
            raise ValueError('Unexpected or disabled credentials: ' + tenant)
        if any(not isinstance(v, str) or '\n' in v or '\r' in v or '\x00' in v for v in values.values()):
            raise ValueError('Invalid environment value: ' + tenant)


def main(bundle_path):
    if os.geteuid() != 0:
        raise RuntimeError('Root required')
    path = Path(bundle_path)
    if path.stat().st_mode & 0o077:
        raise ValueError('Bundle permissions must be 0600')
    bundle = json.loads(path.read_text())
    clients = json.loads((REPOSITORY / 'developer-feed-monitor/clients.json').read_text())
    validate_bundle(bundle, clients)
    if DESTINATION.exists():
        raise RuntimeError('Destination already exists; review existing deployment before updating')
    # Fail before changing the host if a new tenant collides with a prior service.
    for tenant in NEW_TENANTS:
        try:
            pwd.getpwnam(tenant + '-monitor')
        except KeyError:
            pass
        else:
            raise RuntimeError('Tenant service user already exists: ' + tenant)
        if Path('/etc/' + tenant + '-feed-monitor.env').exists() or Path('/var/lib/' + tenant + '-feed-monitor').exists():
            raise RuntimeError('Tenant already exists: ' + tenant)
        for filename in units(tenant, clients[tenant]['username']):
            if Path('/etc/systemd/system', filename).exists():
                raise RuntimeError('Unit already exists: ' + filename)
    spec = importlib.util.spec_from_file_location('onboarding_transport', TRANSPORT)
    transport = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transport)
    for tenant, values in bundle.items():
        identity = transport.Telegram(values['TELEGRAM_BOT_TOKEN']).call('getMe')
        if identity.get('username', '').casefold() != clients[tenant]['username'].casefold():
            raise ValueError('Bot identity mismatch: ' + tenant)
    for filename in source_files():
        target = DESTINATION / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPOSITORY / filename, target)
        os.chmod(target, 0o644)
    sys.path.insert(0, str(ENGINE))
    from deploy.relay import RelayStore, _token_hash
    unit_paths = []
    for tenant, values in bundle.items():
        username = tenant + '-monitor'
        subprocess.run(['useradd', '--system', '--home-dir', '/var/lib/' + tenant + '-feed-monitor',
                        '--shell', '/usr/sbin/nologin', username], check=True)
        info = pwd.getpwnam(username)
        state = Path('/var/lib/' + tenant + '-feed-monitor')
        state.mkdir(mode=0o700)
        env = {**values, 'MONITOR_TENANT': tenant, 'RELAY_DATABASE': str(state / 'subscribers.sqlite3'),
               'RELAY_PATH_SECRET': secrets.token_urlsafe(40),
               'TELEGRAM_LATEST_MESSAGE_URL': 'local-registry'}
        fd = os.open('/etc/' + tenant + '-feed-monitor.env', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(''.join(k + '=' + v + '\n' for k, v in env.items()))
        store = RelayStore(state / 'subscribers.sqlite3')
        store.register_token(tenant, _token_hash(values['TELEGRAM_BOT_TOKEN']))
        store.mark_webhook_configured(tenant)
        # An onboarding notice is never presented as a completed source check.
        projects = ', '.join(p['name'] for p in clients[tenant]['projects'].values())
        notice = ('🟡 <b>' + html.escape(clients[tenant]['name']) + ' — контроль выгрузок</b>\n\n'
                  'Подключение выполняется. Первый опрос источников ещё не завершён; '
                  'количество лотов и публикация пока не подтверждены.\n\n'
                  'Проекты: ' + html.escape(projects) + '\n\n'
                  'Рассылка: 09:00, 13:00, 15:30, 18:30 МСК. '
                  '/start подключает подписку; /stop отключает её.')
        store.save_latest(tenant, notice, dt.datetime.now(dt.timezone.utc).isoformat(), 'HTML')
        for item in state.rglob('*'):
            os.chown(item, info.pw_uid, info.pw_gid)
        os.chown(state, info.pw_uid, info.pw_gid)
        for filename, body in units(tenant, clients[tenant]['username']).items():
            target = Path('/etc/systemd/system', filename)
            target.write_text(body)
            os.chmod(target, 0o644)
            unit_paths.append(str(target))
        subprocess.run([sys.executable, str(ENGINE / 'deploy/configure_bot.py')],
                       env={**os.environ, **env}, check=True)
    subprocess.run(['systemd-analyze', 'verify', *unit_paths], check=True)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    for tenant in NEW_TENANTS:
        for unit in ('advelop-intake-' + tenant + '.service', 'advelop-new-feed-' + tenant + '.timer'):
            subprocess.run(['systemctl', 'enable', '--now', unit], check=True)
        subprocess.run(['systemctl', 'start', '--no-block', 'advelop-new-feed-' + tenant + '.service'], check=True)
        print(tenant + ': isolated monitor, durable intake and four daily checks enabled', flush=True)


if __name__ == '__main__':
    try:
        main(sys.argv[1])
    except Exception as error:
        raise SystemExit('Onboarding stopped: ' + type(error).__name__) from None
