"""Watchdog loop: scans stations for anomalies and emails failure notifications."""

import threading
from datetime import datetime, timedelta

from sensor_network_pwa.access_store import NOTIFICATION_LINK_TTL_MIN, AccessStore
from sensor_network_pwa.anomalies import anomaly_base_type, evaluate_station_anomalies
from sensor_network_pwa.log import logger
from sensor_network_pwa.mailer import compose_external_url, send_email
from sensor_network_pwa.storage import collect_instruments, load_station_rows
from sensor_network_pwa.timeutil import parse_iso_ts, utc_now


def run_watchdog_loop(cfg: dict, access_store: AccessStore, stop_event: threading.Event):
    storage_root = cfg.get("storage_root")
    if not storage_root:
        logger.info("Watchdog disabled: storage is not configured")
        return

    interval = max(10, int(cfg.get("watchdog_interval_sec", 60)))
    logger.info("Watchdog started (interval=%ss)", interval)

    while not stop_event.is_set():
        try:
            run_watchdog_scan(cfg, access_store, storage_root)
        except Exception:
            logger.exception("Watchdog scan failed; retrying in %ss", interval)
        stop_event.wait(interval)


def anomaly_recovery_hold_seconds(cfg: dict) -> int:
    """How long an anomaly must stay unseen before it counts as ended.

    A condition that comes and goes between scans would otherwise produce a
    failure and a recovery notification at every check.
    """
    return max(300, 3 * max(10, int(cfg.get("watchdog_interval_sec", 60))))


def notification_due(notification: dict, interval_min: int, now: datetime):
    """Which email an open failure owes its user now: 'failure', 'reminder' or None."""
    if notification.get("cleared_at") or notification.get("acknowledged_at"):
        return None
    snoozed_until = parse_iso_ts(notification.get("snoozed_until") or "")
    if snoozed_until is not None and snoozed_until > now:
        return None
    last_sent = parse_iso_ts(notification.get("last_sent_at") or "")
    if last_sent is None:
        return "failure"
    if interval_min <= 0:
        return None
    return "reminder" if (now - last_sent).total_seconds() >= interval_min * 60 else None


NOTIFICATION_EVENT_LABELS = {
    "failure": "FAILURE",
    "reminder": "STILL FAILING",
    "recovery": "BACK TO REGULAR",
}


def build_notification_email(cfg: dict, events, interval_min: int, login_link: str):
    """Subject and body of one email carrying all the events owed to a user."""
    if len(events) == 1:
        event = events[0]
        subject = (
            f"[Sensor Network] {NOTIFICATION_EVENT_LABELS[event['kind']]}: "
            f"{event['station_uuid']} - {event['anomaly_type']}"
        )
    else:
        subject = f"[Sensor Network] {len(events)} station notifications"
    lines = []
    for event in events:
        lines.append(f"{NOTIFICATION_EVENT_LABELS[event['kind']]}")
        lines.append(f"  Station: {event['station_uuid']}")
        if event["kind"] == "recovery":
            lines.append(f"  Ended: {event['anomaly_type']}")
            lines.append(f"  Failed at: {event['failed_at']}")
            lines.append(f"  Regular since: {event['resolved_at']}")
        else:
            lines.append(f"  Anomaly: {event['message']}")
            lines.append(f"  Failing since: {event['failed_at']}")
        lines.append("")
    if interval_min > 0:
        lines.append(f"While a failure persists you get a reminder every {interval_min} minutes.")
    else:
        lines.append("You are notified of status changes only; no reminders are sent.")
    lines.append("Acknowledge, snooze or clear your notifications, and change the notification time, here")
    lines.append(f"(automatic login, single use, valid for {NOTIFICATION_LINK_TTL_MIN} minutes):")
    lines.append(login_link)
    lines.append("")
    lines.append("After that, log in and open:")
    lines.append(compose_external_url(cfg["base_url"], "profile") + "#notifications")
    return subject, "\n".join(lines) + "\n"


def send_watchdog_notifications(cfg: dict, access_store: AccessStore, failing, now: datetime):
    """Email each user the status changes and the due reminders of this scan.

    `failing` lists the anomalies seen in the scan. A user gets one email per
    scan, and a notification is marked as sent only when its email was sent.
    """
    emailing = bool(cfg.get("smtp_enabled") and str(cfg.get("smtp_host", "") or "").strip())
    outbox: dict[str, dict] = {}
    for anomaly in failing:
        users = access_store.list_station_notification_users(anomaly["station_uuid"])
        notifications = access_store.sync_notifications(anomaly["id"], [u["username"] for u in users], now)
        silenced_until = access_store.get_anomaly_silenced_until(anomaly["station_uuid"], anomaly["anomaly_type"])
        if not emailing or (silenced_until is not None and silenced_until > now):
            continue
        for user in users:
            notification = notifications.get(user["username"])
            if not user["email"] or notification is None:
                continue
            kind = notification_due(notification, user["interval_min"], now)
            if kind:
                entry = outbox.setdefault(user["username"], {"email": user["email"], "events": []})
                entry["events"].append({**anomaly, "kind": kind, "notification_id": notification["id"]})
    if not emailing:
        return

    for row in access_store.list_pending_recovery_notifications(now - timedelta(hours=24)):
        entry = outbox.setdefault(row["username"], {"email": row["email"], "events": []})
        entry["events"].append({**row, "kind": "recovery", "notification_id": row["id"]})

    for username, entry in outbox.items():
        token = access_store.create_login_token(username, ttl_minutes=NOTIFICATION_LINK_TTL_MIN)
        login_link = compose_external_url(cfg["base_url"], "fast-login", {"token": token, "next": "notifications"})
        subject, body = build_notification_email(
            cfg, entry["events"], access_store.get_notification_interval(username), login_link
        )
        if not send_email(cfg, [entry["email"]], subject, body):
            continue
        for recovery in (False, True):
            ids = [e["notification_id"] for e in entry["events"] if (e["kind"] == "recovery") == recovery]
            if ids:
                access_store.mark_notifications_sent(ids, now, recovery=recovery)


def run_watchdog_scan(cfg: dict, access_store: AccessStore, storage_root: str, now: datetime | None = None):
    now = now or utc_now()
    recovery_hold = anomaly_recovery_hold_seconds(cfg)
    failing = []
    for instrument_uuid in collect_instruments(storage_root):
        try:
            rows = load_station_rows(storage_root, instrument_uuid, limit=500)
            summary = evaluate_station_anomalies(instrument_uuid, rows, now)

            active_types = set()
            for alarm in summary["alarms"]:
                anomaly_type = anomaly_base_type(alarm)
                active_types.add(anomaly_type)
                access_store.upsert_anomaly(
                    instrument_uuid,
                    anomaly_type,
                    alarm,
                    severity="critical" if anomaly_type in ("lost_connectivity", "sensor_failure") else "warning",
                    now=now,
                )

            # Resolve anomalies that have not been seen for the hold time.
            for open_anomaly in access_store.list_open_anomalies():
                if open_anomaly["station_uuid"] != instrument_uuid:
                    continue
                if open_anomaly["anomaly_type"] in active_types:
                    failing.append({**open_anomaly, "failed_at": open_anomaly["created_at"]})
                    continue
                last_seen = parse_iso_ts(open_anomaly["updated_at"])
                if last_seen is None or (now - last_seen).total_seconds() >= recovery_hold:
                    access_store.resolve_anomaly(instrument_uuid, open_anomaly["anomaly_type"], "resolved", now=now)
        except Exception:
            # One unreadable station must not stop the others from being checked.
            logger.exception("Watchdog check failed for station=%s", instrument_uuid)

    send_watchdog_notifications(cfg, access_store, failing, now)
