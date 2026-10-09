# Operations

## Process roles

| Process | Command | Role |
| --- | --- | --- |
| Web | `gunicorn webapp_wsgi:app` | Serves the application; any number of workers |
| Watchdog | `python main.py --config <config> --watchdog-only` | Anomaly detection and alarm emails; exactly one |
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

Open `/`, verify station freshness, then log in and check the intended browse and
download permissions. The watchdog logs `Watchdog started (interval=...)`.

## Backup and restore

The SQLite database holds account data; the configuration holds secrets. Keep both
private. For a consistent copy, stop the web and watchdog services (and the collector,
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
| No watchdog notifications | Check that one watchdog process runs, `pathStorage`, SMTP settings, and alarm silencing. A failed scan is logged as `Watchdog scan failed` or `Watchdog check failed for station=...` and retried at the next interval. |
| Duplicate alarm emails | More than one watchdog process is running. |
| Forms return `403 Cross-site request rejected` | The browser's origin host differs from the request host, forwarded host, and `baseUrl` host. Set `baseUrl` to the URL users open and forward `Host`/`X-Forwarded-Host` from the proxy. |
| Admin is sent to `/change-password` at first login | The account was created with the default or a sample `adminPassword`; choose a strong password. |
| Login returns to home instead of requested URL | `next` must be a local path with one leading slash and no backslashes or control characters. |

See also [WSGI troubleshooting](wsgi.md).
