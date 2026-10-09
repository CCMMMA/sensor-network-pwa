# Documentation

- [Configuration](configuration.md): storage location, web access, InfluxDB reads, email, and watchdog settings.
- [Web GUI and policies](webgui.md): accounts, access rights, dashboards, and charts.
- [Production WSGI](wsgi.md): Gunicorn, reverse proxy, and deployment troubleshooting.
- [Docker deployment](docker.md): web and watchdog services sharing the collector's data volume.
- [Operations](operations.md): process roles, runtime verification, backups, and failure diagnosis.
- [Development and validation](development.md): local setup, tests, and change workflow.
- [Migration](migration.md): replacing the web GUI that used to be part of `sensor-network-collector`.
- [CI/CD](ci-cd.md): checks and GHCR images.

`main.py` holds the application; `webapp_wsgi.py` exposes it to Gunicorn. The data
shown comes from the CSV storage written by
[`sensor-network-collector`](https://github.com/CCMMMA/sensor-network-collector), whose
documentation describes the file layout, units, MQTT ingest, and Signal K forwarding.
