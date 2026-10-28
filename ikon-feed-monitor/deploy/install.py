"""Root-only initial bootstrap. Decrypt credentials in memory, never print them.

Run from a protected staging directory containing monitor.py, config.py, deploy/,
recipient.key, recipient.pem and ikon-sealed.cms. Never replace the shared relay.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import urllib.parse


def main():
    stage = Path(__file__).resolve().parent.parent
    app = Path("/opt/ikon-feed-monitor")
    if Path("/etc/ikon-feed-monitor.env").exists():
        raise RuntimeError("Existing configuration requires explicit operator review")
    result = subprocess.run(
        ["openssl", "cms", "-decrypt", "-binary", "-inform", "DER",
         "-in", str(stage / "ikon-sealed.cms"), "-recip", str(stage / "recipient.pem"),
         "-inkey", str(stage / "recipient.key")],
        check=True, capture_output=True,
    )
    values = json.loads(result.stdout)
    keys = {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "CIAN_API_KEY_RESIDENTIAL",
            "AVITO_CLIENT_ID_RESIDENTIAL", "AVITO_CLIENT_SECRET_RESIDENTIAL"}
    if set(values) != keys or any(not isinstance(v, str) or not v or "\n" in v or "\r" in v for v in values.values()):
        raise RuntimeError("Invalid credential bundle")
    token = values["TELEGRAM_BOT_TOKEN"]
    if not token.startswith("8628586333:"):
        raise RuntimeError("Unexpected bot identity")
    with sqlite3.connect("/var/lib/advelop-telegram-relay/relay.sqlite3") as db:
        record = db.execute("SELECT token_hash FROM credentials WHERE tenant='ikon'").fetchone()
    if not record or record[0] != hashlib.sha256(token.encode()).hexdigest():
        raise RuntimeError("Credential does not match the existing IKON registry")
    relay = dict(line.strip().split("=", 1) for line in Path("/etc/advelop-telegram-relay.env").read_text().splitlines() if "=" in line)
    values["TELEGRAM_LATEST_MESSAGE_URL"] = (
        "http://127.0.0.1:" + relay.get("RELAY_BIND_PORT", "8765")
        + urllib.parse.urlsplit(relay["RELAY_PUBLIC_BASE"]).path.rstrip("/")
        + "/v1/ikon/" + relay["IKON_PATH_SECRET"]
    )
    if subprocess.run(["id", "-u", "ikon-monitor"], capture_output=True).returncode:
        subprocess.run(["useradd", "--system", "--home-dir", "/var/lib/ikon-feed-monitor",
                        "--shell", "/usr/sbin/nologin", "ikon-monitor"], check=True)
    app.mkdir(exist_ok=True)
    (app / "deploy").mkdir(exist_ok=True)
    for name in ("monitor.py", "config.py"):
        shutil.copy2(stage / name, app / name)
        os.chmod(app / name, 0o644)
    state = Path("/var/lib/ikon-feed-monitor")
    state.mkdir(exist_ok=True, mode=0o700)
    subprocess.run(["chown", "ikon-monitor:ikon-monitor", str(state)], check=True)
    fd = os.open("/etc/ikon-feed-monitor.env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        for key, value in values.items():
            # systemd EnvironmentFile double-quote escaping, not shell evaluation.
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            stream.write(key + '="' + escaped + '"\n')
    for name in ("ikon-feed-monitor.service", "ikon-feed-monitor.timer"):
        shutil.copy2(stage / "deploy" / name, Path("/etc/systemd/system") / name)
    subprocess.run(["systemd-analyze", "verify", "/etc/systemd/system/ikon-feed-monitor.service",
                    "/etc/systemd/system/ikon-feed-monitor.timer"], check=True)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    print("IKON installed; registry preserved; timer awaits live validation.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("IKON install failed: " + type(error).__name__)
        raise SystemExit(1)
