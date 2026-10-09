"""Administration pages, anomaly log and admin actions."""

from flask import abort, flash, jsonify, redirect, render_template, request, url_for

from sensor_network_pwa.anomalies import build_admin_network_dashboard
from sensor_network_pwa.config import parse_boolish
from sensor_network_pwa.mailer import compose_external_url, send_email
from sensor_network_pwa.storage import collect_instruments, get_station_preview
from sensor_network_pwa.validation import is_valid_email


def register(app, ctx):
    @app.route("/admin")
    def admin():
        admin_user = ctx.require_admin()
        if not isinstance(admin_user, dict):
            return admin_user

        storage_root = ctx.cfg.get("storage_root")
        instruments = collect_instruments(storage_root) if storage_root else []
        policies = ctx.access_store.list_policies(instruments)
        users = ctx.access_store.list_users()
        pending_requests = ctx.access_store.list_account_requests(status="pending")
        all_access = ctx.access_store.list_all_user_instruments()
        all_controls = ctx.access_store.list_all_user_station_controls()

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
            "admin.html",
            admin_user=admin_user,
            stations=stations,
            users=users,
            regular_users=regular_users,
            pending_requests=pending_requests,
            smtp_ready=bool(ctx.cfg.get("smtp_enabled") and ctx.cfg.get("smtp_host")),
        )

    @app.route("/admin/dashboard")
    def admin_dashboard():
        admin_user = ctx.require_admin()
        if not isinstance(admin_user, dict):
            return admin_user

        return render_template("admin_dashboard.html")

    @app.route("/api/admin/dashboard")
    def admin_dashboard_api():
        admin_user = ctx.require_admin()
        if not isinstance(admin_user, dict):
            return admin_user
        storage_root = ctx.storage_root_or_404()
        return jsonify(build_admin_network_dashboard(storage_root))

    @app.route("/anomalies")
    def anomalies_log():
        user = ctx.require_login()
        if not isinstance(user, dict):
            return user
        items = ctx.access_store.list_anomalies_for_user(user)
        return render_template(
            "anomalies.html",
            items=items,
        )

    @app.route("/anomalies/silence", methods=["POST"])
    def silence_anomaly():
        user = ctx.require_login()
        if not isinstance(user, dict):
            return user
        station_uuid = request.form.get("station_uuid", "").strip()
        anomaly_type = request.form.get("anomaly_type", "").strip()
        try:
            hours = int(request.form.get("hours", "24") or 24)
        except ValueError:
            abort(400, "hours must be an integer")
        if not station_uuid or not anomaly_type:
            abort(400, "Missing station_uuid or anomaly_type")
        if user.get("role") != "admin" and not ctx.station_is_accessible(user, station_uuid):
            abort(403)
        ctx.access_store.set_anomaly_silence(station_uuid, anomaly_type, user["username"], hours)
        return redirect(url_for("anomalies_log"))

    def admin_done(tab: str, ok: bool, message: str):
        """Report the outcome of an admin action on the tab it was started from."""
        flash(message, "success" if ok else "danger")
        return redirect(url_for("admin") + f"#{tab}")

    @app.route("/admin/create-user", methods=["POST"])
    def admin_create_user():
        admin_user = ctx.require_admin()
        if not isinstance(admin_user, dict):
            return admin_user

        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "user")

        if email and not is_valid_email(email):
            return admin_done("users", False, "Email address is not valid")
        ok, msg = ctx.access_store.create_user(username, password, email, role=role)
        if not ok:
            return admin_done("users", False, msg)
        if parse_boolish(request.form.get("force_password_change"), False):
            ctx.access_store.set_force_password_change(username, True)
        msg = f"User {username} created"
        if email:
            token = ctx.access_store.create_login_token(username, ttl_minutes=60)
            link = compose_external_url(ctx.cfg["base_url"], url_for("fast_login"), {"token": token})
            sent = send_email(
                ctx.cfg,
                [email],
                "Welcome to Sensor Network Collector",
                f"Hello {username},\n\nYour account has been created.\n"
                f"Fast login link (expires in 60 minutes):\n{link}\n",
            )
            msg += "; a welcome email with a login link was sent" if sent else "; no welcome email was sent"
        return admin_done("users", True, msg)

    @app.route("/admin/requests/<int:request_id>/approve", methods=["POST"])
    def admin_approve_request(request_id: int):
        admin_user = ctx.require_admin()
        if not isinstance(admin_user, dict):
            return admin_user
        ok, msg = ctx.access_store.approve_request(request_id, admin_user["username"])
        if not ok:
            return admin_done("requests", False, msg)
        req = ctx.access_store.get_account_request(request_id)
        if req and req.get("email"):
            token = ctx.access_store.create_account_request_token(request_id, ttl_hours=48)
            link = compose_external_url(ctx.cfg["base_url"], url_for("complete_account_request"), {"token": token})
            sent = send_email(
                ctx.cfg,
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

    @app.route("/admin/requests/<int:request_id>/reject", methods=["POST"])
    def admin_reject_request(request_id: int):
        admin_user = ctx.require_admin()
        if not isinstance(admin_user, dict):
            return admin_user
        ok, msg = ctx.access_store.reject_request(request_id, admin_user["username"])
        return admin_done("requests", ok, msg)

    @app.route("/admin/policy", methods=["POST"])
    def admin_set_policy():
        admin_user = ctx.require_admin()
        if not isinstance(admin_user, dict):
            return admin_user

        instrument_uuid = request.form.get("instrument_uuid", "")
        policy = request.form.get("policy", "")
        ok, msg = ctx.access_store.set_policy(instrument_uuid, policy, admin_user["username"])
        if ok:
            msg = f"Policy of {instrument_uuid.strip()} set to {policy}"
        return admin_done("stations", ok, msg)

    @app.route("/admin/user-access", methods=["POST"])
    def admin_set_user_access():
        admin_user = ctx.require_admin()
        if not isinstance(admin_user, dict):
            return admin_user

        username = request.form.get("username", "")
        instrument_uuid = request.form.get("instrument_uuid", "")
        allow = parse_boolish(request.form.get("allow", "1"), True)

        ok, msg = ctx.access_store.set_user_instrument_access(username, instrument_uuid, allow)
        return admin_done("users", ok, msg)

    @app.route("/admin/user-control", methods=["POST"])
    def admin_set_user_control():
        admin_user = ctx.require_admin()
        if not isinstance(admin_user, dict):
            return admin_user

        username = request.form.get("username", "")
        station_uuid = request.form.get("station_uuid", "")
        allow = parse_boolish(request.form.get("allow", "1"), True)

        ok, msg = ctx.access_store.set_user_station_control(username, station_uuid, allow)
        return admin_done("users", ok, msg)

    @app.route("/admin/user-permissions", methods=["POST"])
    def admin_set_user_permissions():
        admin_user = ctx.require_admin()
        if not isinstance(admin_user, dict):
            return admin_user
        ok, msg = ctx.access_store.replace_user_permissions(
            request.form.get("username", ""),
            request.form.getlist("station"),
            request.form.getlist("access"),
            request.form.getlist("control"),
        )
        return admin_done("users", ok, msg)

    @app.route("/admin/station-permissions", methods=["POST"])
    def admin_set_station_permissions():
        admin_user = ctx.require_admin()
        if not isinstance(admin_user, dict):
            return admin_user
        ok, msg = ctx.access_store.replace_station_permissions(
            request.form.get("station_uuid", ""),
            request.form.getlist("user"),
            request.form.getlist("access"),
            request.form.getlist("control"),
        )
        return admin_done("stations", ok, msg)

    @app.route("/admin/user-update", methods=["POST"])
    def admin_update_user():
        admin_user = ctx.require_admin()
        if not isinstance(admin_user, dict):
            return admin_user
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
        ok, msg = ctx.access_store.update_user(username, **changes)
        return admin_done("users", ok, msg)

    @app.route("/admin/force-password", methods=["POST"])
    def admin_force_password():
        admin_user = ctx.require_admin()
        if not isinstance(admin_user, dict):
            return admin_user
        username = request.form.get("username", "").strip()
        if not username:
            return admin_done("users", False, "Username is required")
        force = parse_boolish(request.form.get("force", "1"), True)
        ok, msg = ctx.access_store.set_force_password_change(username, force)
        if ok:
            msg = (
                f"{username} must choose a new password at the next login"
                if force
                else f"{username} is no longer asked to change password"
            )
        return admin_done("users", ok, msg)

    @app.route("/admin/set-password", methods=["POST"])
    def admin_set_password():
        admin_user = ctx.require_admin()
        if not isinstance(admin_user, dict):
            return admin_user
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")
        if password != password2:
            return admin_done("users", False, "Passwords do not match")
        force = parse_boolish(request.form.get("force_password_change"), False)
        ok, msg = ctx.access_store.change_password(username, password, force_password_change=force)
        if ok:
            msg = f"Password for {username} updated"
            if force:
                msg += "; a new password will be required at the next login"
        return admin_done("users", ok, msg)
