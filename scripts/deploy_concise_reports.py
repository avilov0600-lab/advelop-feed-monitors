"""Root-only source update with exact version checks and rollback copies."""
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ALLOWED = {'/opt/concise_reports.py', '/opt/publication_control.py',
           '/opt/advelop-client-onboarding/concise_reports.py',
           '/opt/advelop-client-onboarding/publication_control.py'}
BACKUP = Path('/var/backups/advelop-concise-reports-20261030')


def main(path, verify=False):
    assert os.geteuid() == 0, 'Root required'
    plan = json.loads(Path(path).read_text())
    assert len(plan['updates']) == 4 and {r['path'] for r in plan['updates']} == ALLOWED
    prepared = []
    for row in plan['updates']:
        target = Path(row['path'])
        assert not target.is_symlink() and target.parent.is_dir()
        if row['before_sha256'] is None:
            assert not target.exists(), 'Module already exists'
            result = row['content']
        else:
            raw = target.read_bytes()
            assert hashlib.sha256(raw).hexdigest() == row['before_sha256'], 'Source drift: '+str(target)
            result = raw.decode()
            for start,end,replacement in reversed(row['edits']):
                result = result[:start]+replacement+result[end:]
        assert hashlib.sha256(result.encode()).hexdigest() == row['after_sha256']
        ast.parse(result)
        prepared.append((row,result))
    if verify:
        print('CONCISE_PREFLIGHT_OK', len(prepared)); return
    units = ['dar-feed-monitor.service', 'ikon-feed-monitor.service']
    units += ['developer-feed-monitor@'+t+'.service' for t in ('asterus','rodina','plato','aston','summa')]
    units += ['advelop-new-feed-'+t+'.service' for t in ('donstroy','kortros','afi','apsis','sovcombank')]
    for unit in units:
        state = subprocess.run(['systemctl','show',unit,'-p','ActiveState','--value'],capture_output=True,text=True,check=True).stdout.strip()
        assert state not in {'active','activating','deactivating'}, 'A scheduled check is running'
    BACKUP.mkdir(mode=0o700, exist_ok=False)
    for row,_ in prepared:
        if row['before_sha256']:
            destination=BACKUP/row['path'].lstrip('/'); destination.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
            shutil.copy2(row['path'],destination)
    (BACKUP/'manifest.json').write_text(json.dumps(plan,ensure_ascii=False))
    installed=[]
    try:
        for row,source in prepared:
            target=Path(row['path']); pending=target.with_suffix('.concise-new')
            with os.fdopen(os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o644),'w') as stream:
                stream.write(source)
            os.replace(pending,target); installed.append(row)
        assert all(hashlib.sha256(Path(r['path']).read_bytes()).hexdigest()==r['after_sha256'] for r in installed)
    except Exception:
        for row in reversed(installed):
            if row['before_sha256']: shutil.copy2(BACKUP/row['path'].lstrip('/'),row['path'])
            else: Path(row['path']).unlink()
        raise
    print('CONCISE_DEPLOYED',len(installed),'verified files; no service or schedule changes')


if __name__ == '__main__':
    main(sys.argv[1], '--verify' in sys.argv[2:])
