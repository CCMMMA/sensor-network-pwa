"""Anomaly log and silences."""

from datetime import datetime, timedelta

from sensor_network_pwa.access_store.base import StoreBase
from sensor_network_pwa.timeutil import now_utc_iso, parse_iso_ts, utc_iso, utc_now


class AnomaliesMixin(StoreBase):
    def upsert_anomaly(
        self, station_uuid: str, anomaly_type: str, message: str, severity: str = "warning", now: datetime | None = None
    ):
        stamp = utc_iso(now) if now else now_utc_iso()
        with self._lock, self._connect() as con:
            row = con.execute(
                """
                SELECT id, status
                FROM anomalies
                WHERE station_uuid = ? AND anomaly_type = ? AND status = 'open'
                ORDER BY created_at DESC LIMIT 1
                """,
                (station_uuid, anomaly_type),
            ).fetchone()
            if row:
                con.execute(
                    "UPDATE anomalies SET message = ?, updated_at = ? WHERE id = ?",
                    (message, stamp, row["id"]),
                )
                return row["id"], False
            cur = con.execute(
                """
                INSERT INTO anomalies(
                    station_uuid, anomaly_type, message, severity, status, created_at, updated_at, resolved_at
                )
                VALUES(?,?,?,?, 'open',?,?,NULL)
                """,
                (station_uuid, anomaly_type, message, severity, stamp, stamp),
            )
            return cur.lastrowid, True

    def resolve_anomaly(
        self, station_uuid: str, anomaly_type: str, message: str = "resolved", now: datetime | None = None
    ):
        stamp = utc_iso(now) if now else now_utc_iso()
        with self._lock, self._connect() as con:
            open_rows = con.execute(
                "SELECT id FROM anomalies WHERE station_uuid = ? AND anomaly_type = ? AND status = 'open'",
                (station_uuid, anomaly_type),
            ).fetchall()
            if not open_rows:
                return False
            con.execute(
                """
                    UPDATE anomalies
                    SET status='resolved', message=?, updated_at=?, resolved_at=?
                    WHERE station_uuid = ? AND anomaly_type = ? AND status = 'open'
                    """,
                (message, stamp, stamp, station_uuid, anomaly_type),
            )
            return True

    def list_open_anomalies(self):
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT id,station_uuid,anomaly_type,message,severity,status,created_at,updated_at,resolved_at
                FROM anomalies
                WHERE status = 'open'
                ORDER BY updated_at DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def list_anomalies_for_user(self, user):
        if not user:
            return []
        with self._connect() as con:
            if user.get("role") == "admin":
                rows = con.execute(
                    """
                    SELECT id,station_uuid,anomaly_type,message,severity,status,created_at,updated_at,resolved_at
                    FROM anomalies
                    ORDER BY updated_at DESC
                    LIMIT 1000
                    """
                ).fetchall()
                return [dict(r) for r in rows]
            rows = con.execute(
                """
                SELECT a.id,a.station_uuid,a.anomaly_type,a.message,a.severity,a.status,
                       a.created_at,a.updated_at,a.resolved_at
                FROM anomalies a
                LEFT JOIN instrument_policies p ON p.instrument_uuid = a.station_uuid
                LEFT JOIN user_instruments ui ON ui.instrument_uuid = a.station_uuid AND ui.username = ?
                WHERE COALESCE(p.policy, 'account') <> 'restricted' OR ui.username IS NOT NULL
                ORDER BY a.updated_at DESC
                LIMIT 1000
                """,
                (user["username"],),
            ).fetchall()
            return [dict(r) for r in rows]

    def set_anomaly_silence(self, station_uuid: str, anomaly_type: str, silenced_by: str, hours: int):
        now = utc_now()
        hours = min(24, max(1, int(hours)))
        until = now + timedelta(hours=hours)
        with self._lock, self._connect() as con:
            con.execute(
                """
                INSERT INTO anomaly_silence(station_uuid, anomaly_type, silenced_until, silenced_by, created_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(station_uuid, anomaly_type)
                DO UPDATE SET silenced_until=excluded.silenced_until, silenced_by=excluded.silenced_by,
                              created_at=excluded.created_at
                """,
                (station_uuid, anomaly_type, until.isoformat().replace("+00:00", "Z"), silenced_by, now_utc_iso()),
            )
        return until

    def get_anomaly_silenced_until(self, station_uuid: str, anomaly_type: str):
        with self._connect() as con:
            row = con.execute(
                "SELECT silenced_until FROM anomaly_silence WHERE station_uuid = ? AND anomaly_type = ?",
                (station_uuid, anomaly_type),
            ).fetchone()
            if row is None:
                return None
            return parse_iso_ts(row["silenced_until"])
