"""User profile and failure-notification preferences."""

from flask import abort, flash, redirect, render_template, request, url_for

from sensor_network_pwa.access_store import NOTIFICATION_INTERVAL_CHOICES, NOTIFICATION_SNOOZE_HOURS
from sensor_network_pwa.timeutil import parse_iso_ts, utc_now


def register(app, ctx):
    def profile_done(ok: bool, message: str):
        flash(message, "success" if ok else "danger")
        return redirect(url_for("profile") + "#notifications")

    @app.route("/profile")
    def profile():
        user = ctx.require_login()
        if not isinstance(user, dict):
            return user

        def shown_time(value):
            dt = parse_iso_ts(value or "")
            return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else ""

        now = utc_now()
        notifications = []
        for item in ctx.access_store.list_notifications_for_user(user["username"]):
            if user.get("role") != "admin" and not ctx.station_is_accessible(user, item["station_uuid"]):
                continue
            is_open = item["status"] == "open"
            snoozed_until = parse_iso_ts(item["snoozed_until"] or "")
            if not is_open:
                state = "Ended"
            elif item["acknowledged_at"]:
                state = "Acknowledged"
            elif snoozed_until is not None and snoozed_until > now:
                state = f"Snoozed until {shown_time(item['snoozed_until'])}"
            else:
                state = "Active"
            notifications.append(
                {
                    "id": item["id"],
                    "station_uuid": item["station_uuid"],
                    "anomaly_type": item["anomaly_type"],
                    "message": item["message"] if is_open else "",
                    "is_open": is_open,
                    "state": state,
                    "can_acknowledge": is_open and not item["acknowledged_at"],
                    "failed_at": shown_time(item["failed_at"]),
                    "resolved_at": shown_time(item["resolved_at"]),
                    "last_sent_at": shown_time(item["recovery_sent_at"] or item["last_sent_at"]),
                }
            )
        return render_template(
            "profile.html",
            user=user,
            notifications=notifications,
            interval_min=ctx.access_store.get_notification_interval(user["username"]),
            interval_choices=NOTIFICATION_INTERVAL_CHOICES,
            snooze_hours=NOTIFICATION_SNOOZE_HOURS,
        )

    @app.route("/profile/notification-interval", methods=["POST"])
    def profile_notification_interval():
        user = ctx.require_login()
        if not isinstance(user, dict):
            return user
        try:
            minutes = int(request.form.get("minutes", ""))
        except ValueError:
            abort(400, "minutes must be an integer")
        return profile_done(*ctx.access_store.set_notification_interval(user["username"], minutes))

    @app.route("/profile/notifications/<int:notification_id>", methods=["POST"])
    def profile_notification_action(notification_id: int):
        user = ctx.require_login()
        if not isinstance(user, dict):
            return user
        try:
            hours = int(request.form.get("hours", "0") or 0)
        except ValueError:
            abort(400, "hours must be an integer")
        # A notification belongs to one user: only its owner can change it.
        return profile_done(
            *ctx.access_store.update_notification(
                user["username"], notification_id, request.form.get("action", ""), snooze_hours=hours
            )
        )
