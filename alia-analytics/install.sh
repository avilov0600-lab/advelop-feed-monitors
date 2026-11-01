#!/bin/sh
# Install this isolated component only. Does not touch any existing bot or monitor.
set -eu
cd "$(dirname "$0")"
test "$(id -u)" = 0
test -f /etc/asterus-feed-monitor.env
test -f /var/lib/asterus-feed-monitor/subscribers.sqlite3
test -f /opt/advelop-command-intake/transport.py
install -d -m 0755 /opt/alia-analytics
install -d -m 0700 /var/lib/alia-analytics
for file in run.py providers.py dashboard.py render.py requirements.txt; do
    install -m 0644 "$file" "/opt/alia-analytics/$file"
done
python3 -m venv /opt/alia-analytics/.venv
/opt/alia-analytics/.venv/bin/python -m pip install --disable-pip-version-check -q -r /opt/alia-analytics/requirements.txt
/opt/alia-analytics/.venv/bin/python -m py_compile /opt/alia-analytics/*.py
install -m 0644 alia-analytics.service /etc/systemd/system/alia-analytics.service
install -m 0644 alia-analytics.timer /etc/systemd/system/alia-analytics.timer
systemd-analyze verify /etc/systemd/system/alia-analytics.service /etc/systemd/system/alia-analytics.timer
systemctl daemon-reload
printf '%s\n' 'Installed. Timer is not enabled by this installer.'
