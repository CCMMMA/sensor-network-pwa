"""Public pages: home page, public station dashboard and static image assets."""

from pathlib import Path

from flask import abort, jsonify, render_template, request, url_for

from sensor_network_pwa.config import parse_boolish
from sensor_network_pwa.dashboard import build_public_station_snapshot
from sensor_network_pwa.intervals import PUBLIC_TREND_WINDOWS, normalize_public_window
from sensor_network_pwa.storage import get_station_preview
from sensor_network_pwa.timeutil import parse_iso_ts
from sensor_network_pwa.validation import safe_filename
from sensor_network_pwa.web.util import request_preference_cookie


def register(app, ctx):
    @app.route("/assets/<kind>/<path:name>")
    def asset_file(kind: str, name: str):
        if kind == "app":
            p = Path(ctx.app_logo_path) if ctx.app_logo_path else None
            if p is None or not p.is_file() or p.name != name:
                abort(404)
            return ctx.send_image_asset(p)

        if kind == "station":
            p = ctx.station_logo_dir() / safe_filename(name)
            if not p.is_file():
                abort(404)
            return ctx.send_image_asset(p)
        abort(404)

    @app.route("/")
    def index():
        user = ctx.current_user()
        storage_root = ctx.cfg.get("storage_root")
        instruments = ctx.available_instruments(storage_root) if storage_root else []
        policies = ctx.access_store.list_policies(instruments)

        stations = []
        for inst in instruments:
            can_access = ctx.station_is_accessible(user, inst)
            if policies.get(inst) == "restricted" and not can_access:
                continue
            preview = get_station_preview(storage_root, inst)
            stations.append(
                {
                    "uuid": inst,
                    "name": preview["name"],
                    "policy": policies.get(inst, "account"),
                    "can_access": can_access,
                    "latitude": preview["latitude"],
                    "longitude": preview["longitude"],
                    "last_timestamp": preview["last_timestamp"],
                    "rows": preview["rows"],
                    "browse_url": url_for("browse_station", instrument_uuid=inst),
                    "public_url": url_for("public_station", instrument_uuid=inst),
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
            "index.html",
            user=user,
            storage_root=storage_root,
            stations=stations,
            clickable=clickable,
            center=center,
            app_logo_url=ctx.logo_url(ctx.app_logo_path),
            web_app_link=ctx.cfg.get("web_app_link"),
            web_info_link=ctx.cfg.get("web_info_link"),
        )

    @app.route("/public/station/<path:instrument_uuid>")
    def public_station(instrument_uuid: str):
        storage_root = ctx.storage_root_or_404()
        instruments = set(ctx.available_instruments(storage_root))
        if instrument_uuid not in instruments:
            abort(404, "Station not found")

        user = ctx.current_user()
        if not ctx.station_is_public(user, instrument_uuid):
            if user is None:
                return ctx.redirect_to_login("Please log in to view this station.")
            abort(404, "Station not found")
        can_browse_download = bool(user) and ctx.station_is_accessible(user, instrument_uuid)
        can_control = bool(user) and ctx.station_is_controllable(user, instrument_uuid)
        selected_window = request_preference_cookie(
            request, "window", "public_trend_window", normalize_public_window, "hour"
        )
        selected_focus = str(request.args.get("focus", "") or "").strip()
        snapshot = build_public_station_snapshot(
            storage_root,
            instrument_uuid,
            window=selected_window,
            cfg=ctx.cfg,
            access_store=ctx.access_store,
        )
        station_logo_row = ctx.access_store.get_station_logo(instrument_uuid)
        station_logo_url = None
        if station_logo_row:
            station_logo_url = url_for("asset_file", kind="station", name=Path(station_logo_row["logo_path"]).name)
        return render_template(
            "public_station.html",
            snapshot=snapshot,
            can_browse_download=can_browse_download,
            can_control=can_control,
            app_logo_url=ctx.logo_url(ctx.app_logo_path),
            station_logo_url=station_logo_url,
            selected_window=selected_window,
            selected_focus=selected_focus,
            window_options=PUBLIC_TREND_WINDOWS,
        )

    @app.route("/api/public/station/<path:instrument_uuid>/snapshot")
    def public_station_snapshot(instrument_uuid: str):
        storage_root = ctx.storage_root_or_404()
        instruments = set(ctx.available_instruments(storage_root))
        if instrument_uuid not in instruments or not ctx.station_is_public(ctx.current_user(), instrument_uuid):
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
            cfg=ctx.cfg,
            access_store=ctx.access_store,
        )
        changed = bool(snapshot.get("last_timestamp")) and snapshot.get("last_timestamp") != since
        if force:
            changed = True
        if not since:
            changed = True
        return jsonify({"changed": changed, "snapshot": snapshot})
