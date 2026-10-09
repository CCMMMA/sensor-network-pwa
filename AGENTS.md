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
`main.py` only calls `sensor_network_pwa.cli.main`. The `sensor_network_pwa` package holds the application:
- `config.py`: `load_config` (camelCase JSON keys with environment fallbacks, normalised to snake_case `cfg` keys), field units
- `log.py`, `runtime.py`, `timeutil.py`, `validation.py`, `mailer.py`: logging setup, process-wide state, UTC helpers, input validation, email
- `access_store/`: `AccessStore`, the SQLite auth database, built from one mixin per concern over `StoreBase` (`users.py`, `account_requests.py`, `permissions.py`, `tokens.py`, `notifications.py`, `anomalies.py`, `station_settings.py`); `schema.py` holds the tables and column migrations
- `storage.py`: storage readers (`collect_instruments`, `iter_csv_rows`, `load_station_rows`, ...) for `<pathStorage>/<station>/YYYY/MM/DD/<station>_YYYYMMDDZHH00.csv`
- `intervals.py`, `charts.py`, `dashboard.py`, `influx.py`: time windows, chart catalogue and axis helpers, the public-dashboard model (`build_public_station_snapshot`), optional InfluxDB queries
- `anomalies.py`, `watchdog.py`: anomaly evaluation, the watchdog loop and failure notifications
- `cli.py`: argument parsing, signal handling and `main()`; `webapp_wsgi.py` builds the same app for Gunicorn without the watchdog
- `web/app.py`: `create_web_app`, which builds a `WebContext` (`web/context.py`: `cfg`, `access_store`, `current_user`, `require_login`/`require_admin`, `station_is_*`) and calls `register(app, ctx)` of each route module
- `web/hooks.py`: request hooks (cross-site write rejection, enforced password change, security headers, gzip compression)
- `web/pwa.py`, `web/public.py`, `web/stations.py`, `web/auth.py`, `web/admin.py`, `web/profile.py`: the Flask routes, grouped by area. Endpoint names are the plain function names (no blueprints)
- `web/pwa_assets.py`: theme colour and generated icons (`build_pwa_icon_png`); the service worker is the `web/templates/service-worker.js` template
- `web/templates/*.html`: one template per page, rendered with `render_template`; each extends `base.html`, which provides the document head, the PWA tags and the offline/install controls
- `web/static/js/*.js`, `web/static/css/*.css`: the script and stylesheet of each page (`station_browser.js`: chart setup, SVG/PNG publication export and table controls; `pwa.js`: service-worker registration, loaded by `base.html`)

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
- Every page template extends `base.html` (doctype, manifest link, icons, service-worker registration); do not write a standalone HTML document.
- Page behaviour goes in `web/static/js/<page>.js`, never in an inline `<script>`. Static files are not templates: pass server values through `<script id="pageConfig" type="application/json">{{ {...} | tojson }}</script>` and read them with `JSON.parse`.
- The service worker must never cache pages or `/api/` responses; bump the `CACHE` name in `web/templates/service-worker.js` when its cached assets change.
- A station's public dashboard and home-page entry must go through `station_is_public`.
- Tokens are single use: claim them with an `UPDATE ... WHERE used_at IS NULL` inside the store lock.

## Validation
- For each change, at minimum:
  - Run `python -m unittest discover -s tests -v`.
  - Run `ruff check .`, `ruff format --check .` and `mypy` (installed by `pip install -r requirements-dev.txt`; configured in `pyproject.toml`).
  - Ensure `python main.py --config config.json` starts without syntax/runtime import errors, and run `python -m compileall -q main.py webapp_wsgi.py sensor_network_pwa tests`.
  - Verify dependency updates are reflected in `requirements.txt`.
- Add a regression test in `tests/test_web_and_auth.py` for every bug fix.
- If a runtime file is added outside `sensor_network_pwa/`, update both `.dockerignore` and the `Dockerfile` copy list.

## File/Scope Conventions
- Put new logic in the `sensor_network_pwa` module that owns the concern; keep `main.py` and `webapp_wsgi.py` as thin entry points.
- Add new modules only when they reduce complexity and improve testability, and keep the package free of import cycles (web modules import the core modules, never the reverse).
- New routes go in the matching `web/*.py` module inside its `register(app, ctx)`; a new route module must be added to `ROUTE_MODULES` in `web/app.py`.
