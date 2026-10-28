#!/usr/bin/env python3
"""One-time importer for subscriber JSON files from Yandex Object Storage."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from relay import RelayStore, _valid_chat_id


LATEST_NAMES = {
    "masters": "latest-telegram.json",
    "ikon": "ikon-latest-telegram.json",
}


def import_tenant(store: RelayStore, tenant: str, directory: Path) -> dict[str, int | bool]:
    latest_path = directory / LATEST_NAMES[tenant]
    latest_imported = False
    if latest_path.exists():
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        message = latest.get("latest_message")
        if isinstance(message, str) and message.strip():
            store.save_latest(
                tenant,
                message,
                latest.get("latest_checked_at"),
                "HTML" if latest.get("parse_mode") == "HTML" else None,
            )
            latest_imported = True

    imported = 0
    for path in directory.glob("*.json"):
        if path.name == LATEST_NAMES[tenant]:
            continue
        chat_id = path.stem
        record = json.loads(path.read_text(encoding="utf-8"))
        if (
            not _valid_chat_id(chat_id)
            or not isinstance(record, dict)
            or type(record.get("active")) is not bool
            or type(record.get("last_update_id")) is not int
        ):
            raise ValueError(f"invalid {tenant} subscriber record")
        revision = int(record["last_update_id"])
        if record["active"]:
            store.subscribe(tenant, chat_id, revision)
        else:
            store.unsubscribe(tenant, chat_id, revision)
        imported += 1
    return {"subscribers": imported, "latest": latest_imported}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--masters-dir", type=Path, required=True)
    parser.add_argument("--ikon-dir", type=Path, required=True)
    args = parser.parse_args()
    store = RelayStore(args.database)
    result = {
        "masters": import_tenant(store, "masters", args.masters_dir),
        "ikon": import_tenant(store, "ikon", args.ikon_dir),
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
