# Development and validation

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Copy `config.json.sample` to `config.json` (ignored by Git) and point `pathStorage`
at a directory holding collector CSV files:

```text
<pathStorage>/<station>/YYYY/MM/DD/<station>_YYYYMMDDZHH00.csv
```

## Automated checks

```bash
python -m compileall -q main.py webapp_wsgi.py tests
python -m unittest discover -s tests -v
```

`tests/test_web_and_auth.py` uses temporary files only; no collector, broker, InfluxDB,
or SMTP server is needed. It covers configuration loading without collector settings,
login redirects, anomaly log access, HTML/script escaping, cross-site write rejection,
cookie and response headers, password policy, single-use tokens, default-admin
handling, sandboxed logo serving, download cleanup, limited row loading, non-finite
values, watchdog resilience, enforced password changes, rows longer than the CSV
header, map popup escaping, session-secret fallback, the onboarding-only username
check, email validation of account requests, hiding of restricted stations, selection
of hourly files, the unchanged-poll shortcut, response compression, chart thinning, and the
progressive web app assets (doctype, manifest, icons, service worker).

## Container checks

```bash
docker build -t sensor-network-pwa:check .
docker run --rm sensor-network-pwa:check python main.py --help
docker run --rm --volume "$PWD/tests:/tests:ro" sensor-network-pwa:check \
  python -m unittest discover -s /tests -v
```

If adding a runtime file, update both `.dockerignore` and the `Dockerfile` copy list.
