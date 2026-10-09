# Migrating from the web GUI embedded in sensor-network-collector

Older versions of `sensor-network-collector` served this application themselves, either
from the collector process (`httpEnabled: true`) or from a Gunicorn service running
`webapp_wsgi:app` out of the collector image. This project replaces both. No data
conversion is needed.

## What to reuse

- **Configuration file**: the collector's `config.json` works unchanged. Collector-only
  keys are ignored, and `httpEnabled` is not required.
- **Data directory**: mount or point `pathStorage` at the directory the collector writes.
- **Database**: keep `authDbPath` as it is. When it is not set, the default is still
  `<pathStorage>/collector_auth.sqlite`, so accounts, policies, chart settings, logos,
  and the anomaly log are kept.
- **`webSessionSecret`**: keep the same value so users stay logged in.
- **Environment**: `PWA_CONFIG` names the configuration file for Gunicorn;
  `COLLECTOR_CONFIG` is still accepted.

## What changes

- The anomaly watchdog used to run inside the collector process. It is now a process of
  this project: `python main.py --config <config> --watchdog-only`, exactly one
  instance. Without it no alarm emails are sent.
- The reverse proxy must forward to this project's `web` service.

## Steps (Docker Compose)

1. Update the collector first, following `docs/upgrading.md` in
   `sensor-network-collector`; its updater leaves the old web service running.
2. In this checkout, create `docker-compose.yml` and `config.json` as described in
   [Docker deployment](docker.md), using the collector's configuration file and the
   absolute path of its data directory.
3. Start `web` and `watchdog` here. The old web service may keep running during the
   switch (on another host port): both use the same SQLite file safely.
4. Point the reverse proxy at the new `web` service and check login, a station page,
   and the watchdog log line `Watchdog started`.
5. Remove the old web service from the collector's `docker-compose.yml`.
