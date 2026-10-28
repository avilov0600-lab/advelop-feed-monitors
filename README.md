# ADVelop Feed Monitors

Source-code repository for real-estate feed monitoring bots.

The `codex/onboard-five-developers` branch adds eleven projects for Donstroy,
Kortros, AFI, APSIS GLOBE and Sovcombank. These five clients were deployed on
28 October 2026 into their own application tree and are outside the older
eight-bot live-source verification below. The installer uses
a separate `/opt/advelop-client-onboarding` tree and never replaces the eight
existing bots. The public `main` branch retains the verified production baseline.

## Snapshot status — 28 October 2026

The source snapshot was verified against the live VPS through its authenticated
Timeweb COM console at **2026-10-28 13:53:39 MSK**, using read-only SHA-256 checks.
All **51 installed files** in the verification scope match. The inventory covered
all **27 non-test Python files** in the five known application directories, with
no unmapped Python files. Credentials and subscriber databases were not read.
The server, services and schedules were not modified.

This is a verified **source snapshot**, not a full disk/runtime-state backup.
Three historical bootstrap files are retained locally but are not deployed on
the VPS. Per-file hashes, path mappings and scope are in
`verification/live-source-verification-20261028.json`.

- MASTERS: current `main` downloaded from its existing GitHub repository.
- IKON: current GitHub `main`, supplemented by the later local VPS adapter.
- DAR and developer clients: local implementation sources.
- Telegram intake: sources of the shared fix deployed on 19 October 2026.
- Shared comparison modules: October audit versions, now verified on the live VPS.
- Shared relay and diagnostic helpers: live path layout and byte-identical sources.

The Telegram network adapter changes from 19 October are reproduced from the
saved deployment script. Their provenance is recorded in `snapshot.json`.
Historical test mocks/wording were updated to the shared transport and the
already-approved source → website → XML → publication report format; runtime
business rules were not changed as part of this backup.
The diagnostic script under `dar-feed-monitor/deploy/` was reconciled to the live
version; the different root-level diagnostic script is preserved separately.
The original repositories and production services are not changed by this import.

## Components

| Directory | Purpose / clients | Production location |
| --- | --- | --- |
| `masters-feed-monitor/` | Capital Group / MASTERS monitor | Original GitHub Actions workflow |
| `ikon-feed-monitor/` | IKON residential projects | `/opt/ikon-feed-monitor` |
| `dar-feed-monitor/` | DAR: Dom na Chasovoy, Rakurs | `/opt/dar-feed-monitor` |
| `developer-feed-monitor/` | Asterus, Rodina, Plato, Aston, DomDau | `/opt/developer-feed-monitor` |
| `telegram-intake/` | `/start`, subscriptions, durable replies for all eight bots | `/opt/advelop-command-intake` |
| `advelop-telegram-relay/` | Common MASTERS/IKON registry and historical state-import helper | `/opt/advelop-telegram-relay` |
| root Python modules | Publication checks, website catalogues and floor normalization | `/opt/*.py` |

`advelop-telegram-relay/relay.py` is the deployed common registry service for
MASTERS and IKON. Its byte-identical deployment template is preserved as
`telegram-intake/deploy/shared_relay.py`.
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

## Public website availability — 28 October 2026

Every report includes a website availability row beside each project's website
lot count. A positive row means the public endpoint responds; it does not prove
that the inventory parser obtained valid lots. Parsing failures remain unknown,
never zero. Website availability is secondary and does not change the maternal
XML baseline or the feed comparison status.

`site_availability.py` contains the supplied HTTP checker, hardened for malformed
HTTP responses and duplicate URLs. `site_availability_integration.py` is the
report adapter. It checks only configured public websites, never XML feeds,
marketplace cards or private APIs. It uses GET without reading the response
body, follows redirects and verifies TLS. Scheduled reports use three attempts,
an 8-second request timeout and 2-second pauses. The standalone checker's original
30/3/30 defaults remain available. Checks within a client are sequential and
identical URLs are requested once per report.

There are no separate Telegram outage messages, availability databases, history
files or extra scheduled runs. `/start` returns the saved report and makes no
website request. Projects without a configured website do not gain a fabricated
availability result. Each observation has its own time.

For an operational presentation refresh, `--refresh-site-availability --output
/var/lib/CLIENT-feed-monitor/latest.json` reuses a valid existing report and
refreshes only public website availability. Feed/API requests are skipped and
the original lot counts and feed check time are retained. Add the normal send
flags only when a new broadcast is intended.

The source-only VPS rollout is reproducible with
`scripts/prepare_site_availability_deployment.py` and guarded by exact source
hashes in `scripts/deploy_site_availability.py`. Original VPS applications use
the verified `b506c64` baseline plus this focused patch; the isolated five-client
tree uses `a281a8b` plus the patch. Do not overwrite the original developer engine
with the newer isolated engine. Rollback copies are retained on the VPS.
MASTERS is deployed separately in its original private GitHub repository.

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

Before the next production deployment, compare the allowlisted source files
again and import any drift. With the repository available on the VPS, a read-only
repeat check is:

```sh
python3 scripts/verify_live_sources.py verification/live-source-verification-20261028.json
```

Never collect `/etc` secrets or `/var/lib` databases as part of that source check.

## Original repositories

- https://github.com/avilov0600-lab/masters-feed-monitor
- https://github.com/avilov0600-lab/ikon-feed-monitor

Their existing histories remain intact; exact imported revisions and file hashes
are listed in `snapshot.json`.
