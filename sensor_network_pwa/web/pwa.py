"""Progressive web app routes: manifest, service worker, icons and the offline page."""

import json

from flask import abort, render_template, url_for

from sensor_network_pwa.web.pwa_assets import PWA_ICON_SIZES, PWA_THEME_COLOR, build_pwa_icon_png


def register(app, ctx):
    # Used by base.html on every page.
    app.jinja_env.globals.update(pwa_app_name=ctx.cfg["web_app_name"], pwa_theme_color=PWA_THEME_COLOR)

    @app.route("/manifest.webmanifest")
    def pwa_manifest():
        icons = [
            {"src": url_for("pwa_icon", size=size), "sizes": f"{size}x{size}", "type": "image/png", "purpose": purpose}
            for size in (192, 512)
            for purpose in ("any", "maskable")
        ]
        manifest = {
            "name": ctx.cfg["web_app_name"],
            "short_name": ctx.cfg["web_app_short_name"],
            "description": "Sensor network data portal: station map, live dashboards, data browsing and download.",
            "id": url_for("index"),
            "start_url": url_for("index"),
            "scope": url_for("index"),
            "display": "standalone",
            "background_color": "#ffffff",
            "theme_color": PWA_THEME_COLOR,
            "icons": icons,
        }
        response = app.response_class(json.dumps(manifest), mimetype="application/manifest+json")
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.route("/service-worker.js")
    def pwa_service_worker():
        precache = [url_for("pwa_offline"), url_for("pwa_manifest")]
        precache += [url_for("pwa_icon", size=size) for size in PWA_ICON_SIZES]
        script = render_template("service-worker.js", offline_url=url_for("pwa_offline"), precache=precache)
        response = app.response_class(script, mimetype="text/javascript")
        # Browsers must revalidate the worker so that updates are picked up.
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.route("/pwa/icon-<int:size>.png")
    def pwa_icon(size: int):
        if size not in PWA_ICON_SIZES:
            abort(404)
        response = app.response_class(build_pwa_icon_png(size), mimetype="image/png")
        response.headers["Cache-Control"] = "public, max-age=86400"
        return response

    @app.route("/offline")
    def pwa_offline():
        return render_template(
            "offline.html",
            app_name=ctx.cfg["web_app_name"],
        )
