"""Failure notifications and each user's reminder interval."""

from datetime import datetime, timedelta

from sensor_network_pwa.access_store.permissions import PermissionsMixin
from sensor_network_pwa.timeutil import utc_iso, utc_now

# Minutes between reminder emails while a failure persists; 0 sends status changes only.
DEFAULT_NOTIFICATION_INTERVAL_MIN = 60
NOTIFICATION_INTERVAL_CHOICES = (
    (0, "Status changes only (no reminders)"),
    (15, "Every 15 minutes"),
    (30, "Every 30 minutes"),
    (60, "Every hour"),
    (120, "Every 2 hours"),
    (180, "Every 3 hours"),
    (360, "Every 6 hours"),
    (720, "Every 12 hours"),
    (1440, "Every 24 hours"),
)
NOTIFICATION_SNOOZE_HOURS = (1, 4, 8, 24)
NOTIFICATION_LINK_TTL_MIN = 60


def normalize_notification_interval(value):
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return DEFAULT_NOTIFICATION_INTERVAL_MIN
    if minutes in {choice for choice, _ in NOTIFICATION_INTERVAL_CHOICES}:
        return minutes
    return DEFAULT_NOTIFICATION_INTERVAL_MIN


class NotificationsMixin(PermissionsMixin):
    def list_station_notification_users(self, station_uuid: str):
        """Active users notified about a station: admins, plus the users its policy admits."""
        station_uuid = station_uuid.strip()
        policy = self.get_policy(station_uuid)
        with self._connect() as con:
            rows = con.execute(
                "SELECT username,email,notify_interval_min FROM users WHERE role = 'admin' AND active = 1"
            ).fetchall()
            if policy == "account":
                rows += con.execute(
                    "SELECT username,email,notify_interval_min FROM users WHERE role = 'user' AND active = 1"
                ).fetchall()
            elif policy == "restricted":
                rows += con.execute(
                    """
                    SELECT u.username,u.email,u.notify_interval_min
                    FROM users u
                    JOIN user_instruments ui ON ui.username = u.username
                    WHERE ui.instrument_uuid = ? AND u.active = 1
                    """,
                    (station_uuid,),
                ).fetchall()
        users = {}
        for r in rows:
            users[r["username"]] = {
                "username": r["username"],
                "email": (r["email"] or "").strip(),
                "interval_min": normalize_notification_interval(r["notify_interval_min"]),
            }
        return list(users.values())

    def get_notification_interval(self, username: str):
        with self._connect() as con:
            row = con.execute("SELECT notify_interval_min FROM users WHERE username = ?", (username,)).fetchone()
        return normalize_notification_interval(row["notify_interval_min"] if row else None)

    def set_notification_interval(self, username: str, minutes: int):
        if minutes not in {choice for choice, _ in NOTIFICATION_INTERVAL_CHOICES}:
            return False, "Unknown notification time"
        with self._lock, self._connect() as con:
            cur = con.execute("UPDATE users SET notify_interval_min = ? WHERE username = ?", (minutes, username))
            if cur.rowcount <= 0:
                return False, "User not found"
        return True, "Notification time saved"

    def sync_notifications(self, anomaly_id: int, usernames, now: datetime):
        """Give each user a notification for the anomaly; returns them by username."""
        with self._lock, self._connect() as con:
            con.executemany(
                "INSERT OR IGNORE INTO notifications(username, anomaly_id, created_at) VALUES(?,?,?)",
                [(username, anomaly_id, utc_iso(now)) for username in usernames],
            )
            rows = con.execute("SELECT * FROM notifications WHERE anomaly_id = ?", (anomaly_id,)).fetchall()
        return {r["username"]: dict(r) for r in rows}

    def mark_notifications_sent(self, notification_ids, now: datetime, recovery: bool = False):
        column = "recovery_sent_at" if recovery else "last_sent_at"
        with self._lock, self._connect() as con:
            con.executemany(
                f"UPDATE notifications SET {column} = ? WHERE id = ?",
                [(utc_iso(now), notification_id) for notification_id in notification_ids],
            )

    def list_pending_recovery_notifications(self, resolved_since: datetime):
        """Notifications of ended failures whose user was told of the failure but not of its end."""
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT n.id,n.username,u.email,a.station_uuid,a.anomaly_type,a.created_at AS failed_at,a.resolved_at
                FROM notifications n
                JOIN anomalies a ON a.id = n.anomaly_id
                JOIN users u ON u.username = n.username
                WHERE a.status = 'resolved' AND a.resolved_at >= ?
                  AND n.last_sent_at IS NOT NULL AND n.recovery_sent_at IS NULL AND n.cleared_at IS NULL
                  AND u.active = 1 AND u.email IS NOT NULL AND u.email <> ''
                ORDER BY n.id
                """,
                (utc_iso(resolved_since),),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_notifications_for_user(self, username: str, limit: int = 200):
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT n.id,n.created_at,n.last_sent_at,n.recovery_sent_at,n.acknowledged_at,n.snoozed_until,
                       a.station_uuid,a.anomaly_type,a.message,a.severity,a.status,
                       a.created_at AS failed_at,a.resolved_at
                FROM notifications n
                JOIN anomalies a ON a.id = n.anomaly_id
                WHERE n.username = ? AND n.cleared_at IS NULL
                ORDER BY (a.status = 'open') DESC, n.id DESC
                LIMIT ?
                """,
                (username, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_notification(self, username: str, notification_id: int, action: str, snooze_hours: int = 0):
        """Clear, acknowledge or snooze one of the user's own notifications."""
        now = utc_now()
        with self._lock, self._connect() as con:
            row = con.execute(
                """
                    SELECT a.status
                    FROM notifications n
                    JOIN anomalies a ON a.id = n.anomaly_id
                    WHERE n.id = ? AND n.username = ? AND n.cleared_at IS NULL
                    """,
                (notification_id, username),
            ).fetchone()
            if row is None:
                return False, "Notification not found"
            if action == "clear":
                con.execute("UPDATE notifications SET cleared_at = ? WHERE id = ?", (utc_iso(now), notification_id))
                return True, "Notification cleared"
            if row["status"] != "open":
                return False, "The failure has already ended"
            if action == "acknowledge":
                con.execute(
                    "UPDATE notifications SET acknowledged_at = ? WHERE id = ?", (utc_iso(now), notification_id)
                )
                return True, "Notification acknowledged"
            if action == "snooze" and snooze_hours in NOTIFICATION_SNOOZE_HOURS:
                con.execute(
                    "UPDATE notifications SET snoozed_until = ? WHERE id = ?",
                    (utc_iso(now + timedelta(hours=snooze_hours)), notification_id),
                )
                return True, f"Notification snoozed for {snooze_hours} h"
        return False, "Unknown action"
