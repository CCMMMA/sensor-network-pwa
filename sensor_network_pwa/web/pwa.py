"""Progressive web app routes: manifest, service worker, icons and the offline page."""

import json

from flask import Blueprint, Response, abort, render_template, url_for

from sensor_network_pwa.web.access import get_cfg
from sensor_network_pwa.web.pwa_assets import PWA_ICON_SIZES, PWA_THEME_COLOR, build_pwa_icon_png

bp = Blueprint("pwa", __name__)


@bp.app_context_processor
def pwa_template_values():
    """Values that base.html needs on every page."""
    return {"pwa_app_name": get_cfg()["web_app_name"], "pwa_theme_color": PWA_THEME_COLOR}


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
        "background_color": "#ffffff",
        "theme_color": PWA_THEME_COLOR,
        "icons": icons,
    }
    response = Response(json.dumps(data), mimetype="application/manifest+json")
    response.headers["Cache-Control"] = "no-cache"
    return response


@bp.route("/service-worker.js")
def service_worker():
    precache = [url_for("pwa.offline"), url_for("pwa.manifest")]
    precache += [url_for("pwa.icon", size=size) for size in PWA_ICON_SIZES]
    script = render_template("pwa/service-worker.js", offline_url=url_for("pwa.offline"), precache=precache)
    response = Response(script, mimetype="text/javascript")
    # Browsers must revalidate the worker so that updates are picked up.
    response.headers["Cache-Control"] = "no-cache"
    return response


@bp.route("/pwa/icon-<int:size>.png")
def icon(size: int):
    if size not in PWA_ICON_SIZES:
        abort(404)
    response = Response(build_pwa_icon_png(size), mimetype="image/png")
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@bp.route("/offline")
def offline():
    return render_template(
        "pwa/offline.html",
        app_name=get_cfg()["web_app_name"],
    )
