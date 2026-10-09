"""Administration pages, anomaly log and admin actions."""

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, url_for

from sensor_network_pwa.anomalies import build_admin_network_dashboard
from sensor_network_pwa.config import parse_boolish
from sensor_network_pwa.mailer import compose_external_url, send_email
from sensor_network_pwa.storage import collect_instruments, get_station_preview
from sensor_network_pwa.validation import is_valid_email
from sensor_network_pwa.web.access import (
    admin_required,
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
    instruments = collect_instruments(storage_root) if storage_root else []
    policies = get_store().list_policies(instruments)
    users = get_store().list_users()
    pending_requests = get_store().list_account_requests(status="pending")
    all_access = get_store().list_all_user_instruments()
    all_controls = get_store().list_all_user_station_controls()

    for u in users:
        u["access"] = set(all_access.get(u["username"], []))
        u["controls"] = set(all_controls.get(u["username"], []))
    regular_users = [u for u in users if u["role"] != "admin"]

    stations = []
    for inst in instruments:
        preview = get_station_preview(storage_root, inst)
        stations.append(
            {
                "uuid": inst,
                "name": preview["name"],
                "last_timestamp": preview["last_timestamp"],
                "policy": policies.get(inst, "account"),
                "access_count": sum(1 for u in regular_users if inst in u["access"]),
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
    )


@bp.route("/admin/dashboard")
@admin_required
def dashboard():

    return render_template("admin/admin_dashboard.html")


@bp.route("/api/admin/dashboard")
@admin_required
def dashboard_api():
    storage_root = storage_root_or_404()
    return jsonify(build_admin_network_dashboard(storage_root))


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
        sent = send_email(
            get_cfg(),
            [email],
            "Welcome to Sensor Network Collector",
            f"Hello {username},\n\nYour account has been created.\nFast login link (expires in 60 minutes):\n{link}\n",
        )
        msg += "; a welcome email with a login link was sent" if sent else "; no welcome email was sent"
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
        sent = send_email(
            get_cfg(),
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
            msg = f"Request approved; the onboarding link was emailed to {req['email']}"
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
        request.form.getlist("access"),
        request.form.getlist("control"),
    )
    return admin_done("users", ok, msg)


@bp.route("/admin/station-permissions", methods=["POST"])
@admin_required
def set_station_permissions():
    ok, msg = get_store().replace_station_permissions(
        request.form.get("station_uuid", ""),
        request.form.getlist("user"),
        request.form.getlist("access"),
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
