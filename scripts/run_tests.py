"""Run isolated, offline unit suites; never execute the monitor or deployment."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SUITES = ['.', 'dar-feed-monitor', 'developer-feed-monitor', 'ikon-feed-monitor', 'masters-feed-monitor', 'telegram-intake']


def main():
    failed = []
    for name in SUITES:
        directory = ROOT / name
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', MONITOR_TENANT='asterus',
                   PYTHONPATH=os.pathsep.join([str(directory), str(ROOT)]))
        # No production service credentials are inherited into tests.
        for key in list(env):
            if key.startswith(('TELEGRAM_', 'CIAN_', 'AVITO_')):
                del env[key]
        command = [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests', '-q']
        print('Testing ' + name, flush=True)
        if subprocess.run(command, cwd=directory, env=env).returncode:
            failed.append(name)
    if failed:
        print('FAILED: ' + ', '.join(failed))
        return 1
    print('All component suites passed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
