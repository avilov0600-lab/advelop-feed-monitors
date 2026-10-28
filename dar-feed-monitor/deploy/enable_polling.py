"""One-time root migration of DAR intake; no credentials on stdout."""
import os
import subprocess
from pathlib import Path
env=dict(line.split('=',1) for line in Path('/etc/dar-feed-monitor.env').read_text().splitlines() if '=' in line)
path=Path('/etc/dar-telegram-poller.env')
fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
with os.fdopen(fd,'w') as file: file.write('TELEGRAM_BOT_TOKEN='+env['TELEGRAM_BOT_TOKEN']+'\n')
os.chmod(path,0o600)
relay_env=Path('/etc/dar-telegram-relay.env')
content=relay_env.read_text()
if 'RELAY_DELIVERY_MODE=' not in content:
    relay_env.write_text(content.rstrip()+'\nRELAY_DELIVERY_MODE=polling\n')
for args in (['daemon-reload'],['restart','dar-telegram-relay.service'],['enable','--now','dar-telegram-poller.service']):
    subprocess.run(['systemctl',*args],check=True)
print('DAR outbound command intake enabled.')
