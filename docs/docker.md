# Docker Deployment

## Files

- `Dockerfile`: image whose default command is Gunicorn serving `webapp_wsgi:app` on port 8080
- `docker-compose.yml.sample`: `web` (Gunicorn) and `watchdog` services
- `config.json.sample`
- `nginx/default.conf`: optional local reverse proxy, see [WSGI setup](wsgi.md)

Images contain no configuration or data; provide both through volume mounts.

## Setup

```bash
cp docker-compose.yml.sample docker-compose.yml
cp config.json.sample config.json
```

Use container paths in `config.json`:

```json
{
  "pathStorage": "/data/storage",
  "authDbPath": "/data/collector_auth.sqlite",
  "baseUrl": "https://portal.example.org"
}
```

`./data` in the sample must be the host directory that `sensor-network-collector`
mounts as `/data`. Replace it with an absolute path, or a symlink, when the two
projects are checked out in different directories.

Important:

- Set a strong `webSessionSecret` and `adminPassword`.
- Set `baseUrl` to the public HTTPS URL used by browsers.
- Use network-reachable hostnames for InfluxDB/SMTP, not `localhost`.
- The process user needs write access to `authDbPath` and `<pathStorage>/_logos`.

## Build and start

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f web watchdog
```

- `web` serves the browser-facing application through Gunicorn
- `watchdog` scans stations for anomalies and sends alarm emails; keep a single instance

## Backup

Stop these services and the collector before copying active SQLite/CSV files; see
[operations](operations.md#backup-and-restore).

## Networking

If exposed publicly, run behind Nginx/Caddy/Traefik with TLS and forward only the
`web` port. Forward `Host`/`X-Forwarded-Host` so cross-site form protection accepts
legitimate requests.
