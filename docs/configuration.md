# Configuration Reference

`config.json` is a JSON object. Each key can also be supplied through the environment
variable named beside it; the file takes precedence. Keys that belong only to
`sensor-network-collector` (`mqttBroker`, `storage`, `signalk*`, `httpEnabled`, ...)
are ignored, so a configuration file shared with the collector works unchanged.

## Minimal profile

```json
{
  "pathStorage": "/data/storage",
  "authDbPath": "/data/collector_auth.sqlite",
  "webSessionSecret": "replace-with-strong-secret",
  "adminUser": "admin",
  "adminPassword": "replace-with-strong-password",
  "baseUrl": "https://portal.example.org"
}
```

## Key groups

### Logging and data source

- `logLevel` (`LOG_LEVEL`): `DEBUG|INFO|WARNING|ERROR`
- `pathStorage` (`STORAGE_ROOT`): the collector's CSV storage root. Stations are
  discovered from its sub-directories. The application reads the CSV files and writes
  only station logos, under `<pathStorage>/_logos`. Without it no station is shown and
  the watchdog is disabled.
- `signalkPathMap` (`SIGNALK_PATH_MAP`): optional; only `meta.units` of each entry is
  used, to label fields that have no built-in unit.

### InfluxDB (optional, read-only)

- `influxdb` (`INFLUXDB_ENABLED`): query InfluxDB for public dashboard trends. Defaults
  to `true` when `influxdbUrl` is set, otherwise `false`.
- `influxdbUrl`, `influxdbToken`, `influxdbOrg`, `influxdbBucket`: required when enabled;
  a read-only token is enough
- `influxMeasurement`: measurement written by the collector (default `mqtt_data`)

### Web server and security

- `httpHost`, `httpPort`: bind address of the development server (`python main.py`);
  Gunicorn uses its own `--bind`
- `baseUrl`: public URL used in email links (fast-login, onboarding, password reset);
  its host is also accepted as a valid origin for form submissions. Defaults to
  `http://<httpHost>:<httpPort>`
- `authDbPath`: SQLite file for users, account requests, station policies and
  assignments, chart-control rights, chart axis settings, anomaly log and silence
  windows, station logos. Defaults to `<pathStorage>/collector_auth.sqlite`
- `webSessionSecret`: strong random secret used to sign session cookies. When it is
  empty or still a documented sample value (`replace-with-...`), the application
  generates a random secret once, stores it in the auth database, and logs a warning;
  all workers sharing `authDbPath` then use that stored secret
- `adminUser`, `adminPassword`: used only to create the admin account when it does not
  exist yet; an account created with the default (`admin`) or a sample password must
  change it at first login
- `webAppName` (`WEB_APP_NAME`): application name in the web manifest, shown when the
  app is installed. Defaults to `Sensor Network Data Portal`
- `webAppShortName` (`WEB_APP_SHORT_NAME`): name under the home-screen icon. Defaults
  to `Sensor Network`
- `webAppLogo`: optional absolute path to the app logo image
- `webAppLink`: optional external URL opened when the home-page logo is clicked
- `webInfoLink`: optional external URL shown as `Info` before `Login` on the home page

### SMTP and notifications

- `smtpEnabled`
- `smtpHost`, `smtpPort`
- `smtpUser`, `smtpPass`
- `smtpFrom` (required when `smtpEnabled` is true)
- `smtpUseTls`

Behavior:

- if `smtpHost` is not set, the application does not try to send emails
- if `smtpPort`, `smtpUser`, and `smtpPass` are omitted, SMTP defaults to unauthenticated port `25`
- if `smtpUser` is set, SMTP login is attempted
- `smtpUseTls` can explicitly force TLS behavior; the default follows the port/auth fallback
- onboarding, forgot-password, and alarm emails need both SMTP configuration and a correct `baseUrl`

```json
{
  "smtpEnabled": true,
  "smtpHost": "smtp.mailprovider.net",
  "smtpPort": 587,
  "smtpUser": "noreply@example.org",
  "smtpPass": "smtp-password",
  "smtpFrom": "noreply@example.org",
  "smtpUseTls": true
}
```

### Watchdog

- `watchdogIntervalSec`: scan period for anomaly detection (minimum 10, default 60)

The scan period is not the email period: emails are sent on status changes and at each
user's notification time (default 60 minutes, set on `/profile`). See
[Failure notifications](webgui.md#failure-notifications).

The watchdog runs inside `python main.py`, or alone with `--watchdog-only`. Gunicorn
workers do not run it.

## WSGI

`webapp_wsgi.py` reads the file named by `PWA_CONFIG` (falling back to
`COLLECTOR_CONFIG`, then `config.json`).
