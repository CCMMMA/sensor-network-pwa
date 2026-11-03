"""Public pages: home page, documentation, station dashboards and image assets."""

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import markdown
from flask import Blueprint, abort, jsonify, render_template, request, send_file, url_for

from sensor_network_pwa.config import parse_boolish
from sensor_network_pwa.dashboard import build_public_station_snapshot
from sensor_network_pwa.intervals import PUBLIC_TREND_WINDOWS, normalize_public_window
from sensor_network_pwa.runtime import runtime
from sensor_network_pwa.storage import get_station_preview
from sensor_network_pwa.timeutil import parse_iso_ts
from sensor_network_pwa.validation import safe_filename
from sensor_network_pwa.web.access import (
    app_logo_path,
    available_instruments,
    current_user,
    get_cfg,
    get_store,
    logo_url,
    redirect_to_login,
    send_image_asset,
    station_is_accessible,
    station_is_controllable,
    station_is_downloadable,
    station_is_public,
    station_logo_dir,
    storage_root_or_404,
)
from sensor_network_pwa.web.util import request_preference_cookie

bp = Blueprint("public", __name__)
DOCS_ROOT = Path(__file__).resolve().parents[2] / "docs"
DOC_PAGES = (
    "README",
    "getting-started",
    "joining-the-network",
    "using-data",
    "workflows-api",
    "webgui",
    "architecture",
    "configuration",
    "operations",
    "docker",
    "wsgi",
    "data-protection",
    "development",
    "ci-cd",
    "migration",
)
DOC_ASSET_SUFFIXES = {".jpg", ".jpeg", ".png", ".svg", ".webp", ".gif"}


@bp.get("/health")
def health():
    """Readiness check for the database, CSV storage and optional InfluxDB."""
    checks = {}
    try:
        checks["database"] = "ok" if get_store().healthcheck() else "error"
    except Exception:
        checks["database"] = "error"

    storage_root = get_cfg().get("storage_root")
    checks["storage"] = (
        "disabled"
        if not storage_root
        else "ok"
        if Path(storage_root).is_dir() and os.access(storage_root, os.R_OK | os.X_OK)
        else "error"
    )

    if not get_cfg().get("enable_influx"):
        checks["influxdb"] = "disabled"
    else:
        client = runtime.get("influx_client")
        try:
            checks["influxdb"] = "ok" if client is not None and client.ping() else "error"
        except Exception:
            checks["influxdb"] = "error"

    healthy = all(status in ("ok", "disabled") for status in checks.values())
    response = jsonify(status="ok" if healthy else "error", checks=checks)
    response.status_code = 200 if healthy else 503
    response.headers["Cache-Control"] = "no-store"
    return response


def _documentation_pages():
    pages = []
    for slug in DOC_PAGES:
        source = DOCS_ROOT / f"{slug}.md"
        if not source.is_file():
            continue
        text = source.read_text(encoding="utf-8")
        title_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else slug.replace("-", " ").title()
        pages.append({"slug": slug, "title": title, "url": url_for("public.documentation", slug=slug)})
    return pages


def _rewrite_doc_links(content: str) -> str:
    """Point relative Markdown links at the online docs routes and assets."""

    def replace(match):
        attribute, quote, target = match.groups()
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or target.startswith(("/", "#", "mailto:")):
            return match.group(0)
        path = parsed.path
        if path.endswith(".md"):
            destination = Path(path).stem
            href = url_for("public.documentation", slug=destination)
        elif Path(path).suffix.lower() in DOC_ASSET_SUFFIXES:
            href = url_for("public.documentation_asset", name=path)
        else:
            return match.group(0)
        if parsed.query:
            href += f"?{parsed.query}"
        if parsed.fragment:
            href += f"#{parsed.fragment}"
        return f"{attribute}={quote}{href}{quote}"

    return re.sub(r"\b(href|src)=(['\"])([^'\"]+)\2", replace, content)


@bp.get("/docs")
@bp.get("/docs/<slug>")
def documentation(slug: str = "README"):
    """Render a canonical repository Markdown guide inside the PWA."""
    if slug not in DOC_PAGES:
        abort(404)
    source = DOCS_ROOT / f"{slug}.md"
    if not source.is_file():
        abort(404, "Documentation is not installed")
    content = markdown.markdown(
        source.read_text(encoding="utf-8"),
        extensions=("fenced_code", "tables", "toc", "sane_lists"),
    )
    pages = _documentation_pages()
    current = next(page for page in pages if page["slug"] == slug)
    return render_template(
        "public/documentation.html",
        content=_rewrite_doc_links(content),
        pages=pages,
        current=current,
    )


@bp.get("/docs/assets/<path:name>")
def documentation_asset(name: str):
    """Serve media directly from docs, the canonical documentation tree."""
    target = (DOCS_ROOT / name).resolve()
    is_allowed = DOCS_ROOT.resolve() in target.parents and target.suffix.lower() in DOC_ASSET_SUFFIXES
    if not is_allowed or not target.is_file():
        abort(404)
    return send_file(target, conditional=True)


@bp.get("/api/docs/search")
def documentation_search():
    """Search canonical Markdown without maintaining a separate index."""
    query = str(request.args.get("q") or "").strip().lower()
    if len(query) < 2:
        return jsonify({"results": []})
    terms = query.split()[:8]
    results = []
    for page in _documentation_pages():
        source = (DOCS_ROOT / f"{page['slug']}.md").read_text(encoding="utf-8")
        searchable = source.lower()
        if not all(term in searchable for term in terms):
            continue
        first = min(searchable.find(term) for term in terms)
        start = max(0, first - 90)
        end = min(len(source), first + 210)
        snippet = re.sub(r"[`#*|>\[\]()]", " ", source[start:end])
        snippet = re.sub(r"\s+", " ", snippet).strip()
        results.append({"title": page["title"], "url": page["url"], "snippet": snippet})
    return jsonify({"results": results[:12]})


@bp.get("/privacy")
def privacy_notice():
    """Human-readable information about the portal's personal-data processing."""
    return render_template("public/privacy.html", cfg=get_cfg())


@bp.route("/assets/<kind>/<path:name>")
def asset_file(kind: str, name: str):
    if kind == "app":
        from sensor_network_pwa.web.access import DEFAULT_LOGO

        p = Path(app_logo_path()) if app_logo_path() else DEFAULT_LOGO
        if not p.is_file() or p.name != name:
            abort(404)
        return send_image_asset(p)

    if kind == "station":
        p = station_logo_dir() / safe_filename(name)
        if not p.is_file():
            abort(404)
        return send_image_asset(p)
    abort(404)


@bp.route("/")
def index():
    user = current_user()
    cfg = get_cfg()
    storage_root: str = cfg.get("storage_root") or ""
    instruments = available_instruments(storage_root) if storage_root else []
    policies = get_store().list_policies(instruments)

    stations = []
    for inst in instruments:
        can_browse = station_is_accessible(user, inst)
        can_download = station_is_downloadable(user, inst)
        if policies.get(inst) == "restricted" and not (can_browse or can_download):
            continue
        preview = get_station_preview(storage_root, inst)
        stations.append(
            {
                "uuid": inst,
                "name": get_store().station_display_name(inst, preview["name"]),
                "policy": policies.get(inst, "account"),
                "can_access": can_browse,
                "can_download": can_download,
                "latitude": preview["latitude"],
                "longitude": preview["longitude"],
                "last_timestamp": preview["last_timestamp"],
                "rows": preview["rows"],
                "browse_url": url_for("stations.browse_station", instrument_uuid=inst),
                "public_url": url_for("public.station", instrument_uuid=inst),
            }
        )

    # Map center based on first station with known coordinates.
    center = {"lat": 40.0, "lon": 14.0, "zoom": 6}
    for st in stations:
        if st["latitude"] is not None and st["longitude"] is not None:
            center = {"lat": st["latitude"], "lon": st["longitude"], "zoom": 9}
            break

    clickable = [s for s in stations if s["can_access"]]

    return render_template(
        "public/index.html",
        user=user,
        storage_root=storage_root,
        stations=stations,
        clickable=clickable,
        center=center,
        app_logo_url=logo_url(app_logo_path()),
        web_app_link=cfg.get("web_app_link"),
        web_info_link=cfg.get("web_info_link"),
        map_tile_url=cfg["map_tile_url"],
        map_tile_attribution=cfg["map_tile_attribution"],
    )


@bp.route("/public/station/<path:instrument_uuid>")
def station(instrument_uuid: str):
    storage_root = storage_root_or_404()
    instruments = set(available_instruments(storage_root))
    if instrument_uuid not in instruments:
        abort(404, "Station not found")

    user = current_user()
    if not station_is_public(user, instrument_uuid):
        if user is None:
            return redirect_to_login("Please log in to view this station.")
        abort(404, "Station not found")
    can_browse = station_is_accessible(user, instrument_uuid)
    can_download = station_is_downloadable(user, instrument_uuid)
    can_control = bool(user) and station_is_controllable(user, instrument_uuid)
    selected_window = request_preference_cookie(
        request, "window", "public_trend_window", normalize_public_window, "hour"
    )
    selected_focus = str(request.args.get("focus", "") or "").strip()
    snapshot = build_public_station_snapshot(
        storage_root,
        instrument_uuid,
        window=selected_window,
        cfg=get_cfg(),
        access_store=get_store(),
    )
    snapshot["station_name"] = get_store().station_display_name(
        instrument_uuid, snapshot.get("station_name") or instrument_uuid
    )
    station_logo_row = get_store().get_station_logo(instrument_uuid)
    station_logo_url = None
    if station_logo_row:
        station_logo_url = url_for("public.asset_file", kind="station", name=Path(station_logo_row["logo_path"]).name)
    return render_template(
        "public/public_station.html",
        snapshot=snapshot,
        can_browse=can_browse,
        can_download=can_download,
        can_control=can_control,
        app_logo_url=logo_url(app_logo_path()),
        station_logo_url=station_logo_url,
        selected_window=selected_window,
        selected_focus=selected_focus,
        window_options=PUBLIC_TREND_WINDOWS,
    )


@bp.route("/api/public/station/<path:instrument_uuid>/snapshot")
def station_snapshot(instrument_uuid: str):
    storage_root = storage_root_or_404()
    instruments = set(available_instruments(storage_root))
    if instrument_uuid not in instruments or not station_is_public(current_user(), instrument_uuid):
        abort(404, "Station not found")

    window = normalize_public_window(request.args.get("window", "hour"))
    since = request.args.get("since", "")
    force = parse_boolish(request.args.get("force", "0"), False)
    since_dt = parse_iso_ts(since)
    if since_dt is not None and not force:
        # Most polls arrive before the station has sent new data: answer those
        # from the latest stored row instead of rebuilding the whole snapshot.
        latest = get_station_preview(storage_root, instrument_uuid).get("last_timestamp")
        if parse_iso_ts(latest or "") == since_dt:
            return jsonify({"changed": False})
    snapshot = build_public_station_snapshot(
        storage_root,
        instrument_uuid,
        window=window,
        cfg=get_cfg(),
        access_store=get_store(),
    )
    snapshot["station_name"] = get_store().station_display_name(
        instrument_uuid, snapshot.get("station_name") or instrument_uuid
    )
    changed = bool(snapshot.get("last_timestamp")) and snapshot.get("last_timestamp") != since
    if force:
        changed = True
    if not since:
        changed = True
    return jsonify({"changed": changed, "snapshot": snapshot})
