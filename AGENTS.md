# AGENTS.md

## Project Overview
- Repository: `sensor-network-pwa`
- Purpose: web application for the `meteo@uniparthenope` sensor network data portal.
- Data source: the hourly CSV storage (and optionally the InfluxDB bucket) written by the separate `sensor-network-collector` project. This project never talks to MQTT.
- Entry points: `main.py` (development server + watchdog), `webapp_wsgi.py` (Gunicorn)
- Language: Python 3 (3.10+; CI runs 3.10-3.14)

## Setup
1. Create a virtual environment.
2. Install dependencies:
   - `pip install -r requirements.txt`

## Run
- Development server with watchdog:
  - `python main.py --config config.json`
- Production:
  - `PWA_CONFIG=config.json gunicorn webapp_wsgi:app`
  - `python main.py --config config.json --watchdog-only`

## Architecture
`main.py` is organised top to bottom as:
- configuration (`load_config`: camelCase JSON keys with environment fallbacks, normalised to snake_case `cfg` keys)
- `AccessStore`: the SQLite auth database (users, account requests, tokens, station policies, anomalies, logos, chart settings, generated session secret)
- storage readers (`collect_instruments`, `iter_csv_rows`, `load_station_rows`, ...) for `<pathStorage>/<station>/YYYY/MM/DD/<station>_YYYYMMDDZHH00.csv`
- chart and public-dashboard models (`build_public_station_snapshot`, axis helpers)
- anomaly evaluation and the watchdog loop
- progressive web app assets (`PWA_SERVICE_WORKER_JS`, `PWA_BODY_SNIPPET`, `build_pwa_icon_png`)
- `STATION_BROWSER_TEMPLATE`: the station data page (chart setup, SVG/PNG publication export and table controls run in the browser)
- `create_web_app`: every Flask route, with HTML/JS templates inline as `render_template_string`; the `add_pwa_markup` response hook adds the manifest link and service-worker registration to every page
- `main()`; `webapp_wsgi.py` builds the same app for Gunicorn without the watchdog

## Development Guidelines
- Keep changes focused and minimal.
- Prefer small, reviewable commits.
- Do not commit secrets, credentials, or environment-specific values (`config.json` is not tracked; only `config.json.sample` is).
- Update `README.md`, `docs/*.md`, and sample configuration files in the same work item whenever behavior, configuration, routes, UI/UX, deployment, or storage semantics change.
- The CSV layout is owned by `sensor-network-collector` (`docs/storage.md` there); change the readers here only together with that project.
- Read CSV files only through `iter_csv_rows`/`load_station_rows`, so malformed rows are handled in one place.
- Find CSV files through `iter_csv_files_newest_first` with time bounds; never scan a station's whole history (`rglob`) on a request path.
- Routes polled by pages (`/api/public/station/<uuid>/snapshot`, `/api/admin/dashboard`) must stay cheap: check their timing against a large storage tree when changing them.

## Security Conventions
- Station names, field names, and values originate from sensor payloads: treat them as untrusted. In templates rely on Jinja autoescaping or `tojson`; in JavaScript use `textContent`/DOM nodes or `escapeHtml`, never string-built HTML.
- Embed JSON in `<script>` blocks with `json_for_script` or the `tojson` filter.
- New routes must check access with `require_login`/`require_admin` and `station_is_accessible`/`station_is_controllable`. State-changing routes must use `POST`, which the cross-site check covers.
- Build links in templates and scripts with `url_for`; build links for emails with `compose_external_url(cfg["base_url"], ...)`.
- Every page template starts with `<!doctype html>` and needs both `</head>` and `</body>` so the PWA markup is added.
- The service worker must never cache pages or `/api/` responses; bump the `CACHE` name in `PWA_SERVICE_WORKER_JS` when its cached assets change.
- A station's public dashboard and home-page entry must go through `station_is_public`.
- Tokens are single use: claim them with an `UPDATE ... WHERE used_at IS NULL` inside the store lock.

## Validation
- For each change, at minimum:
  - Run `python -m unittest discover -s tests -v`.
  - Ensure `python main.py --config config.json` starts without syntax/runtime import errors.
  - Verify dependency updates are reflected in `requirements.txt`.
- Add a regression test in `tests/test_web_and_auth.py` for every bug fix.
- If a runtime file is added, update both `.dockerignore` and the `Dockerfile` copy list.

## File/Scope Conventions
- Put project-wide runtime logic in `main.py` unless a refactor is explicitly requested.
- Add new modules only when they reduce complexity and improve testability.
