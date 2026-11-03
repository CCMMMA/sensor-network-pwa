"""Progressive web app routes: manifest, service worker, icons and the offline page."""

import json
from pathlib import Path

from flask import Blueprint, Response, abort, render_template, url_for

from sensor_network_pwa.web.access import DEFAULT_ICON, app_icon_path, app_logo_path, current_user, get_cfg, logo_url
from sensor_network_pwa.web.pwa_assets import PWA_ICON_SIZES, resize_icon_png

bp = Blueprint("pwa", __name__)


@bp.app_context_processor
def pwa_template_values():
    """Values that base.html needs on every page."""
    cfg = get_cfg()
    return {
        "pwa_app_name": cfg["web_app_name"],
        "pwa_theme_color": cfg["web_theme_primary"],
        "portal_theme": {
            "primary": cfg["web_theme_primary"],
            "secondary": cfg["web_theme_secondary"],
            "accent": cfg["web_theme_accent"],
        },
        "portal_user": current_user(),
        "portal_logo_url": logo_url(app_logo_path()),
        "portal_info_url": cfg.get("web_info_link"),
    }


@bp.route("/manifest.webmanifest")
def manifest():
    icons = [
        {"src": url_for("pwa.icon", size=size), "sizes": f"{size}x{size}", "type": "image/png", "purpose": purpose}
        for size in (192, 512)
        for purpose in ("any", "maskable")
    ]
    data = {
        "name": get_cfg()["web_app_name"],
        "short_name": get_cfg()["web_app_short_name"],
        "description": "Sensor network data portal: station map, live dashboards, data browsing and download.",
        "id": url_for("public.index"),
        "start_url": url_for("public.index"),
        "scope": url_for("public.index"),
        "display": "standalone",
        "display_override": ["window-controls-overlay", "standalone", "minimal-ui"],
        "orientation": "any",
        "background_color": "#f4f7fa",
        "theme_color": get_cfg()["web_theme_primary"],
        "categories": ["weather", "education", "utilities"],
        "shortcuts": [
            {
                "name": "Station network",
                "short_name": "Stations",
                "url": url_for("public.index") + "#stationsMap",
                "icons": [{"src": url_for("pwa.icon", size=192), "sizes": "192x192", "type": "image/png"}],
            }
        ],
        "icons": icons,
    }
    response = Response(json.dumps(data), mimetype="application/manifest+json")
    response.headers["Cache-Control"] = "no-cache"
    return response


@bp.route("/service-worker.js")
def service_worker():
    precache = [
        url_for("pwa.offline"),
        url_for("pwa.manifest"),
        url_for("static", filename="css/portal.css"),
        url_for("static", filename="css/cookie_consent.css"),
        url_for("static", filename="js/theme.js"),
        url_for("static", filename="js/pwa.js"),
        url_for("static", filename="images/meteo-uniparthenope-logo.png"),
    ]
    precache += [url_for("pwa.icon", size=size) for size in PWA_ICON_SIZES]
    script = render_template("pwa/service-worker.js", offline_url=url_for("pwa.offline"), precache=precache)
    response = Response(script, mimetype="text/javascript")
    # Browsers must revalidate the worker so that updates are picked up.
    response.headers["Cache-Control"] = "no-cache"
    return response


@bp.route("/pwa/icon-v7-<int:size>.png")
def icon(size: int):
    if size not in PWA_ICON_SIZES:
        abort(404)
    custom = Path(app_icon_path()) if app_icon_path() else DEFAULT_ICON
    response = Response(resize_icon_png(custom, size), mimetype="image/png")
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@bp.route("/offline")
def offline():
    return render_template(
        "pwa/offline.html",
        app_name=get_cfg()["web_app_name"],
    )
