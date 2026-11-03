"""Administration pages, anomaly log and admin actions."""

import csv
import hashlib
import io
import re
from collections import Counter
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from flask import Blueprint, Response, abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from PIL import Image, UnidentifiedImageError

from sensor_network_pwa.anomalies import build_admin_network_dashboard
from sensor_network_pwa.config import parse_boolish
from sensor_network_pwa.mailer import compose_external_url, queue_email
from sensor_network_pwa.storage import collect_instruments, get_station_preview
from sensor_network_pwa.timeutil import parse_date_ymd, utc_now
from sensor_network_pwa.validation import is_valid_email, safe_filename
from sensor_network_pwa.web.access import (
    admin_required,
    branding_asset_dir,
    get_cfg,
    get_store,
    login_required,
    station_is_accessible,
    storage_root_or_404,
)

bp = Blueprint("admin", __name__)


@bp.route("/admin")
@admin_required
def index():
    admin_user = g.user

    storage_root: str = get_cfg().get("storage_root") or ""
    discovered = collect_instruments(storage_root) if storage_root else []
    registered = {row["station_uuid"]: row for row in get_store().list_registered_stations()}
    instruments = sorted(set(discovered) | set(registered))
    policies = get_store().list_policies(instruments)
    users = get_store().list_users()
    pending_requests = get_store().list_account_requests(status="pending")
    all_browse = get_store().list_all_user_instruments()
    all_download = get_store().list_all_user_download_stations()
    all_controls = get_store().list_all_user_station_controls()
    email_queue = get_store().email_queue_summary()

    for u in users:
        u["browse"] = set(all_browse.get(u["username"], []))
        u["download"] = set(all_download.get(u["username"], []))
        u["controls"] = set(all_controls.get(u["username"], []))
    regular_users = [u for u in users if u["role"] != "admin"]

    stations = []
    for inst in instruments:
        preview = (
            get_station_preview(storage_root, inst) if inst in discovered else {"name": inst, "last_timestamp": None}
        )
        registry = registered.get(inst, {})
        stations.append(
            {
                "uuid": inst,
                "name": registry.get("display_name") or preview["name"],
                "last_timestamp": preview["last_timestamp"],
                "enabled": bool(registry.get("enabled", 1)),
                "has_data": inst in discovered,
                "policy": policies.get(inst, "account"),
                "browse_count": sum(1 for u in regular_users if inst in u["browse"]),
                "download_count": sum(1 for u in regular_users if inst in u["download"]),
                "control_count": sum(1 for u in regular_users if inst in u["controls"]),
            }
        )

    return render_template(
        "admin/admin.html",
        admin_user=admin_user,
        stations=stations,
        users=users,
        regular_users=regular_users,
        pending_requests=pending_requests,
        smtp_ready=bool(get_cfg().get("smtp_enabled") and get_cfg().get("smtp_host")),
        email_queue=email_queue,
        appearance=get_cfg(),
    )


def _appearance_url(value: str) -> bool:
    if not value:
        return True
    parsed = urlsplit(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


@bp.post("/admin/settings/appearance")
@admin_required
def save_appearance():
    values = {
        "web_app_name": request.form.get("web_app_name", "").strip(),
        "web_app_short_name": request.form.get("web_app_short_name", "").strip(),
        "web_app_link": request.form.get("web_app_link", "").strip(),
        "web_theme_primary": request.form.get("web_theme_primary", "").strip().lower(),
        "web_theme_secondary": request.form.get("web_theme_secondary", "").strip().lower(),
        "web_theme_accent": request.form.get("web_theme_accent", "").strip().lower(),
    }
    if not values["web_app_name"] or not values["web_app_short_name"]:
        return admin_done("settings", False, "Brand name and short name are required")
    if not _appearance_url(values["web_app_link"]):
        return admin_done("settings", False, "Brand link must be an http or https URL")
    colour_keys = ("web_theme_primary", "web_theme_secondary", "web_theme_accent")
    if any(not re.fullmatch(r"#[0-9a-f]{6}", values[key]) for key in colour_keys):
        return admin_done("settings", False, "Theme colours must use six-digit hexadecimal values")

    uploads = (
        ("logo", "web_app_logo", (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")),
        ("icon", "web_app_icon", (".png", ".jpg", ".jpeg", ".webp")),
    )
    asset_dir = branding_asset_dir()
    for form_key, setting_key, extensions in uploads:
        if request.form.get(f"reset_{form_key}"):
            values[setting_key] = ""
            continue
        upload = request.files.get(form_key)
        if upload is None or not upload.filename:
            continue
        ext = Path(safe_filename(upload.filename)).suffix.lower()
        if ext not in extensions:
            return admin_done("settings", False, f"Unsupported {form_key} format")
        content = upload.read()
        if not content:
            return admin_done("settings", False, f"The {form_key} file is empty")
        if form_key == "icon":
            try:
                with Image.open(io.BytesIO(content)) as image:
                    image.verify()
            except (UnidentifiedImageError, OSError):
                return admin_done("settings", False, "The application icon is not a valid image")
        digest = hashlib.sha256(content).hexdigest()[:12]
        path = asset_dir / f"{form_key}_{digest}{ext}"
        path.write_bytes(content)
        values[setting_key] = str(path)

    get_store().set_app_settings(values)
    get_cfg().update(values)
    return admin_done("settings", True, "Appearance settings saved")


@bp.route("/admin/dashboard")
@admin_required
def dashboard():

    return render_template("admin/admin_dashboard.html")


@bp.route("/api/admin/dashboard")
@admin_required
def dashboard_api():
    storage_root = storage_root_or_404()
    return jsonify(build_admin_network_dashboard(storage_root))


ACCESS_EVENT_LABELS = {
    "login_success": "Successful sign-in",
    "login_failure": "Failed sign-in",
    "logout": "Sign-out",
    "station_dashboard": "Live dashboard",
    "station_view": "Station data",
    "csv_download": "CSV download",
    "raw_download": "Raw ZIP download",
    "api_query": "API query",
    "api_download": "API download",
}


def _access_log_filters():
    today = utc_now().date()
    start_text = request.args.get("from_date", "") or (today - timedelta(days=29)).isoformat()
    end_text = request.args.get("to_date", "") or today.isoformat()
    start = parse_date_ymd(start_text)
    end = parse_date_ymd(end_text)
    if start is None or end is None or end < start:
        abort(400, "Invalid access-log date range")
    event_type = request.args.get("event_type", "").strip()
    if event_type and event_type not in ACCESS_EVENT_LABELS:
        abort(400, "Unknown access event type")
    return {
        "username": request.args.get("username", "").strip(),
        "station_uuid": request.args.get("station_uuid", "").strip(),
        "event_type": event_type,
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
    }


@bp.get("/admin/access-log")
@admin_required
def access_log():
    filters = _access_log_filters()
    events = get_store().list_access_events(**filters, limit=10000)
    by_day = Counter(item["created_at"][:10] for item in events)
    by_type = Counter(item["event_type"] for item in events)
    by_user = Counter(item["username"] or "Anonymous" for item in events)
    by_station = Counter(item["station_uuid"] for item in events if item["station_uuid"])
    downloads = sum(by_type[k] for k in ("csv_download", "raw_download", "api_download"))
    chart_data = {
        "daily": [{"label": day, "value": by_day[day]} for day in sorted(by_day)],
        "types": [
            {"label": ACCESS_EVENT_LABELS.get(kind, kind), "value": count} for kind, count in by_type.most_common()
        ],
    }
    return render_template(
        "admin/access_log.html",
        filters=filters,
        events=events[:500],
        truncated=len(events) > 500,
        event_labels=ACCESS_EVENT_LABELS,
        total_events=len(events),
        downloads=downloads,
        unique_users=len(by_user),
        unique_stations=len(by_station),
        top_users=by_user.most_common(10),
        top_stations=by_station.most_common(10),
        chart_data=chart_data,
    )


@bp.get("/admin/access-log/export.csv")
@admin_required
def access_log_csv():
    filters = _access_log_filters()
    events = get_store().list_access_events(**filters, limit=10000)
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(("timestamp_utc", "username", "event", "station", "method", "path", "status"))
    for item in events:
        writer.writerow(
            (
                item["created_at"],
                item["username"] or "",
                item["event_type"],
                item["station_uuid"] or "",
                item["method"],
                item["path"],
                item["status_code"],
            )
        )
    response = Response(stream.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = "attachment; filename=access-log.csv"
    return response


@bp.route("/anomalies")
@login_required
def anomalies_log():
    user = g.user
    items = get_store().list_anomalies_for_user(user)
    return render_template(
        "admin/anomalies.html",
        items=items,
    )


@bp.route("/anomalies/silence", methods=["POST"])
@login_required
def silence_anomaly():
    user = g.user
    station_uuid = request.form.get("station_uuid", "").strip()
    anomaly_type = request.form.get("anomaly_type", "").strip()
    try:
        hours = int(request.form.get("hours", "24") or 24)
    except ValueError:
        abort(400, "hours must be an integer")
    if not station_uuid or not anomaly_type:
        abort(400, "Missing station_uuid or anomaly_type")
    if user.get("role") != "admin" and not station_is_accessible(user, station_uuid):
        abort(403)
    get_store().set_anomaly_silence(station_uuid, anomaly_type, user["username"], hours)
    return redirect(url_for("admin.anomalies_log"))


def admin_done(tab: str, ok: bool, message: str):
    """Report the outcome of an admin action on the tab it was started from."""
    flash(message, "success" if ok else "danger")
    return redirect(url_for("admin.index") + f"#{tab}")


@bp.route("/admin/impersonate", methods=["POST"])
@admin_required
def impersonate_user():
    if session.get("impersonator_username"):
        abort(409, "Stop the current impersonation before starting another")
    admin_user = g.user
    username = request.form.get("username", "").strip()
    target = get_store().get_user(username)
    if not target or int(target.get("active") or 0) != 1:
        return admin_done("users", False, "Active user not found")
    if username == admin_user["username"]:
        return admin_done("users", False, "You are already signed in as this user")
    get_store().record_impersonation_event(admin_user["username"], username, "started")
    session["impersonator_username"] = admin_user["username"]
    session["username"] = username
    return redirect(url_for("public.index"))


@bp.route("/impersonation/stop", methods=["POST"])
def stop_impersonation():
    admin_username = str(session.get("impersonator_username") or "")
    target_username = str(session.get("username") or "")
    admin_user = get_store().get_user(admin_username) if admin_username else None
    if not admin_user or admin_user.get("role") != "admin" or int(admin_user.get("active") or 0) != 1:
        session.clear()
        abort(403, "The administrator account is no longer available")
    get_store().record_impersonation_event(admin_username, target_username, "ended")
    session.clear()
    session["username"] = admin_username
    return redirect(url_for("admin.index"))


@bp.route("/admin/create-user", methods=["POST"])
@admin_required
def create_user():

    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "user")

    if email and not is_valid_email(email):
        return admin_done("users", False, "Email address is not valid")
    ok, msg = get_store().create_user(username, password, email, role=role)
    if not ok:
        return admin_done("users", False, msg)
    if parse_boolish(request.form.get("force_password_change"), False):
        get_store().set_force_password_change(username, True)
    msg = f"User {username} created"
    if email:
        token = get_store().create_login_token(username, ttl_minutes=60)
        link = compose_external_url(get_cfg()["base_url"], url_for("auth.fast_login"), {"token": token})
        sent = queue_email(
            get_cfg(),
            get_store(),
            [email],
            "Welcome to Sensor Network Collector",
            f"Hello {username},\n\nYour account has been created.\nFast login link (expires in 60 minutes):\n{link}\n",
        )
        msg += "; a welcome email with a login link was queued" if sent else "; no welcome email was queued"
    return admin_done("users", True, msg)


@bp.route("/admin/requests/<int:request_id>/approve", methods=["POST"])
@admin_required
def approve_request(request_id: int):
    admin_user = g.user
    ok, msg = get_store().approve_request(request_id, admin_user["username"])
    if not ok:
        return admin_done("requests", False, msg)
    req = get_store().get_account_request(request_id)
    if req and req.get("email"):
        token = get_store().create_account_request_token(request_id, ttl_hours=48)
        link = compose_external_url(get_cfg()["base_url"], url_for("auth.complete_account_request"), {"token": token})
        sent = queue_email(
            get_cfg(),
            get_store(),
            [req["email"]],
            "Sensor Network Collector: account approved",
            (
                "Hello,\n\n"
                "Your account request has been approved.\n"
                "Use the link below to complete onboarding and choose your username and password.\n"
                "This link expires in 48 hours.\n\n"
                f"{link}\n"
            ),
        )
        if sent:
            msg = f"Request approved; the onboarding email was queued for {req['email']}"
        else:
            # Without email the link would be lost: hand it to the admin instead.
            msg = (
                f"Request approved, but no email could be sent. Give this onboarding link "
                f"(valid 48 hours) to {req['email']}: {link}"
            )
    return admin_done("requests", True, msg)


@bp.route("/admin/requests/<int:request_id>/reject", methods=["POST"])
@admin_required
def reject_request(request_id: int):
    admin_user = g.user
    ok, msg = get_store().reject_request(request_id, admin_user["username"])
    return admin_done("requests", ok, msg)


@bp.route("/admin/policy", methods=["POST"])
@admin_required
def set_policy():
    admin_user = g.user

    instrument_uuid = request.form.get("instrument_uuid", "")
    policy = request.form.get("policy", "")
    ok, msg = get_store().set_policy(instrument_uuid, policy, admin_user["username"])
    if ok:
        msg = f"Policy of {instrument_uuid.strip()} set to {policy}"
    return admin_done("stations", ok, msg)


@bp.route("/admin/user-access", methods=["POST"])
@admin_required
def set_user_access():

    username = request.form.get("username", "")
    instrument_uuid = request.form.get("instrument_uuid", "")
    allow = parse_boolish(request.form.get("allow", "1"), True)

    ok, msg = get_store().set_user_instrument_access(username, instrument_uuid, allow)
    return admin_done("users", ok, msg)


@bp.route("/admin/user-control", methods=["POST"])
@admin_required
def set_user_control():

    username = request.form.get("username", "")
    station_uuid = request.form.get("station_uuid", "")
    allow = parse_boolish(request.form.get("allow", "1"), True)

    ok, msg = get_store().set_user_station_control(username, station_uuid, allow)
    return admin_done("users", ok, msg)


@bp.route("/admin/user-permissions", methods=["POST"])
@admin_required
def set_user_permissions():
    ok, msg = get_store().replace_user_permissions(
        request.form.get("username", ""),
        request.form.getlist("station"),
        request.form.getlist("browse"),
        request.form.getlist("download"),
        request.form.getlist("control"),
    )
    return admin_done("users", ok, msg)


@bp.route("/admin/user-api", methods=["POST"])
@admin_required
def set_user_api():
    username = request.form.get("username", "")
    allow = parse_boolish(request.form.get("allow", "0"), False)
    ok, msg = get_store().set_can_api(username, allow)
    return admin_done("users", ok, msg)


@bp.route("/admin/user-workflows", methods=["POST"])
@admin_required
def set_user_workflows():
    username = request.form.get("username", "")
    allow = parse_boolish(request.form.get("allow", "0"), False)
    ok, msg = get_store().set_can_workflows(username, allow)
    return admin_done("users", ok, msg)


@bp.route("/admin/station-permissions", methods=["POST"])
@admin_required
def set_station_permissions():
    ok, msg = get_store().replace_station_permissions(
        request.form.get("station_uuid", ""),
        request.form.getlist("user"),
        request.form.getlist("browse"),
        request.form.getlist("download"),
        request.form.getlist("control"),
    )
    return admin_done("stations", ok, msg)


@bp.route("/admin/user-update", methods=["POST"])
@admin_required
def update_user():
    admin_user = g.user
    username = request.form.get("username", "").strip()
    action = request.form.get("action", "")
    actions: dict[str, dict] = {
        "activate": {"active": True},
        "deactivate": {"active": False},
        "make_admin": {"role": "admin"},
        "make_user": {"role": "user"},
        "email": {"email": request.form.get("email", "")},
    }
    changes = actions.get(action)
    if changes is None:
        return admin_done("users", False, "Unknown action")
    if username == admin_user["username"] and action in ("deactivate", "make_user"):
        return admin_done("users", False, "You cannot disable or demote your own account")
    ok, msg = get_store().update_user(username, **changes)
    return admin_done("users", ok, msg)


@bp.post("/admin/user-rename")
@admin_required
def rename_user():
    username = request.form.get("username", "").strip()
    if username == g.user["username"]:
        return admin_done("users", False, "You cannot rename your current account")
    ok, msg = get_store().rename_user(username, request.form.get("new_username", ""))
    return admin_done("users", ok, msg)


@bp.post("/admin/user-delete")
@admin_required
def delete_user():
    username = request.form.get("username", "").strip()
    if username == g.user["username"]:
        return admin_done("users", False, "You cannot delete your current account")
    ok, msg = get_store().delete_user(username)
    return admin_done("users", ok, msg)


@bp.post("/admin/user-notifications")
@admin_required
def user_notifications():
    username = request.form.get("username", "").strip()
    try:
        minutes = int(request.form.get("minutes", "-1"))
    except ValueError:
        minutes = -1
    ok, msg = get_store().set_notification_interval(username, minutes)
    return admin_done("users", ok, msg)


@bp.post("/admin/notifications/stop-all")
@admin_required
def stop_all_notifications():
    count = get_store().stop_all_email_notifications()
    return admin_done("users", True, f"Alert emails stopped for {count} users")


@bp.post("/admin/stations/save")
@admin_required
def save_station():
    station_uuid = request.form.get("station_uuid", "")
    ok, msg = get_store().save_registered_station(
        station_uuid, request.form.get("display_name", ""), g.user["username"]
    )
    if ok:
        ok, msg = get_store().set_policy(station_uuid, request.form.get("policy", "account"), g.user["username"])
    return admin_done("stations", ok, msg)


@bp.post("/admin/stations/delete")
@admin_required
def delete_station():
    ok, msg = get_store().set_registered_station_enabled(
        request.form.get("station_uuid", ""), False, g.user["username"]
    )
    return admin_done("stations", ok, msg)


@bp.post("/admin/stations/restore")
@admin_required
def restore_station():
    ok, msg = get_store().set_registered_station_enabled(request.form.get("station_uuid", ""), True, g.user["username"])
    return admin_done("stations", ok, msg)


@bp.route("/admin/force-password", methods=["POST"])
@admin_required
def force_password():
    username = request.form.get("username", "").strip()
    if not username:
        return admin_done("users", False, "Username is required")
    force = parse_boolish(request.form.get("force", "1"), True)
    ok, msg = get_store().set_force_password_change(username, force)
    if ok:
        msg = (
            f"{username} must choose a new password at the next login"
            if force
            else f"{username} is no longer asked to change password"
        )
    return admin_done("users", ok, msg)


@bp.route("/admin/set-password", methods=["POST"])
@admin_required
def set_password():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    password2 = request.form.get("password2", "")
    if password != password2:
        return admin_done("users", False, "Passwords do not match")
    force = parse_boolish(request.form.get("force_password_change"), False)
    ok, msg = get_store().change_password(username, password, force_password_change=force)
    if ok:
        msg = f"Password for {username} updated"
        if force:
            msg += "; a new password will be required at the next login"
    return admin_done("users", ok, msg)
