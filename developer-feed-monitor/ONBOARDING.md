# New clients — 28 October 2026

Status: all five bots deployed to Timeweb on 28 October 2026. First source
checks, saved reports, durable intake and four daily timers were verified.
Each bot was started through Telegram Web; all five subscriber registries
recorded the test subscription. Client material listed below remains pending.

| Tenant | Telegram username approved by owner | Projects | Known missing material |
| --- | --- | --- | --- |
| donstroy | advelop_donstroy_bot | Река, Остров, Татарская | Confirmed Cian key; non-API project cards |
| kortros | advelop_kortros_bot | Secret Garden, Октарин | Non-API project cards; Secret Garden M2 URL or confirmation of no placement |
| afi | advelop_afi_bot | Сиреневый парк, Одинбург, AFI Tower | Maternal sources; confirmed Cian key; non-API project cards |
| apsis | advelop_apsis_bot | ГАЛА | Independent client baseline; non-API project cards |
| sovcombank | advelop_sovcombank_bot | Серебро, Три квартала | Source table of saleable lots |

The Cian credential entered for Donstroy and AFI was identical. Both Cian
accounts are disabled in configuration; no request, including daily statistics,
may use that credential even if it is accidentally present in the environment.
Kortros and Sovcombank have two distinct account mappings each. Secrets and
credentials are not part of this document or the repository.

Donstroy's maternal XML contains several other developments. Match the exact
`JKSchema/Name` (Река, Остров, Татарская 35), then select residential sale records.
A missing project, changed schema or invalid response is unavailable, never a
successful zero. Download the shared XML only once per run.

Donstroy's agency Cian IDs add verified technical namespaces: `_11` for Река
and Татарская 35, `_1` for Остров. Matching canonical IDs were checked against
the shared maternal XML for all matching lots, with identical numeric areas
and floors. Strip only the configured suffix in those project-specific Cian
feeds; unexpected suffixes fail closed. Other tenants and the maternal IDs
remain unchanged. Missing/zero maternal building storeys are unknown values,
reported as an unperformed storey comparison rather than a zero-storey building.

AFI's supplied maternal URLs are homepages. Do not use them as inventory feeds.
GALA's client exports appear in both feed columns; until an independent baseline
is supplied, compare the supplied XMLs to one another and to actual publication,
without claiming independent baseline confirmation. Sovcombank has no website
by design: website checks are not applicable, not an outage.

Reports retain project headings, spaces, source → website → XML → publication
order, marketing-date checks and separate Cian daily statistics. Missing card
URLs remain explicit and prevent a fully green report. A green XML comparison
never establishes public publication on a non-API marketplace.

## Installation

Use `deploy/onboard_clients.py` only from a fully staged repository on Timeweb.
It validates all five bot identities and rejects duplicate tokens, disabled
credentials, existing tenant environments, service users and units before
installation. Source files are copied into `/opt/advelop-client-onboarding`.
Existing `/opt/developer-feed-monitor`, its clients and registries are not changed.

Each new client uses its own `/etc/TENANT-feed-monitor.env` (0600), service user,
`/var/lib/TENANT-feed-monitor` directory (0700), subscriber SQLite and latest report.
The shared durable intake code is reused without replacing it. Never start an
old poller beside the durable intake. An explicit setup notice is saved before
the first network run, so `/start` can subscribe immediately and return a response.

Schedule: 09:00, 13:00, 15:30, 18:30 Europe/Moscow. Each new timer is named
`advelop-new-feed-TENANT.timer`; subscriber intake is `advelop-intake-TENANT.service`.
The application and schedule are not running merely because this file exists.

For repeat deployments, verify all five service identities, timers, saved reports
and `/start` subscription/delivery before sharing bot links with colleagues. Delete
the temporary credential-transfer files after the server has received and
verified its protected environments. Never put that bundle in Git or a public
download location.

The first run returned a Cian publication discrepancy for GALA (21 versus 20
in XML) and an exact Avito match for AFI (301 versus 301 in XML). Sovcombank's
two Cian and two Avito accounts returned zero in the existing publication scope;
those access/account mappings need owner review. A red report can reflect missing
client material or inventory mismatches while the services remain healthy.

Original application files and all eight original intake services were left in
place. Temporary credential bundles on the Mac and VPS were removed after the
five root-owned 0600 environment files were verified.

## Subscriber delivery recheck

All five bots were checked again through Telegram Web after the 15:30 scheduled
run. `/start` returned that saved scheduled report and the subscription
confirmation; its verification timestamp did not change. The same deployed
registry/intake combination is covered by tests for three independent new chats
per client, the next broadcast recipient list without any owner ID, repeated
starts without duplicate subscriptions, a network failure followed by a worker
restart, and one user's Telegram rate limit without blocking the next colleague.
These checks add no runtime changes and do not rerun feed/API requests on `/start`.
