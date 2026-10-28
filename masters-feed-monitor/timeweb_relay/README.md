# Timeweb Telegram relay

The relay keeps MASTERS and IKON subscribers in separate SQLite namespaces and
stores the latest digest for `/start`. It replaces the unavailable Yandex Cloud
Functions endpoints while leaving monitoring in GitHub Actions.

Runtime layout:

- application: `/opt/advelop-telegram-relay/relay.py`;
- secrets: `/etc/advelop-telegram-relay.env` (`0600`, not committed);
- state: `/var/lib/advelop-telegram-relay/relay.sqlite3`;
- service: `advelop-telegram-relay.service`;
- public base: `https://app.topsku.pro/advelop-telegram`;
- local listener: `127.0.0.1:8765` behind Nginx.

The first authenticated `sync_latest` request captures only a SHA-256 digest of
the bot token and configures the corresponding Telegram webhook. The bot token
itself is not persisted on the VPS. Each tenant has its own random path secret.

`import_yandex_state.py` performs the one-time migration of the previous JSON
subscriber registries and cached messages. The exported JSON files are copied
through a protected temporary directory and deleted after a verified import.

If the relay is unavailable, the monitor must still deliver the report to
`TELEGRAM_CHAT_ID`; this fallback is implemented in each client repository.
