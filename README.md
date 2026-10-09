# sensor-network-pwa

Web application for navigating the meteo@uniparthenope sensor network data portal.

It was split out of [`sensor-network-collector`](https://github.com/CCMMMA/sensor-network-collector).
The collector ingests MQTT and writes hourly CSV files (and optionally InfluxDB); this
project only reads that data and serves it. It never connects to MQTT.

## Features

- Installable progressive web app (web manifest, service worker, offline page) built on Bootstrap
- Map-based station discovery on the home page, with per-station popups
- Data browsing per station: searchable parameter picker, shared axes per unit, paged table, statistics
- Publication-quality plots (vector SVG, PNG up to 1200 dpi) and CSV download of any time range
- Live station trend pages and a public sensor network dashboard, reading only the hourly files a view needs
- Public station dashboard with per-station trend-chart axis settings (JSON export/import)
- Data download (ZIP) with authentication and per-station access policies
- Accounts: admin approval of requests, forced/self-service password change, reset by email
- Administration page with per-user and per-station rights tables, roles, and account enable/disable
- Watchdog anomaly detection with a persisted anomaly log and email notifications of status changes (failure, back to regular), with reminders at a per-user notification time
- Profile page where each user sets the notification time and acknowledges, snoozes, or clears notifications
- Web app logo and per-station logo upload
- Optional InfluxDB v2 reads that complement the CSV files on the public dashboards

## Requirements

- Python 3.10+ (CI checks 3.10–3.14; Docker uses 3.11)
- Read access to the collector's `pathStorage` directory, with write access to its
  `_logos` subdirectory for station logo uploads
- A writable location for the SQLite database (`authDbPath`)
- Optional: the collector's InfluxDB v2 bucket (read token), an SMTP relay

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
cp config.json.sample config.json
python3 main.py --config config.json
```

This starts the Flask development server on `httpHost:httpPort` together with the
anomaly watchdog. Options:

- `--config <path>` config file path (default `config.json`)
- `--watchdog-only` run only the watchdog, for deployments where Gunicorn serves the web application

Production:

```bash
PWA_CONFIG=/path/config.json gunicorn --workers 2 --bind 127.0.0.1:8080 webapp_wsgi:app
python3 main.py --config /path/config.json --watchdog-only
```

Run exactly one watchdog process: Gunicorn workers do not start it.

With Docker, copy `docker-compose.yml.sample` to `docker-compose.yml`; it runs the web
and watchdog services from the same image (see [Docker deployment](docs/docker.md)).

## Configuration

`config.json` is a JSON object; `config.json.sample` lists every key. The essential ones:

| Key | Purpose |
| --- | --- |
| `pathStorage` | Storage root written by the collector (read here, plus `_logos` for uploads) |
| `authDbPath` | SQLite file for accounts, policies, anomalies, chart settings |
| `baseUrl` | URL users open; used in email links and to accept form submissions |
| `webSessionSecret` | Secret signing the session cookies |
| `webAppName`, `webAppShortName` | Names shown when the app is installed |
| `adminUser`, `adminPassword` | Initial admin account, created only if missing |
| `influxdb`, `influxdb*` | Optional InfluxDB v2 read access |
| `smtp*` | Optional email delivery (alarms, onboarding, password reset) |
| `watchdogIntervalSec` | Seconds between anomaly scans (minimum 10) |

Most keys can also come from environment variables; see the
[configuration reference](docs/configuration.md).

## Repository layout

```text
main.py                    application: config, auth store, CSV readers, watchdog, Flask routes and templates
webapp_wsgi.py             Gunicorn entry point (webapp_wsgi:app)
tests/                     unittest suite (temporary files only, no external services)
docs/                      configuration, web GUI, deployment and operations guides
config.json.sample         sample configuration
Dockerfile, docker-compose.yml.sample, nginx/   container deployment
.github/workflows/ci-cd.yml                     tests on Python 3.10-3.14, image build and GHCR publish
```

## Sharing data with the collector

```text
MQTT -> sensor-network-collector -> CSV files under pathStorage (+ InfluxDB)
                                      -> sensor-network-pwa (this project)
```

Set `pathStorage` to the directory the collector writes to. A `config.json` used by a
collector version that still embedded the web GUI can be reused unchanged: collector-only
keys (`mqttBroker`, `signalk*`, ...) are ignored here, and the default database file is
still `<pathStorage>/collector_auth.sqlite`, so existing accounts and policies are kept.

## Documentation

- [Documentation index](docs/README.md)
- [Configuration reference](docs/configuration.md)
- [Web GUI and policies](docs/webgui.md)
- [Production WSGI setup](docs/wsgi.md)
- [Docker deployment](docs/docker.md)
- [Operations and troubleshooting](docs/operations.md)
- [Development and validation](docs/development.md)
- [Continuous integration and image delivery](docs/ci-cd.md)
- [Migrating from the GUI embedded in sensor-network-collector](docs/migration.md)

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Security notes

- Change `adminPassword` immediately; an admin created with the default or a sample password
  cannot use the application until the password is changed. The same applies to users
  an admin flags for a password change.
- Set `baseUrl` to the URL users open: form submissions from other hosts are rejected.
- Set a strong `webSessionSecret` in production. An empty or sample value is never used
  to sign sessions: a random secret is generated and kept in the auth database instead.
- Serve the application behind TLS/a reverse proxy.
- Public station dashboards (`/public/station/<uuid>`) are readable without login for
  `open` and `account` stations. `restricted` stations are hidden from everyone except
  their assigned users and admins.
- The service worker caches only static assets; pages and API data are never stored
  on the device.
- Login attempts are not rate limited by the application; add a limit at the reverse
  proxy when the portal is exposed to the Internet.
- Pages load Bootstrap, Leaflet, and Chart.js from public CDNs and map tiles from
  OpenStreetMap, so browsers need Internet access.

## License

See [LICENSE](LICENSE).
