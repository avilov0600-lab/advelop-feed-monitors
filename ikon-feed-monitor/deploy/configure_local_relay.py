"""Root-only repair of the loopback URL; never changes relay or subscribers."""
import os
from pathlib import Path
import shlex
import urllib.parse

path = Path("/etc/ikon-feed-monitor.env")
values = dict(shlex.split(line)[0].split("=", 1) for line in path.read_text().splitlines())
relay = dict(line.strip().split("=", 1) for line in Path("/etc/advelop-telegram-relay.env").read_text().splitlines() if "=" in line)
values["TELEGRAM_LATEST_MESSAGE_URL"] = (
    "http://127.0.0.1:" + relay.get("RELAY_BIND_PORT", "8765")
    + urllib.parse.urlsplit(relay["RELAY_PUBLIC_BASE"]).path.rstrip("/")
    + "/v1/ikon/" + relay["IKON_PATH_SECRET"]
)
fd = os.open(str(path) + ".new", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as stream:
    for key, value in values.items():
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        stream.write(key + '="' + escaped + '"\n')
os.replace(str(path) + ".new", path)
print("IKON loopback endpoint configured; credentials and registry preserved.")
