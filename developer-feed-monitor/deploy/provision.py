"""Run once as root on the existing Timeweb host; never print credentials."""
import json
import os
import pwd
import secrets
import subprocess
import sys
from pathlib import Path

ROOT=Path('/opt/developer-feed-monitor')
clients=json.loads((ROOT/'clients.json').read_text())
prepare_only = sys.argv[1] == '--prepare'
bundle={tenant:{} for tenant in clients} if prepare_only else json.loads(Path(sys.argv[1]).read_text())
# Validate all identities before creating environments or starting any poller.
# A token from another BotFather response must never start a second consumer.
if not prepare_only:
    for tenant, values in bundle.items():
        if tenant not in clients or not values.get('TELEGRAM_BOT_TOKEN'):
            raise SystemExit('Unknown tenant or missing bot token')
        subprocess.run([sys.executable,str(ROOT/'deploy'/'configure_bot.py'),'--verify-only'],
            env={**os.environ,**values,'MONITOR_TENANT':tenant,'TELEGRAM_IP_FAMILY':'ipv6'},check=True)
for tenant, values in bundle.items():
    if tenant not in clients:
        raise SystemExit('Unknown tenant')
    user=tenant+'-monitor'
    try: pwd.getpwnam(user)
    except KeyError: subprocess.run(['useradd','--system','--home-dir','/var/lib/'+tenant+'-feed-monitor','--shell','/usr/sbin/nologin',user],check=True)
    state=Path('/var/lib/'+tenant+'-feed-monitor')
    state.mkdir(mode=0o700,exist_ok=True)
    info=pwd.getpwnam(user)
    os.chown(state,info.pw_uid,info.pw_gid)
    os.chmod(state,0o700)
    if prepare_only:
        print(tenant+': runtime directory prepared; no service started')
        continue
    envpath=Path('/etc/'+tenant+'-feed-monitor.env')
    existing=dict(line.split('=',1) for line in envpath.read_text().splitlines() if '=' in line) if envpath.exists() else {}
    env={**existing,**values,'MONITOR_TENANT':tenant,'RELAY_DATABASE':str(state/'subscribers.sqlite3'),
        'RELAY_PATH_SECRET':existing.get('RELAY_PATH_SECRET') or secrets.token_urlsafe(40),
        'TELEGRAM_LATEST_MESSAGE_URL':'local-registry','TELEGRAM_IP_FAMILY':'ipv6'}
    if not env.get('TELEGRAM_BOT_TOKEN'):
        raise SystemExit('Bot token missing: '+tenant)
    fd=os.open(envpath,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:
        f.write(''.join(k+'='+str(v)+'\n' for k,v in env.items()))
    os.chmod(envpath,0o600)
    print(tenant+': isolated environment provisioned')
for filename in ('developer-feed-monitor@.service','developer-feed-monitor@.timer','developer-telegram-poller@.service'):
    subprocess.run(['install','-m','644',str(ROOT/'deploy'/filename),'/etc/systemd/system/'+filename],check=True)
subprocess.run(['systemctl','daemon-reload'],check=True)
if prepare_only:
    raise SystemExit(0)
for tenant in bundle:
    subprocess.run(['systemctl','enable','--now','developer-telegram-poller@'+tenant+'.service'],check=True)
    subprocess.run(['systemctl','enable','--now','developer-feed-monitor@'+tenant+'.timer'],check=True)
    subprocess.run(['systemctl','start','--no-block','developer-feed-monitor@'+tenant+'.service'],check=True)
