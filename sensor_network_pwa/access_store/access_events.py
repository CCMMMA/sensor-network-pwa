"""User access and data-download audit events."""

from sensor_network_pwa.access_store.base import StoreBase
from sensor_network_pwa.timeutil import now_utc_iso


class AccessEventsMixin(StoreBase):
    def record_access_event(
        self,
        event_type: str,
        *,
        username: str | None = None,
        station_uuid: str | None = None,
        method: str = "GET",
        path: str = "",
        status_code: int = 200,
    ) -> None:
        with self._lock, self._connect() as con:
            con.execute(
                """INSERT INTO access_events
                   (username,event_type,station_uuid,method,path,status_code,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (username, event_type, station_uuid, method, path, status_code, now_utc_iso()),
            )

    def list_access_events(
        self,
        *,
        username: str = "",
        event_type: str = "",
        station_uuid: str = "",
        from_date: str = "",
        to_date: str = "",
        limit: int = 500,
    ) -> list[dict]:
        clauses = []
        values: list[object] = []
        for column, value in (("username", username), ("event_type", event_type), ("station_uuid", station_uuid)):
            if value:
                clauses.append(f"{column} = ?")
                values.append(value)
        if from_date:
            clauses.append("created_at >= ?")
            values.append(f"{from_date}T00:00:00Z")
        if to_date:
            clauses.append("created_at <= ?")
            values.append(f"{to_date}T23:59:59.999999Z")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, min(int(limit), 10000)))
        with self._lock, self._connect() as con:
            rows = con.execute(
                f"SELECT * FROM access_events{where} ORDER BY created_at DESC, id DESC LIMIT ?", values
            ).fetchall()
        return [dict(row) for row in rows]
