# Private report-delivery diagnostics

Prepared on 4 November 2026. This document describes the implementation, not
proof of production deployment. The feature is disabled unless the service has
`TELEGRAM_DELIVERY_JOURNAL` set. VPS rollout and MASTERS activation must be
verified separately. At preparation time browser control was disabled and SSH
was unreachable; production activation had not been performed.

## Scope and privacy

The shared adapter covers scheduled feed reports for all 13 bots: MASTERS,
DAR, IKON, Asterus / ALIA, Rodina, Plato / Kutuzov, Aston, DomDau, Donstroy,
Kortros, AFI, APSIS GLOBE and Sovcombank.

Names, usernames and delivery errors are not added to subscriber reports.
Private receipt databases have mode `0600` and live on Timeweb. The diagnostic
CLI requires the server administrator; the owner can receive its findings
privately. Administrators who can read these server files can also see the
recipient identifiers. Ordinary service logs contain hashed recipient references,
report identifiers and safe error categories, without names, chat IDs or tokens.

No dashboard is read or edited by this feature. There are no Google API calls,
new dashboard tabs, spreadsheet logs or permission changes. Existing schedules,
feed comparisons, website alarms and `/start` behaviour remain unchanged.
The separate ALIA 11:30 analytics report, `/start` reply outbox and website
incident outbox retain their existing delivery implementations; they are not
silently migrated to this feed-report journal.

## Receipts and retry rules

A report is identified by tenant and the original timezone-aware `checked_at`.
Each recipient has a persistent claim and receipt. Identical report text with a
new scheduled observation time is a new report. Reformatting an old observation
does not create permission to send it again; conflicting text is rejected.

| State | Meaning | Retry |
| --- | --- | --- |
| `pending` | Recipient was queued durably; no request attempted yet | Safe to send to this recipient |
| `accepted` | Telegram returned a positive message ID | Skip permanently for this report/recipient |
| `rejected` | Telegram explicitly rejected the request | Retry only this recipient |
| `unconfirmed` | Connection/response failure; delivery cannot be established | No automatic resend; confirm with recipient first |
| `attempting` | Durable claim exists; process may have stopped before storing its receipt | Same conservative rule as `unconfirmed` |

`accepted` confirms Telegram accepted the message; it does not establish that
the person read it or that their phone showed a notification. Telegram does not
provide an atomic transaction with the local journal. A connection can fail
after acceptance, so an absolute exactly-once guarantee is not claimed.

Explicit 429 responses with a delay of at most five seconds receive at most
three attempts, for that recipient only. Longer delays are recorded and honored.
Network uncertainty never triggers a second message request or a post-send IP
failover. Receipt-storage requests may be repeated because they are idempotent.

The entire recipient list is persisted before sending, so a stopped process
leaves both attempted and not-yet-attempted recipients visible in diagnostics.
Concurrent senders cannot claim the same report/recipient. A journal outage
prevents untracked sends. There is no fallback broadcast to the owner. One
recipient's Telegram rejection does not prevent processing the remaining list.
Blocked subscribers remain recorded for diagnosis; this adapter does not alter
subscription state or turn a Telegram network error into an unsubscribe.

Existing cached reports are seeded with `pre_journal_unknown`, not declared
delivered or failed. Historical recipients cannot be reconstructed from missing
logs. Seeding never overwrites a known receipt or broadcasts an old report.

## Closed operator workflow

Run on Timeweb as root. No diagnostics are posted in Telegram by these commands.

```sh
python3 /opt/advelop-command-intake/delivery_admin.py dar status
python3 /opt/advelop-command-intake/delivery_admin.py dar status --names
```

The first command returns references and reasons. `--names` resolves names via
Telegram `getChat` for this private console only. Review the original `checked_at`,
report key, recipient reference, active subscription, error code and reason.

Preview one intended retry; replace placeholders with the exact diagnostic values:

```sh
python3 /opt/advelop-command-intake/delivery_admin.py dar retry --report REPORT_KEY --recipient RECIPIENT_REF
```

Add `--send` to actually send only that report to that recipient. The CLI refuses
accepted receipts, inactive subscriptions, superseded reports and unspecific
broadcast retries. For `unconfirmed`/`attempting`, first establish that the person
did not receive it, wait at least ten minutes after the attempt, and add
`--confirmed-missing`. This override is stored in the private audit history.
Do not resolve an incident by regenerating a whole new report for all subscribers.

Safe reasons include `bot_blocked`, `account_deactivated`, `chat_not_found`,
`rate_limited`, `invalid_formatting`, `message_too_long`, `network` and
`invalid_response`. Unknown outcomes must not be described as proven nondelivery.

## Rollout

Stage this reviewed repository in a readable source-only directory on Timeweb,
outside the running app trees. Do not put credentials in the staging directory.

```sh
python3 scripts/deploy_delivery_journal.py
python3 scripts/deploy_delivery_journal.py --apply
```

Dry-run verifies exact shared-source hashes, monitor hook locations, registry
schemas, cached reports, users and state paths. Apply refuses active monitor jobs,
backs up sources/drop-ins, seeds private journals, atomically installs the adapter
and minimal live-engine hooks, tests imports as each service user, sets the 12 VPS
job environments, and restarts only the shared registry HTTP service. Monitor
timers and subscriber intake services are not restarted or rescheduled. No
network source check or Telegram report send is initiated by the installer.

Verify service health and the authenticated MASTERS claim/receipt endpoint before
enabling MASTERS. Its private GitHub repository needs the identical
`telegram_delivery.py`, monitor hook, and `TELEGRAM_DELIVERY_JOURNAL: remote` in
the report step environment. Do not activate that workflow before the endpoint
is confirmed live. Its existing bot-token authentication remains required;
the endpoint provides neither a public recipient list nor a general send API.

Source/drop-in rollback copies and metadata are stored under
`/var/backups/advelop-delivery-journal-TIMESTAMP`. Apply failure restores changed
sources/drop-ins and reloads systemd. Inert seeded journals remain private and
are retained deliberately; never delete live receipts to force resending.
After deployment, verify the next scheduled run's receipt totals for all bots.
Record deployment hashes and activation evidence separately from this design.
