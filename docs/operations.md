# Operations

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
Download, and `Can API` permissions. Open `/api-tokens` with an API-enabled account,
create a short-lived test token, and query an allowed station with its Bearer value.
Open `/workflows` and confirm its readable stations are offered. The watchdog logs
`Watchdog started (interval=...)`.

## Backup and restore

The SQLite database holds account data, hashed API tokens, workflows, workflow firing state, permissions,
and anomaly/notification state; the configuration holds secrets. Keep both private.
For a consistent copy, stop the web and watchdog services (and the collector,
for the CSV files) before archiving the data directory, then start them again. An image
rollback does not roll back the SQLite schema.

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
| Workflow editor/API returns `403` | The account needs `Can API` unless it is an administrator. The referenced station must also grant Browse access; `/api/download` requires Download access. A forced password change blocks API requests too. |
| Bearer request returns `401` | Check the `Authorization: Bearer ...` syntax, token expiry/revocation, and that its owner is active and still has `Can API`. The full secret cannot be recovered; create a replacement token. |
| A workflow does not send email | Check that it is enabled, its owner is active and retains `Can API` and Browse access, exactly one watchdog runs, SMTP is configured, and the condition changed from false to true. Inspect `last_error` through `GET /api/workflows`. |
| A workflow sends no second email while a reading remains above the threshold | This is intentional edge-triggered behavior. The condition must become false before a later true value can fire again. |
| Duplicate alarm or workflow emails | More than one watchdog process is running. |
| Too many or too few reminder emails | Each user sets the notification time on `/profile` (default 60 minutes). |
| Forms return `403 Cross-site request rejected` | The browser's origin host differs from the request host, forwarded host, and `baseUrl` host. Set `baseUrl` to the URL users open and forward `Host`/`X-Forwarded-Host` from the proxy. |
| Map tiles show `403 Access blocked` | Use the canonical default `mapTileUrl`, keep the app's `strict-origin-when-cross-origin` response policy, and ensure a reverse proxy does not replace it with a policy that removes cross-origin referrers. For sustained public traffic, configure a suitable tile provider and its required attribution. |
| Admin is sent to `/change-password` at first login | The account was created with the default or a sample `adminPassword`; choose a strong password. |
| Dashboards are slow | Check the storage latency of `pathStorage` and that files follow `<station>/YYYY/MM/DD/<station>_YYYYMMDDZHH00.csv`; files elsewhere are found but cannot be skipped by date. With InfluxDB enabled, check its query time (`Influx query failed` warnings). |
| No `Install app` option in the browser | Installation needs HTTPS (or `localhost`), and `/manifest.webmanifest` and `/service-worker.js` must be reachable through the proxy. |
| A station disappeared from the home page | Its policy is `restricted` and the current user is not assigned to it. |
| Login returns to home instead of requested URL | `next` must be a local path with one leading slash and no backslashes or control characters. |

See also [WSGI troubleshooting](wsgi.md).
