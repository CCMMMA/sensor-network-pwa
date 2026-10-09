"""Sign-in, password and account-request routes."""

from flask import abort, jsonify, redirect, render_template, request, session, url_for

from sensor_network_pwa.mailer import compose_external_url, send_email
from sensor_network_pwa.validation import validate_password_strength


def register(app, ctx):
    @app.route("/login", methods=["GET", "POST"])
    def login():
        err = str(request.args.get("err", "") or "").strip()
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = ctx.access_store.authenticate(username, password)
            if user:
                session.clear()
                session["username"] = user["username"]
                if int(user.get("force_password_change", 0)) == 1:
                    return redirect(url_for("change_password"))
                nxt = request.args.get("next") or url_for("index")
                if (
                    not nxt.startswith("/")
                    or nxt.startswith("//")
                    or "\\" in nxt
                    or any(ord(ch) < 32 or ord(ch) == 127 for ch in nxt)
                ):
                    nxt = url_for("index")
                return redirect(nxt)
            err = "Invalid credentials or inactive user"

        return render_template(
            "login.html",
            err=err,
        )

    @app.route("/fast-login")
    def fast_login():
        token = request.args.get("token", "")
        # Destinations are named, so a link cannot redirect anywhere else.
        target = url_for("index")
        if request.args.get("next") == "notifications":
            target = url_for("profile") + "#notifications"
        user = ctx.access_store.consume_login_token(token)
        if not user:
            if target != url_for("index"):
                # A notification link read late: the page is still one login away.
                if ctx.current_user():
                    return redirect(target)
                return redirect(url_for("login", next=target, err="The login link has expired. Please log in."))
            abort(403, "Invalid or expired login token")
        session.clear()
        session["username"] = user["username"]
        if int(user.get("force_password_change", 0)) == 1:
            return redirect(url_for("change_password"))
        return redirect(target)

    @app.route("/logout")
    def logout():
        session.pop("username", None)
        return redirect(url_for("index"))

    @app.route("/change-password", methods=["GET", "POST"])
    def change_password():
        user = ctx.require_login()
        if not isinstance(user, dict):
            return user
        msg = ""
        err = ""
        if request.method == "POST":
            p1 = request.form.get("password", "")
            p2 = request.form.get("password2", "")
            if p1 != p2:
                err = "Passwords do not match"
            else:
                ok, txt = ctx.access_store.change_password(user["username"], p1)
                if ok:
                    msg = txt
                else:
                    err = txt
        return render_template(
            "change_password.html",
            msg=msg,
            err=err,
        )

    @app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
        msg = ""
        err = ""
        if request.method == "POST":
            identity = request.form.get("identity", "").strip()
            user = ctx.access_store.find_active_user_by_identity(identity)
            if user and user.get("email"):
                token = ctx.access_store.create_password_reset_token(user["username"], ttl_minutes=30)
                link = compose_external_url(ctx.cfg["base_url"], url_for("reset_password"), {"token": token})
                send_email(
                    ctx.cfg,
                    [user["email"]],
                    "Sensor Network Collector: password reset",
                    (
                        f"Hello {user['username']},\n\n"
                        f"Use the following link to reset your password. It expires in 30 minutes:\n{link}\n"
                    ),
                )
            msg = "If the account exists and has an email address, a reset link has been sent."

        return render_template(
            "forgot_password.html",
            msg=msg,
            err=err,
        )

    @app.route("/reset-password", methods=["GET", "POST"])
    def reset_password():
        token = request.args.get("token", "").strip()
        user = ctx.access_store.get_password_reset_user(token)
        if user is None:
            abort(403, "Invalid or expired password reset token")

        msg = ""
        err = ""
        if request.method == "POST":
            p1 = request.form.get("password", "")
            p2 = request.form.get("password2", "")
            strong, strength_msg = validate_password_strength(p1)
            if p1 != p2:
                err = "Passwords do not match"
            elif not strong:
                # Checked before consuming the single-use token.
                err = strength_msg
            else:
                consumed_user = ctx.access_store.consume_password_reset_token(token)
                if consumed_user is None:
                    err = "Invalid or expired password reset token"
                else:
                    ok, txt = ctx.access_store.change_password(consumed_user["username"], p1)
                    if ok:
                        msg = txt
                    else:
                        err = txt

        return render_template(
            "reset_password.html",
            user=user,
            msg=msg,
            err=err,
        )

    @app.route("/request-account", methods=["GET", "POST"])
    def request_account():
        msg = ""
        err = ""
        if request.method == "POST":
            email = request.form.get("email", "").strip()
            reason = request.form.get("reason", "")
            ok, text = ctx.access_store.create_account_request(email, reason)
            if ok:
                msg = text
                send_email(
                    ctx.cfg,
                    [email],
                    "Sensor Network Collector: registration request received",
                    "Hello,\n\nYour registration request has been received and is pending admin approval.",
                )
                admin_emails = ctx.access_store.list_admin_emails()
                if admin_emails:
                    send_email(
                        ctx.cfg,
                        admin_emails,
                        "Sensor Network Collector: new account request",
                        (
                            "A new account request has been submitted.\n\n"
                            f"Email: {email}\n"
                            f"Reason: {reason.strip() or '-'}\n\n"
                            "Review it in the admin panel:\n"
                            f"{compose_external_url(ctx.cfg['base_url'], url_for('admin'))}\n"
                        ),
                    )
            else:
                err = text

        return render_template(
            "request_account.html",
            msg=msg,
            err=err,
        )

    @app.route("/api/check-username")
    def check_username():
        # Only the onboarding form needs this; without a valid link it would list usernames.
        if ctx.access_store.get_account_request_for_token(request.args.get("token", "").strip()) is None:
            abort(403, "Invalid or expired onboarding link")
        username = request.args.get("username", "").strip()
        if not username:
            return jsonify({"available": False, "message": "Username is required"})
        available = not ctx.access_store.username_exists(username)
        return jsonify({"available": available, "message": "" if available else "Username already exists"})

    @app.route("/request-account/complete", methods=["GET", "POST"])
    def complete_account_request():
        token = request.args.get("token", "").strip()
        request_row = ctx.access_store.get_account_request_for_token(token)
        if request_row is None:
            return render_template("account_request_invalid.html")
        msg = ""
        err = ""
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")
            if password != confirm_password:
                err = "Passwords do not match"
            else:
                ok, text = ctx.access_store.complete_account_request(token, username, password)
                if ok:
                    msg = text
                    session["username"] = username
                    return redirect(url_for("index"))
                err = text
        return render_template(
            "account_request_complete.html",
            request_row=request_row,
            onboarding_token=token,
            msg=msg,
            err=err,
        )
