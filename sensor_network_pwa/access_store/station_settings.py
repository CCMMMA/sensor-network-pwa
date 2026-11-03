"""Per-station logo and chart settings."""

from sensor_network_pwa.access_store.base import StoreBase
from sensor_network_pwa.timeutil import now_utc_iso


class StationSettingsMixin(StoreBase):
    def list_registered_stations(self):
        with self._connect() as con:
            rows = con.execute(
                "SELECT station_uuid,display_name,enabled,created_at,updated_at,updated_by "
                "FROM station_registry ORDER BY station_uuid"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_registered_station(self, station_uuid: str):
        with self._connect() as con:
            row = con.execute(
                "SELECT station_uuid,display_name,enabled FROM station_registry WHERE station_uuid=?",
                (station_uuid.strip(),),
            ).fetchone()
        return dict(row) if row else None

    def save_registered_station(self, station_uuid: str, display_name: str, updated_by: str):
        station_uuid = station_uuid.strip()
        display_name = display_name.strip()
        if not station_uuid:
            return False, "Station UUID is required"
        if any(part in station_uuid for part in ("/", "\\", "..")):
            return False, "Station UUID is not valid"
        now = now_utc_iso()
        with self._lock, self._connect() as con:
            con.execute(
                """
                INSERT INTO station_registry(station_uuid,display_name,enabled,created_at,updated_at,updated_by)
                VALUES(?,?,1,?,?,?)
                ON CONFLICT(station_uuid) DO UPDATE SET
                    display_name=excluded.display_name,enabled=1,
                    updated_at=excluded.updated_at,updated_by=excluded.updated_by
                """,
                (station_uuid, display_name, now, now, updated_by),
            )
        return True, f"Station {station_uuid} saved"

    def set_registered_station_enabled(self, station_uuid: str, enabled: bool, updated_by: str):
        station_uuid = station_uuid.strip()
        now = now_utc_iso()
        with self._lock, self._connect() as con:
            con.execute(
                """
                INSERT INTO station_registry(station_uuid,display_name,enabled,created_at,updated_at,updated_by)
                VALUES(?,'',?,?,?,?)
                ON CONFLICT(station_uuid) DO UPDATE SET
                    enabled=excluded.enabled,updated_at=excluded.updated_at,updated_by=excluded.updated_by
                """,
                (station_uuid, int(enabled), now, now, updated_by),
            )
        return True, (f"Station {station_uuid} restored" if enabled else f"Station {station_uuid} removed from portal")

    def station_is_enabled(self, station_uuid: str):
        row = self.get_registered_station(station_uuid)
        return row is None or bool(row["enabled"])

    def station_display_name(self, station_uuid: str, fallback: str):
        row = self.get_registered_station(station_uuid)
        return (row["display_name"] if row else "") or fallback

    def set_station_logo(self, station_uuid: str, logo_path: str, username: str):
        with self._lock, self._connect() as con:
            con.execute(
                """
                INSERT INTO station_logos(station_uuid, logo_path, uploaded_by, uploaded_at)
                VALUES(?,?,?,?)
                ON CONFLICT(station_uuid)
                DO UPDATE SET logo_path=excluded.logo_path, uploaded_by=excluded.uploaded_by,
                              uploaded_at=excluded.uploaded_at
                """,
                (station_uuid, logo_path, username, now_utc_iso()),
            )

    def get_station_logo(self, station_uuid: str):
        with self._connect() as con:
            row = con.execute(
                "SELECT logo_path, uploaded_by, uploaded_at FROM station_logos WHERE station_uuid = ?",
                (station_uuid,),
            ).fetchone()
            return dict(row) if row else None

    def get_station_chart_settings(self, station_uuid: str):
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT station_uuid,series_key,y_min,y_max,y_step,updated_at,updated_by
                FROM station_chart_settings
                WHERE station_uuid = ?
                ORDER BY series_key
                """,
                (station_uuid,),
            ).fetchall()
            return {
                row["series_key"]: {
                    "series_key": row["series_key"],
                    "y_min": row["y_min"],
                    "y_max": row["y_max"],
                    "y_step": row["y_step"],
                    "updated_at": row["updated_at"],
                    "updated_by": row["updated_by"],
                }
                for row in rows
            }

    def replace_station_chart_settings(self, station_uuid: str, settings_map: dict, updated_by: str):
        station_uuid = station_uuid.strip()
        now = now_utc_iso()
        normalized = []
        for series_key, raw in (settings_map or {}).items():
            if not series_key:
                continue
            item = raw or {}
            normalized.append(
                (
                    station_uuid,
                    str(series_key).strip(),
                    item.get("y_min"),
                    item.get("y_max"),
                    item.get("y_step"),
                    now,
                    updated_by,
                )
            )

        with self._lock, self._connect() as con:
            con.execute("DELETE FROM station_chart_settings WHERE station_uuid = ?", (station_uuid,))
            if normalized:
                con.executemany(
                    """
                    INSERT INTO station_chart_settings(
                        station_uuid, series_key, y_min, y_max, y_step, updated_at, updated_by
                    )
                    VALUES(?,?,?,?,?,?,?)
                    """,
                    normalized,
                )
        return True
