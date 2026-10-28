"""Prepare source-only, SHA-guarded updates for both current VPS application trees."""
import ast
import base64
import difflib
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zlib

ROOT = Path(__file__).resolve().parents[1]
OLD_BASE = 'b506c6409e4a014b99340ff4d5a66e04c0d6aee0'
NEW_BASE = 'a281a8b'


def phase(source, client):
    parser_marker = '    return parser\n'
    if source.count(parser_marker) != 1:
        raise ValueError('Unexpected CLI parser')
    source = source.replace(parser_marker,
        "    parser.add_argument('--refresh-site-availability', action='store_true',\n"
        "                        help='Refresh only public website status in an existing --output report')\n" + parser_marker, 1)
    old_main = '    if not args.offline:\n        download_sources(args.cache_dir)\n    report = analyze_directory(args.cache_dir)\n'
    if source.count(old_main) != 1:
        raise ValueError('Unexpected source-check orchestration')
    source = source.replace(old_main,
        '    if args.refresh_site_availability:\n'
        '        from site_availability_integration import read_existing_report\n'
        '        report = read_existing_report(args.output)\n'
        '    else:\n'
        '        if not args.offline:\n'
        '            download_sources(args.cache_dir)\n'
        '        report = analyze_directory(args.cache_dir)\n', 1)
    marker = '        report = analyze_directory(args.cache_dir)\n    if args.output:'
    if source.count(marker) != 1:
        raise ValueError('Unexpected main orchestration')
    return source.replace(marker, '        report = analyze_directory(args.cache_dir)\n'
        '    from site_availability_integration import check_report_sites\n'
        '    check_report_sites(report, ' + client + ', PROJECTS, WEBSITES, offline=args.offline)\n'
        '    if args.output:', 1)


def render(source):
    source = source.replace('from zoneinfo import ZoneInfo\n',
        'from zoneinfo import ZoneInfo\nfrom site_availability_integration import availability_status_line\n', 1)
    marker = "                if not compact: lines.extend(['','<i>Сайт (справочно, не эталон)</i>'])\n"
    if source.count(marker) != 1:
        raise ValueError('Unexpected shared renderer')
    source = source.replace(marker, marker +
        "                if site.get('status') != 'not_applicable':\n"
        "                    lines.append(e(availability_status_line(report, project_key, compact=compact)))\n", 1)
    old = "lines.append(e('⚪ Сайт недоступен: '+site.get('reason','сетевая ошибка')+'; число квартир не проверено'))"
    if source.count(old) != 1:
        raise ValueError('Unexpected website count fallback')
    return source.replace(old, "lines.append(e('⚪ Число квартир сайта не подтверждено: '+site.get('reason','каталог не удалось прочитать')))", 1)


def original(ref, name):
    return subprocess.check_output(['git', '-C', str(ROOT), 'show', ref + ':' + name]).decode()


def update(path, old, new):
    ast.parse(new)
    result = {'path': path, 'before_sha256': hashlib.sha256(old.encode()).hexdigest() if old is not None else None,
              'after_sha256': hashlib.sha256(new.encode()).hexdigest()}
    if old is None:
        result['content'] = new
    else:
        a, b = old.splitlines(keepends=True), new.splitlines(keepends=True)
        result['edits'] = [[sum(map(len, a[:i])), sum(map(len, a[:j])), ''.join(b[k:l])]
            for op, i, j, k, l in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if op != 'equal']
    return result


def build_plan():
    updates = []
    for prefix in ('/opt', '/opt/advelop-client-onboarding'):
        for name in ('site_availability.py', 'site_availability_integration.py'):
            updates.append(update(prefix + '/' + name, None, (ROOT / name).read_text()))
    for name, client in (('dar-feed-monitor', "'DAR'"), ('ikon-feed-monitor', "'IKON DEVELOPMENT'"),
                         ('developer-feed-monitor', 'CLIENT_NAME')):
        old = original(OLD_BASE, name + '/monitor.py')
        updates.append(update('/opt/' + name + '/monitor.py', old, phase(old, client)))
    old = original(OLD_BASE, 'publication_control.py')
    updates.append(update('/opt/publication_control.py', old, render(old)))
    for name in ('publication_control.py', 'developer-feed-monitor/monitor.py'):
        updates.append(update('/opt/advelop-client-onboarding/' + name,
                              original(NEW_BASE, name), (ROOT / name).read_text()))
    return {'updates': updates, 'old_base': OLD_BASE, 'new_base': NEW_BASE,
            'scope': 'public website availability; no credentials, databases, subscriptions or schedules'}


if __name__ == '__main__':
    destination = Path(sys.argv[1])
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    plan = build_plan()
    raw = json.dumps(plan, ensure_ascii=False).encode()
    (destination / 'plan.json').write_bytes(raw)
    (destination / 'plan.zlib.b64').write_text(base64.b64encode(zlib.compress(raw)).decode())
    print(json.dumps({'files': len(plan['updates']), 'payload_bytes': len(raw),
                      'compressed_base64_chars': len((destination / 'plan.zlib.b64').read_text()),
                      'plan_sha256': hashlib.sha256(raw).hexdigest()}))
