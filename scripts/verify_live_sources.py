"""Read-only server source comparison. Never reads credentials, state, or logs."""
import argparse
import hashlib
import json
from pathlib import Path

APPLICATIONS = [Path('/opt') / name for name in (
    'dar-feed-monitor', 'developer-feed-monitor', 'ikon-feed-monitor',
    'advelop-command-intake', 'advelop-telegram-relay')]
SHARED = {'/opt/floor_control.py', '/opt/publication_control.py',
          '/opt/website_catalogues.py', '/opt/audit_all_project_floors.py',
          '/opt/check_monitor_sites.py'}


def allowed(path):
    if path.is_symlink():
        return False
    if str(path) in SHARED:
        return True
    if any(root in path.parents for root in APPLICATIONS):
        return path.suffix in {'.py', '.service', '.timer'} or path.name in {'clients.json', 'config.json'}
    return (path.parent == Path('/etc/systemd/system')
            and path.suffix in {'.service', '.timer'}
            and path.name.startswith(('advelop-intake-', 'advelop-telegram-relay.', 'dar-', 'ikon-', 'developer-')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence', type=Path)
    args = parser.parse_args()
    evidence = json.loads(args.evidence.read_text())
    results = []
    for row in evidence['files']:
        path = Path(row['server_path'])
        if not allowed(path):
            raise ValueError('Refusing path outside project source allowlist')
        status = ('MISSING' if not path.is_file() else 'MATCH' if
                  hashlib.sha256(path.read_bytes()).hexdigest() == row['sha256'] else 'DIFF')
        results.append({'file': row['server_path'], 'status': status})
    known = {row['server_path'] for row in evidence['files']}
    live_python = [path for root in APPLICATIONS for path in root.rglob('*.py')
                   if path.is_file() and '__pycache__' not in path.parts and 'tests' not in path.parts]
    unmapped = [str(path) for path in live_python if str(path) not in known]
    success = all(row['status'] == 'MATCH' for row in results) and not unmapped
    print(json.dumps({'success': success, 'checked': len(results),
                      'matched': sum(row['status'] == 'MATCH' for row in results),
                      'installed_python_files': len(live_python), 'unmapped_python_files': unmapped,
                      'differences': [row for row in results if row['status'] != 'MATCH']}, indent=2))
    return 0 if success else 1


if __name__ == '__main__':
    raise SystemExit(main())
