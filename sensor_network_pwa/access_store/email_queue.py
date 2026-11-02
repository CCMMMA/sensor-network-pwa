"""Persistent outgoing email queue shared by web and watchdog processes."""

import json
from datetime import datetime, timedelta

from sensor_network_pwa.access_store.users import UsersMixin
from sensor_network_pwa.timeutil import utc_iso


class EmailQueueMixin(UsersMixin):
    def enqueue_email(self, recipients, subject: str, body_text: str, now: datetime):
        recipients = [str(item).strip() for item in recipients if str(item).strip()]
        if not recipients:
            return False
        stamp = utc_iso(now)
        with self._lock, self._connect() as con:
            con.execute(
                """
                INSERT INTO email_queue(recipients_json,subject,body_text,status,attempts,created_at,next_attempt_at)
                VALUES(?,?,?,'pending',0,?,?)
                """,
                (json.dumps(recipients), subject, body_text, stamp, stamp),
            )
        return True

    def claim_queued_emails(self, now: datetime, limit: int):
        stamp = utc_iso(now)
        stale = utc_iso(now - timedelta(minutes=5))
        with self._lock, self._connect() as con:
            # Claiming must be serial across separate watchdog processes too.
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                """
                SELECT id,recipients_json,subject,body_text,attempts
                FROM email_queue
                WHERE (status='pending' AND next_attempt_at <= ?)
                   OR (status='sending' AND locked_at <= ?)
                ORDER BY id LIMIT ?
                """,
                (stamp, stale, max(1, min(int(limit), 100))),
            ).fetchall()
            ids = [row["id"] for row in rows]
            con.executemany(
                "UPDATE email_queue SET status='sending',locked_at=? WHERE id=?",
                [(stamp, queue_id) for queue_id in ids],
            )
        return [{**dict(row), "recipients": json.loads(row["recipients_json"])} for row in rows]

    def finish_queued_email(self, queue_id: int, now: datetime):
        with self._lock, self._connect() as con:
            con.execute(
                "UPDATE email_queue SET status='sent',sent_at=?,locked_at=NULL,last_error=NULL WHERE id=?",
                (utc_iso(now), queue_id),
            )

    def retry_queued_email(self, queue_id: int, attempts: int, error: str, now: datetime):
        attempts = int(attempts) + 1
        delay_min = min(60, 2 ** min(attempts - 1, 6))
        with self._lock, self._connect() as con:
            con.execute(
                """
                UPDATE email_queue
                SET status='pending',attempts=?,next_attempt_at=?,locked_at=NULL,last_error=?
                WHERE id=?
                """,
                (attempts, utc_iso(now + timedelta(minutes=delay_min)), str(error)[:500], queue_id),
            )

    def email_queue_summary(self):
        with self._connect() as con:
            counts = con.execute("SELECT status,COUNT(*) AS count FROM email_queue GROUP BY status").fetchall()
            recent = con.execute(
                """
                SELECT id,recipients_json,subject,status,attempts,created_at,next_attempt_at,sent_at,last_error
                FROM email_queue ORDER BY id DESC LIMIT 100
                """
            ).fetchall()
        return {
            "counts": {row["status"]: row["count"] for row in counts},
            "recent": [{**dict(row), "recipients": json.loads(row["recipients_json"])} for row in recent],
        }
