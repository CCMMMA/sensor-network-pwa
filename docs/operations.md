# Operations

Authenticated pages and API responses are sent with `Cache-Control: no-store` so private station and account data are not retained by browser or intermediary caches. All responses deny framing, and deployments whose `baseUrl` uses HTTPS send a one-year HSTS policy. Keep `baseUrl` aligned with the public URL; only enable HTTPS there when the portal is consistently available over HTTPS.

Browser writes use a per-session CSRF token in addition to Origin/Referer checks.
Login and password-reset attempts are throttled in the shared SQLite database, so
limits apply across Gunicorn workers. A Content Security Policy restricts scripts and
styles to this origin plus the UI CDN origins, blocks plugins and framing, and allows
HTTPS map imagery.

Operate the PWA as one part of the layered system described in
[Architecture and data flows](architecture.md). A healthy portal does not by itself
prove that edge acquisition is healthy; verify each boundary independently.

## Process roles

| Process | Command | Role |
| --- | --- | --- |
| Web | `gunicorn webapp_wsgi:app` | Serves the application; any number of workers |
| Watchdog | `python main.py --config <config> --watchdog-only` | Anomaly detection, workflow evaluation and emails; exactly one |
| Development | `python main.py --config <config>` | Flask development server and watchdog in one process |

All of them share the SQLite database and need the same `webSessionSecret` (when it
is unset, the web processes share a generated secret kept in that database). MQTT
ingest and the CSV files belong to
[`sensor-network-collector`](https://github.com/CCMMMA/sensor-network-collector).

## Verify a deployment

```bash
docker compose ps
docker compose logs --tail=100 web watchdog
```

Open `/`, verify station freshness, then log in and check the intended Browse,
Download, `Can API`, and `Can workflows` permissions. Open `/api-tokens` with an API-enabled account,
create a short-lived test token, and query an allowed station with its Bearer value.
Open `/workflows` with a workflow-enabled account and confirm its readable stations are offered. The watchdog logs
`Watchdog started (interval=...)`.

Check readiness with `curl --fail https://portal.example.org/health`. A `503`
identifies the failing dependency as `database`, `storage`, or `influxdb` without
disclosing credentials or filesystem paths.

The application publishes the canonical [workflow and API reference](workflows-api.md)
at `/docs/workflows-api`; use it when verifying authentication, graph structure, and
edge-triggered execution.

## Backup and restore

The SQLite database holds account data, hashed API tokens, workflows, workflow firing state, permissions,
anomaly/notification state, the outgoing email queue, administrator impersonation audit events, and the
user access/download log; the configuration holds secrets. Administrators can inspect and export that log at
`/admin/access-log`.
Keep both private.
Create a transactionally consistent online backup while the application is running:

```bash
python -m sensor_network_pwa.maintenance --config config.json \
  backup /secure-backups/auth-2026-11-03.sqlite
python -m sensor_network_pwa.maintenance --config config.json drill
```

The command refuses to overwrite a backup and runs `PRAGMA integrity_check`. The
drill restores into a disposable directory, checks required tables, and compares the
user count without touching the live database. Run it after every backup-system
change and at least quarterly.

To restore for real: stop web and watchdog processes, preserve the failed database,
run the drill against the selected backup, copy it to `authDbPath` with ownership and
mode preserved, start one web worker, verify `/health` and account access, then start
the remaining workers and watchdog. Stop the collector as well when restoring the CSV
tree. An image rollback does not roll back the SQLite schema.

## Load testing

Run the dependency-free probe from a host with production-like network and storage
latency. Use a public test station and do not target the public service without an
approved test window:

```bash
python -m sensor_network_pwa.loadtest https://portal.example.org STATION_UUID \
  --requests 500 --concurrency 20
```

It exercises dashboard snapshot polling and the public station page and reports
median, p95, maximum latency, and failures. Record the dataset size and results in the
release ticket; investigate failures or a p95 above the deployment's SLO.

## Troubleshooting

| Symptom | Checks |
| --- | --- |
| No stations listed | `pathStorage` must be the collector's storage root and readable by the process; directory names beginning with `.` or `_` are excluded from discovery. |
| Startup asks for InfluxDB credentials | `influxdb` is true, or `influxdbUrl` is set, without token/org/bucket. Set `influxdb=false` to use CSV only. |
| `Auth database is read-only` | Make `authDbPath` and its directory writable by the process user. |
| Sessions disappear after restart or between hosts | Configure the same `webSessionSecret` in all web processes. Without it the secret is stored in the auth DB, so processes using different `authDbPath` files do not share sessions. |
| Every page redirects to `/change-password` | The account is flagged for a password change (sample admin password, or forced by an admin); it is lifted once a strong password is saved. |
| `logLevel` seems ignored | It is applied by both `main.py` and `webapp_wsgi.py`; a `logLevel` key in the config file takes precedence over the `LOG_LEVEL` environment variable. |
| No watchdog notifications | Check that one watchdog process runs, `pathStorage`, SMTP settings, the user's email address, alarm silencing, and whether the user acknowledged, snoozed, or cleared the notification on `/profile`. A failed scan is logged as `Watchdog scan failed` or `Watchdog check failed for station=...` and retried at the next interval. |
| Duplicate watchdog notifications after restart | Upgrade to a version with database-backed notification claims, verify that web and watchdog use the same persistent `authDbPath`, and still keep exactly one steady-state watchdog process. |
| Workflow editor/API returns `403` | Workflow routes require `Can workflows`; data routes require `Can API`, unless the account is an administrator. The referenced station must also grant Browse access; `/api/download` requires Download access. A forced password change blocks requests too. |
| Bearer request returns `401` | Check the `Authorization: Bearer ...` syntax, token expiry/revocation, and that its owner is active and still has `Can API`. The full secret cannot be recovered; create a replacement token. |
| A workflow does not send email | Check that it is enabled, its owner is active and retains `Can workflows` and Browse access, exactly one watchdog runs, SMTP is configured, and the condition changed from false to true. Inspect `last_error` through `GET /api/workflows`. |
| A workflow sends no second email while a reading remains above the threshold | This is intentional edge-triggered behavior. The condition must become false before a later true value can fire again. |
| Duplicate alarm or workflow emails | More than one watchdog process is running. |
| Too many or too few reminder emails | Each user sets the notification time on `/profile` (default 60 minutes). |
| Forms return `403 Cross-site request rejected` | The browser's origin host differs from the request host, forwarded host, and `baseUrl` host. Set `baseUrl` to the URL users open and forward `Host`/`X-Forwarded-Host` from the proxy. |
| Forms or session JSON return `403 Invalid or missing CSRF token` | Reload the page to obtain a fresh session token. Custom browser clients submit `csrf_token`; same-origin JavaScript sends `X-CSRF-Token`. Bearer API clients are exempt. |
| Login/reset returns `429` | Wait for `Retry-After`. Repeated responses can indicate credential attacks. |
| Map tiles show `403 Access blocked` | Use the canonical default `mapTileUrl`, keep the app's `strict-origin-when-cross-origin` response policy, and ensure a reverse proxy does not replace it with a policy that removes cross-origin referrers. For sustained public traffic, configure a suitable tile provider and its required attribution. |
| Admin is sent to `/change-password` at first login | The account was created with the default or a sample `adminPassword`; choose a strong password. |
| Dashboards are slow | Check the storage latency of `pathStorage` and that files follow `<station>/YYYY/MM/DD/<station>_YYYYMMDDZHH00.csv`; files elsewhere are found but cannot be skipped by date. With InfluxDB enabled, check its query time (`Influx query failed` warnings). |
| No `Install app` option in the browser | Installation needs HTTPS (or `localhost`), and `/manifest.webmanifest` and `/service-worker.js` must be reachable through the proxy. |
| A station disappeared from the home page | Its policy is `restricted` and the current user is not assigned to it. |
| Login returns to home instead of requested URL | `next` must be a local path with one leading slash and no backslashes or control characters. |

See also [WSGI troubleshooting](wsgi.md).
