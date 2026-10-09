"""Per-station logo and chart settings."""

from sensor_network_pwa.access_store.base import StoreBase
from sensor_network_pwa.timeutil import now_utc_iso


class StationSettingsMixin(StoreBase):
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
