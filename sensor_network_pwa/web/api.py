"""Session- or bearer-authenticated data, token and workflow API."""

import functools

from flask import Blueprint, abort, after_this_request, g, jsonify, render_template, request, send_file

from sensor_network_pwa.storage import load_station_rows, make_zip_for_download
from sensor_network_pwa.timeutil import parse_iso_ts, utc_iso, utc_now
from sensor_network_pwa.validation import safe_filename
from sensor_network_pwa.web.access import available_instruments, current_user, get_store, storage_root_or_404
from sensor_network_pwa.workflows import validate_workflow_graph

bp = Blueprint("api", __name__)
MAX_QUERY_ROWS = 10000


def api_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        authorization = request.headers.get("Authorization", "")
        user = None
        if authorization:
            scheme, separator, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not separator or not token.strip():
                return jsonify(error="invalid Authorization header"), 401
            user = get_store().authenticate_api_token(token.strip())
        else:
            user = current_user()
        if user is None:
            return jsonify(error="authentication required"), 401
        if user.get("force_password_change"):
            return jsonify(error="password change required"), 403
        if user.get("role") != "admin" and not user.get("can_api"):
            return jsonify(error="Can API permission required"), 403
        g.user = user
        return view(*args, **kwargs)

    return wrapped


def session_api_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            return jsonify(error="session authentication required"), 401
        if user.get("force_password_change"):
            return jsonify(error="password change required"), 403
        if user.get("role") != "admin" and not user.get("can_api"):
            return jsonify(error="Can API permission required"), 403
        g.user = user
        return view(*args, **kwargs)

    return wrapped


def workflow_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        authorization = request.headers.get("Authorization", "")
        user = None
        if authorization:
            scheme, separator, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not separator or not token.strip():
                return jsonify(error="invalid Authorization header"), 401
            user = get_store().authenticate_api_token(token.strip())
        else:
            user = current_user()
        if user is None:
            return jsonify(error="authentication required"), 401
        if user.get("force_password_change"):
            return jsonify(error="password change required"), 403
        if user.get("role") != "admin" and not user.get("can_workflows"):
            return jsonify(error="Can workflows permission required"), 403
        g.user = user
        return view(*args, **kwargs)

    return wrapped


def _token_expiry(value):
    text = str(value or "").strip()
    if not text:
        return None
    expires = parse_iso_ts(text)
    if expires is None or expires <= utc_now():
        abort(400, "expires_at must be a future ISO-8601 timestamp")
    return utc_iso(expires)


def _station_list(value):
    return list(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))


def _parse_bounds():
    start = parse_iso_ts(request.args.get("from", ""))
    end = parse_iso_ts(request.args.get("to", ""))
    if request.args.get("from") and start is None or request.args.get("to") and end is None:
        abort(400, "from and to must be ISO-8601 timestamps")
    if start and end and end < start:
        abort(400, "to must be after from")
    return start, end


def _authorized_stations(permission="browse"):
    root = storage_root_or_404()
    requested = _station_list(request.args.get("station", ""))
    if not requested:
        abort(400, "station is required")
    known = set(available_instruments(root))
    checker = get_store().can_download if permission == "download" else get_store().can_browse
    for station in requested:
        if station not in known:
            abort(404, f"station not found: {station}")
        if not checker(g.user, station):
            abort(403, f"station is not {permission}able: {station}")
    return root, requested


@bp.get("/api/query")
@api_required
def query():
    root, stations = _authorized_stations()
    start, end = _parse_bounds()
    try:
        limit = min(MAX_QUERY_ROWS, max(1, int(request.args.get("limit", "1000"))))
    except ValueError:
        abort(400, "limit must be an integer")
    fields = _station_list(request.args.get("fields", ""))
    result = []
    for station in stations:
        rows = load_station_rows(root, station, from_date=start, to_date=end, limit=limit)
        if start or end:
            rows = [
                row
                for row in rows
                if (ts := parse_iso_ts(str(row.get("timestamp") or "")))
                and (not start or ts >= start)
                and (not end or ts <= end)
            ]
        if fields:
            rows = [{key: row.get(key) for key in ("timestamp", *fields) if key in row} for row in rows]
        result.append({"station": station, "rows": rows[-limit:]})
    return jsonify({"from": request.args.get("from"), "to": request.args.get("to"), "stations": result})


@bp.get("/api/download")
@api_required
def download():
    root, stations = _authorized_stations("download")
    start, end = _parse_bounds()
    archive, count = make_zip_for_download(root, stations, from_date=start, to_date=end)
    if count == 0:
        archive.unlink(missing_ok=True)
        abort(404, "no CSV files in the requested interval")

    @after_this_request
    def cleanup(response):
        response.call_on_close(lambda: archive.unlink(missing_ok=True))
        return response

    return send_file(archive, as_attachment=True, download_name=safe_filename("sensor-data.zip"))


def _can_browse(station):
    return get_store().can_browse(g.user, str(station).strip())


@bp.route("/workflows")
@workflow_required
def dashboard():
    root = storage_root_or_404()
    stations = [station for station in available_instruments(root) if get_store().can_browse(g.user, station)]
    return render_template("workflows/dashboard.html", stations=stations)


@bp.get("/api-tokens")
@session_api_required
def token_dashboard():
    return render_template("api_tokens/dashboard.html")


@bp.get("/api/tokens")
@session_api_required
def list_tokens():
    return jsonify({"tokens": get_store().list_api_tokens(g.user["username"])})


@bp.post("/api/tokens")
@session_api_required
def create_token():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 100:
        abort(400, "name is required and must be at most 100 characters")
    return jsonify(
        get_store().create_api_token(g.user["username"], name, _token_expiry(payload.get("expires_at")))
    ), 201


@bp.get("/api/tokens/<int:token_id>")
@session_api_required
def get_token(token_id):
    token = get_store().get_api_token(token_id, g.user["username"])
    if token is None:
        abort(404)
    return jsonify(token)


@bp.put("/api/tokens/<int:token_id>")
@session_api_required
def update_token(token_id):
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 100:
        abort(400, "name is required and must be at most 100 characters")
    if not get_store().update_api_token(token_id, g.user["username"], name, _token_expiry(payload.get("expires_at"))):
        abort(404)
    return jsonify(get_store().get_api_token(token_id, g.user["username"]))


@bp.delete("/api/tokens/<int:token_id>")
@session_api_required
def delete_token(token_id):
    if not get_store().delete_api_token(token_id, g.user["username"]):
        abort(404)
    return "", 204


@bp.get("/api/workflows")
@workflow_required
def list_workflows():
    return jsonify({"workflows": get_store().list_workflows(g.user["username"])})


@bp.post("/api/workflows")
@workflow_required
def create_workflow():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 100:
        abort(400, "name is required and must be at most 100 characters")
    graph = payload.get("graph")
    ok, message = validate_workflow_graph(graph, _can_browse)
    if not ok:
        abort(400, message)
    assert isinstance(graph, dict)
    workflow_id = get_store().save_workflow(g.user["username"], name, graph, bool(payload.get("enabled", True)))
    return jsonify(get_store().get_workflow(workflow_id)), 201


@bp.put("/api/workflows/<int:workflow_id>")
@workflow_required
def update_workflow(workflow_id):
    existing = get_store().get_workflow(workflow_id)
    if not existing or existing["username"] != g.user["username"]:
        abort(404)
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    graph = payload.get("graph")
    ok, message = validate_workflow_graph(graph, _can_browse)
    if not name or len(name) > 100 or not ok:
        abort(400, message if not ok else "invalid name")
    assert isinstance(graph, dict)
    get_store().save_workflow(g.user["username"], name, graph, bool(payload.get("enabled", True)), workflow_id)
    return jsonify(get_store().get_workflow(workflow_id))


@bp.delete("/api/workflows/<int:workflow_id>")
@workflow_required
def delete_workflow(workflow_id):
    if not get_store().delete_workflow(workflow_id, g.user["username"]):
        abort(404)
    return "", 204
