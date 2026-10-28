"""Retry a saved digest without requesting websites, XML or marketplace APIs."""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import run  # Installs the isolated local subscriber registry.
from deploy.relay import RelayStore
from monitor import broadcast_telegram


def main():
    tenant = os.environ['MONITOR_TENANT']
    latest = RelayStore(Path(os.environ['RELAY_DATABASE'])).latest(tenant)
    if not latest.get('message'):
        raise RuntimeError('No saved report')
    broadcast_telegram(latest['message'], latest['checked_at'])


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit('Saved report delivery failed: ' + type(error).__name__) from None
