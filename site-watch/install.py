"""Install only this new service; never modify other bots, credentials or registries."""
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

TARGETS = {
    'watch.py': ('/opt/advelop-site-watch/watch.py', 0o644),
    'sites.json': ('/etc/advelop-site-watch/sites.json', 0o600),
    'advelop-site-watch.service': ('/etc/systemd/system/advelop-site-watch.service', 0o644),
}


def main(directory):
    assert os.geteuid() == 0
    source = Path(directory)
    manifest = json.loads((source/'hashes.json').read_text())
    assert set(manifest) == set(TARGETS)
    assert not Path('/var/lib/advelop-site-watch').exists(), 'Existing watcher state: review before updating'
    for name, (target, _) in TARGETS.items():
        assert hashlib.sha256((source/name).read_bytes()).hexdigest() == manifest[name]
        assert not Path(target).exists() and not Path(target).is_symlink(), 'Existing target: '+target
    ast.parse((source/'watch.py').read_text())
    subprocess.run(['systemd-analyze','verify',str(source/'advelop-site-watch.service')],check=True)
    installed=[]
    try:
        for name,(target,mode) in TARGETS.items():
            path=Path(target)
            path.parent.mkdir(parents=True,exist_ok=True,mode=0o700 if name=='sites.json' else 0o755)
            with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,mode),'wb') as stream:
                stream.write((source/name).read_bytes())
            installed.append(path)
        Path('/var/lib/advelop-site-watch').mkdir(mode=0o700)
        subprocess.run(['systemctl','daemon-reload'],check=True)
        subprocess.run(['systemctl','enable','--now','advelop-site-watch.service'],check=True)
        subprocess.run(['systemctl','is-active','--quiet','advelop-site-watch.service'],check=True)
    except Exception:
        subprocess.run(['systemctl','disable','--now','advelop-site-watch.service'],check=False,capture_output=True)
        # Keep new state for diagnosis; original apps and data are never touched.
        raise
    print('SITE_WATCH_ENABLED verified_files='+str(len(installed)))


if __name__ == '__main__':
    main(sys.argv[1])
