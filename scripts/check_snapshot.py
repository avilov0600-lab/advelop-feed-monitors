"""Fail closed on runtime files, likely embedded credentials and snapshot drift."""
import hashlib
import json
from pathlib import Path
import re
import sys
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
TOKEN = re.compile(r'\b[0-9]{6,12}:[A-Za-z0-9_-]{25,}\b')
PRIVATE_KEY = re.compile(r'-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----')
GITHUB_TOKEN = re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{40,})\b')
ASSIGNMENT = re.compile(r'''(?im)^\s*(?:["']?)(?:TELEGRAM_BOT_TOKEN|CIAN_API_KEY(?:_\w+)?|AVITO_CLIENT_SECRET(?:_\w+)?|(?:RELAY_)?PATH_SECRET|PASSWORD)(?:["']?)\s*[:=]\s*["']([^"'\n]{8,})["']''')
URL = re.compile(r'https?://[^\s<>"\']+')
SENSITIVE_PARAMS = {'access_token', 'api_key', 'apikey', 'token', 'password', 'secret', 'client_secret', 'key'}


def files():
    return sorted(p for p in ROOT.rglob('*') if p.is_file() and '.git' not in p.relative_to(ROOT).parts
                  and '__pycache__' not in p.parts and '.pytest_cache' not in p.parts)


def check():
    findings = []
    reviewed = []
    for path in files():
        relative = path.relative_to(ROOT)
        name = relative.as_posix()
        if path.suffix.lower() in {'.key', '.pem', '.token', '.db', '.sqlite3', '.sqlite', '.rtf', '.zip', '.cms'} or (path.name.endswith('.env') or (path.name.startswith('.env') and path.name != '.env.example')):
            findings.append((name, 'forbidden runtime/credential file'))
            continue
        if any(part in {'reports', 'analysis', 'output', 'outputs', 'cache', '.cache', 'node_modules'} for part in relative.parts):
            findings.append((name, 'runtime/generated content'))
            continue
        if path.name == 'snapshot.json':
            continue
        text = path.read_text(encoding='utf-8')
        for label, regex in [('bot token', TOKEN), ('private key', PRIVATE_KEY), ('GitHub token', GITHUB_TOKEN)]:
            for match in regex.finditer(text):
                # Repeated-character fixtures are intentionally non-production.
                value = match.group()
                if label == 'bot token' and len(set(value.split(':', 1)[1])) <= 2 and 'tests' in relative.parts:
                    continue
                findings.append((name, label + ' at line ' + str(text.count('\n', 0, match.start()) + 1)))
        for match in ASSIGNMENT.finditer(text):
            value = match.group(1)
            if not any(marker in value.lower() for marker in ('replace_', 'test', 'example', 'dummy', '${', '/var/lib/', '/etc/', 'os.environ', 'secrets.')):
                findings.append((name, 'literal credential assignment at line ' + str(text.count('\n', 0, match.start()) + 1)))
        for match in URL.finditer(text):
            parsed = urllib.parse.urlsplit(match.group())
            if parsed.username or parsed.password:
                findings.append((name, 'credential-bearing URL'))
            for key, value in urllib.parse.parse_qsl(parsed.query):
                if key.lower() in SENSITIVE_PARAMS and value and not any(marker in value.lower() for marker in ('example', 'test', 'replace', '{', 'token_placeholder')):
                    findings.append((name, 'credential-like URL parameter: ' + key))
        reviewed.append(name)
    manifest_path = ROOT / 'snapshot.json'
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        for name, digest in manifest.get('files_sha256', {}).items():
            path = ROOT / name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                findings.append((name, 'snapshot checksum drift'))
    if findings:
        for name, reason in sorted(set(findings)):
            print(name + ': ' + reason)
        return 1
    print('PASS: ' + str(len(reviewed)) + ' source/documentation files checked; no forbidden files or likely embedded secrets; checksums match.')
    return 0


if __name__ == '__main__':
    raise SystemExit(check())
