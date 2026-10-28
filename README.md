# ADVelop Feed Monitors

Private source-code repository for eight real-estate feed monitoring bots.

## Snapshot status — 28 October 2026

This repository preserves the available sources, **not a verified disk snapshot
of the live VPS**. SSH to Timeweb was unavailable during import. The web console
reached a login prompt; live verification is pending authentication.

- MASTERS: current `main` downloaded from its existing GitHub repository.
- IKON: current GitHub `main`, supplemented by the later local VPS adapter.
- DAR and developer clients: local implementation sources.
- Telegram intake: sources of the shared fix deployed on 19 October 2026.
- Shared comparison modules: local versions from the completed October audit.

The Telegram network adapter changes from 19 October are reproduced from the
saved deployment script. Their provenance is recorded in `snapshot.json`.
Historical test mocks/wording were updated to the shared transport and the
already-approved source → website → XML → publication report format; runtime
business rules were not changed as part of this backup.
Do not describe this import as live-server-verified until hashes are compared.
The original repositories and production services are not changed by this import.

## Components

| Directory | Purpose / clients | Production location |
| --- | --- | --- |
| `masters-feed-monitor/` | Capital Group / MASTERS monitor | Original GitHub Actions workflow |
| `ikon-feed-monitor/` | IKON residential projects | `/opt/ikon-feed-monitor` |
| `dar-feed-monitor/` | DAR: Dom na Chasovoy, Rakurs | `/opt/dar-feed-monitor` |
| `developer-feed-monitor/` | Asterus, Rodina, Plato, Aston, DomDau | `/opt/developer-feed-monitor` |
| `telegram-intake/` | `/start`, subscriptions, durable replies for all eight bots | `/opt/advelop-command-intake` |
| root Python modules | Publication checks, website catalogues and floor normalization | `/opt/*.py` |

`telegram-intake/deploy/shared_relay.py` is the saved common registry service for
MASTERS and IKON, deployed as `/opt/advelop-telegram-relay/relay.py`.
The deploy script in that directory records the service definitions for all tenants.

## Product rules

Client maternal source is authoritative. Website information is secondary.
Agency XML feeds are compared with the maternal source by individual lots.
Cian and Avito publication checks use their APIs; other marketplace publication
checks use project cards, with unavailable checks shown explicitly.

`/start` subscribes a private chat and returns the latest saved report without
rerunning the feed check. `/stop` disables the subscription. Registries remain
tenant-scoped. Network failures use a persistent reply outbox and retries.

Report schedule: **09:00, 13:00, 15:30, 18:30 Europe/Moscow**. Importing this
repository does not create schedules, deploy services or send Telegram messages.
The per-component historical `.github/workflows` directories are nested and do
not run from this repository's root.

## Credentials and runtime state

No real tokens, private keys, subscriber identifiers, databases, production
environment files, feed dumps or generated reports belong in this repository.

Production credentials stay in the existing protected `/etc/*feed-monitor.env`
and `/etc/*telegram*.env` files. MASTERS monitor secrets remain in its original
GitHub Actions Secrets. Runtime registries/reports live under `/var/lib/`.
Those files require a **separate encrypted operational backup**, not Git.
This source repository alone is not a full disaster-recovery backup.

## Verification

Python 3.12+; the monitor backends and intake use the Python standard library.

```sh
python3 scripts/check_snapshot.py
python3 scripts/run_tests.py
```

The first command checks for forbidden runtime files, likely embedded secrets,
and snapshot checksum drift. The second runs each component's test suite in a
separate process to prevent `monitor` / `config` import collisions.

## Deployment caution

This is initially a versioned source snapshot. It does **not** replace the active
deployments. Do not run historical `install.py`, `provision.py` or `deploy.py`
against production without reviewing the current server state and backups.
Some historical scripts predate the 19 October intake replacement and contain
obsolete IPv6-only/legacy poller settings. The canonical replacement is
`telegram-intake/`; never start a legacy poller beside the new consumer.

Before the next production deployment, authenticate to Timeweb, compare the
allowlisted source files with this snapshot, import any drift, and update the
live-verification status. Never collect `/etc` secrets or `/var/lib` databases
as part of that source comparison.

## Original repositories

- https://github.com/avilov0600-lab/masters-feed-monitor
- https://github.com/avilov0600-lab/ikon-feed-monitor

Their existing histories remain intact; exact imported revisions and file hashes
are listed in `snapshot.json`.
