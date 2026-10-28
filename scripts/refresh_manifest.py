"""Record provenance and hashes. Optional deterministic replay of the saved code patch."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def reconstruct():
    source = ROOT / 'telegram-intake/deploy/deploy.py'
    tree = ast.parse(source.read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'patch_broadcast')
    values = {node.targets[0].id: ast.literal_eval(node.value) for node in function.body
              if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
              and node.targets[0].id in {'old', 'new'}}
    for component in ('dar-feed-monitor', 'developer-feed-monitor', 'ikon-feed-monitor'):
        path = ROOT / component / 'monitor.py'
        text = path.read_text()
        if values['new'] in text:
            continue
        if text.count(values['old']) != 1:
            raise RuntimeError('Cannot reconstruct adapter: ' + component)
        result = text.replace(values['old'], values['new'], 1)
        ast.parse(result)
        path.write_text(result)
    spec = importlib.util.spec_from_file_location('saved_deploy', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    destination = ROOT / 'deployment/intake'
    destination.mkdir(parents=True, exist_ok=True)
    for tenant, client in module.CLIENTS.items():
        (destination / ('advelop-intake-' + tenant + '.service')).write_text(module.unit(tenant, client))


def main():
    if '--reconstruct-deployment' in sys.argv:
        reconstruct()
    path = ROOT / 'snapshot.json'
    if path.exists():
        manifest = json.loads(path.read_text())
    else:
        revisions = {}
        for name, directory in [('masters', '/private/tmp/advelop-source-snapshots-20261028/masters'),
                                ('ikon', '/private/tmp/advelop-source-snapshots-20261028/ikon')]:
            revisions[name] = subprocess.check_output(['git', '-C', directory, 'rev-parse', 'HEAD'], text=True).strip()
        manifest = {
            'snapshot_date_moscow': '2026-10-28',
            'live_server_verified': False,
            'live_verification_status': 'pending: SSH unavailable; web console requires authenticated login',
            'secrets_and_runtime_state_included': False,
            'original_github_revisions': revisions,
            'provenance': {
                'masters-feed-monitor': 'GitHub main at original_github_revisions.masters; generated analysis/report files excluded',
                'ikon-feed-monitor': 'GitHub main at original_github_revisions.ikon plus later local monitor/config and reconstructed October 19 adapter',
                'dar-feed-monitor': 'local implementation plus reconstructed October 19 adapter',
                'developer-feed-monitor': 'local multi-client implementation plus reconstructed October 19 adapter',
                'telegram-intake': 'local sources of October 19 deployed intake replacement',
                'deployment/intake': 'generated verbatim by saved October 19 deployment unit() function',
                'shared_modules': 'local floor_control.py, publication_control.py, website_catalogues.py from October audit'
            },
            'excluded': ['credential files', 'subscriber databases and chat identifiers', 'generated reports', 'feed dumps', 'private keys', 'runtime caches']
        }
    evidence_path = ROOT / 'verification/live-source-verification-20261028.json'
    if evidence_path.is_file():
        evidence = json.loads(evidence_path.read_text())
        current_matches = all((ROOT / row['repository_path']).is_file() and
            hashlib.sha256((ROOT / row['repository_path']).read_bytes()).hexdigest() == row['sha256']
            for row in evidence['files'])
        manifest['live_server_verified'] = current_matches and evidence['unmapped_installed_python_files'] == 0
        manifest['live_verification_status'] = ('verified: 51 installed files match; all 27 installed non-test Python files covered'
            if manifest['live_server_verified'] else 'source drift since live verification; repeat comparison required')
        manifest['live_verified_at_moscow'] = evidence['verified_at_moscow']
        manifest['live_verification_evidence'] = evidence_path.relative_to(ROOT).as_posix()
        manifest['provenance']['advelop-telegram-relay'] = 'live-verified common registry and historical import helper'
        manifest['provenance']['server_diagnostics'] = 'root and deploy variants retained separately and matched against live SHA-256'
    manifest['files_sha256'] = {}
    for candidate in sorted(ROOT.rglob('*')):
        if not candidate.is_file() or candidate == path:
            continue
        relative = candidate.relative_to(ROOT)
        if any(part in {'.git', '__pycache__', '.pytest_cache'} for part in relative.parts):
            continue
        manifest['files_sha256'][relative.as_posix()] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print('Manifest recorded for ' + str(len(manifest['files_sha256'])) + ' files; live verification remains ' + str(manifest['live_server_verified']))


if __name__ == '__main__':
    main()
