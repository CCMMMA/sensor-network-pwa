# Docker Deployment

## Files

- `Dockerfile`: image whose default command is Gunicorn serving `webapp_wsgi:app` on port 8080
- `docker-compose.yml.sample`: PostgreSQL, `web` (Gunicorn), and `watchdog` services
- `config.json.sample`
- `nginx/default.conf`: optional local reverse proxy, see [WSGI setup](wsgi.md)

Images contain no configuration or data; provide both through volume mounts.
The image pins its official Python base by multi-architecture digest and runs as the
unprivileged `10001:10001` user. Dependabot proposes digest and dependency updates.

## Setup

```bash
cp docker-compose.yml.sample docker-compose.yml
printf 'POSTGRES_PASSWORD=replace-with-a-long-random-secret\n' > .env
cp config.json.sample config.json
```

Use container paths in `config.json`:

```json
{
  "pathStorage": "/data/storage",
  "databaseUrl": "",
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
- Compose injects `DATABASE_URL` for PostgreSQL. Do not commit `.env`; use Docker secrets or
  your orchestrator's secret store in a production cluster.
- The process user needs write access to `<pathStorage>/_logos` and
  `<pathStorage>/_branding`.
- On Linux, make writable bind mounts accessible to UID/GID 10001, for example
  `sudo chown -R 10001:10001 ./data`. Do not make the directory world-writable.

## Build and start

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f web watchdog
```

For a release deployment, replace each `build:` block with the immutable image
published by CI, preferably its SHA tag:

```yaml
image: ghcr.io/ccmmma/sensor-network-pwa:sha-<git-commit>
```

- `web` serves the browser-facing application through Gunicorn
- `watchdog` scans stations for anomalies, evaluates enabled workflows, and sends
  alarm/workflow emails; keep a single instance

The image includes the repository `docs/` directory so the web service can render the
canonical workflow and API reference at `/docs/workflows-api`.
Docker reports the web container healthy only when `/health` can read the auth
database, access configured CSV storage, and ping configured InfluxDB. The watchdog
service disables this HTTP health check because it does not run a web server.

## Backup

Use the online SQLite backup and non-destructive restore drill:

```bash
docker compose exec web python -m sensor_network_pwa.maintenance \
  --config /app/config.json backup /data/backups/auth-$(date +%F).sqlite
docker compose exec web python -m sensor_network_pwa.maintenance \
  --config /app/config.json drill
```

See [operations](operations.md#backup-and-restore) for the full restore procedure.

## Networking

If exposed publicly, run behind Nginx/Caddy/Traefik with TLS and forward only the
`web` port. Forward `Host`/`X-Forwarded-Host` so cross-site form protection accepts
legitimate requests.
