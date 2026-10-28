"""Run as root from a protected staging directory on the existing VPS."""
import os
import secrets
import shutil
import subprocess
from pathlib import Path

stage = Path(__file__).resolve().parent.parent
app = Path('/opt/dar-feed-monitor')
def run(*args): subprocess.run(args, check=True)
def private_file(path, content):
    fd=os.open(path, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
    with os.fdopen(fd,'w') as file: file.write(content)
    os.chmod(path,0o600)

for user in ('dar-monitor','dar-relay'):
    if subprocess.run(['id','-u',user],capture_output=True).returncode:
        run('useradd','--system','--home-dir',f'/var/lib/{user}','--shell','/usr/sbin/nologin',user)
app.mkdir(exist_ok=True)
(app/'deploy').mkdir(exist_ok=True)
for name in ('monitor.py','config.py'):
    shutil.copy2(stage/name,app/name)
for name in ('relay.py','poller.py','configure_bot.py'):
    shutil.copy2(stage/'deploy'/name,app/'deploy'/name)
for path in app.rglob('*.py'): os.chmod(path,0o644)
for name,user in (('dar-feed-monitor','dar-monitor'),('dar-telegram-relay','dar-relay')):
    path=Path('/var/lib')/name;path.mkdir(exist_ok=True,mode=0o700)
    run('chown',f'{user}:{user}',str(path))
relay_env=Path('/etc/dar-telegram-relay.env')
if relay_env.exists():
    existing=dict(line.split('=',1) for line in relay_env.read_text().splitlines() if '=' in line)
    secret=existing['DAR_PATH_SECRET']
else: secret=secrets.token_urlsafe(48)
private_file(relay_env,'RELAY_PUBLIC_BASE=https://app.topsku.pro/dar-telegram\nRELAY_BIND_PORT=8766\nRELAY_DELIVERY_MODE=polling\nDAR_PATH_SECRET='+secret+'\n')
bot_token=(stage/'bot-token').read_text().strip()
assert ':' in bot_token and len(bot_token)>40
private_file('/etc/dar-telegram-poller.env','TELEGRAM_BOT_TOKEN='+bot_token+'\n')
private_file('/etc/dar-feed-monitor.env',(stage/'marketplace.env').read_text().rstrip()+'\nTELEGRAM_BOT_TOKEN='+bot_token+'\nTELEGRAM_LATEST_MESSAGE_URL=https://app.topsku.pro/dar-telegram/v1/dar/'+secret+'\n')
for name in ('dar-feed-monitor.service','dar-feed-monitor.timer','dar-telegram-relay.service','dar-telegram-poller.service'):
    shutil.copy2(stage/'deploy'/name,Path('/etc/systemd/system')/name)
nginx=Path('/etc/nginx/sites-available/topsku-edge')
content=nginx.read_text()
marker='    location / {'
if '^/dar-telegram/' not in content:
    if content.count(marker)!=1: raise RuntimeError('unexpected nginx structure')
    backup=nginx.with_name('topsku-edge.before-dar')
    if not backup.exists(): shutil.copy2(nginx,backup)
    nginx.write_text(content.replace(marker,(stage/'deploy/dar-nginx.conf').read_text()+'\n'+marker,1))
    result=subprocess.run(['nginx','-t'],capture_output=True)
    if result.returncode:
        nginx.write_text(content)
        raise RuntimeError('nginx validation failed; original config restored')
    run('systemctl','reload','nginx')
run('systemd-analyze','verify','/etc/systemd/system/dar-feed-monitor.service','/etc/systemd/system/dar-feed-monitor.timer','/etc/systemd/system/dar-telegram-relay.service')
run('systemctl','daemon-reload')
run('systemctl','enable','--now','dar-telegram-relay.service')
run('systemctl','restart','dar-telegram-relay.service')
run('systemctl','enable','--now','dar-telegram-poller.service')
# Timer enabled after the first live report is verified, not before.
print('DAR installed; relay enabled; monitoring timer awaits live verification.')
