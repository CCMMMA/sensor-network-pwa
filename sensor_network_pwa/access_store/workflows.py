"""Persisted user-owned data-processing workflows and their firing state."""

import json

from sensor_network_pwa.access_store.station_settings import StationSettingsMixin
from sensor_network_pwa.timeutil import now_utc_iso


class WorkflowsMixin(StationSettingsMixin):
    def list_workflows(self, username: str | None = None, enabled_only: bool = False):
        clauses, params = [], []
        if username is not None:
            clauses.append("username = ?")
            params.append(username)
        if enabled_only:
            clauses.append("enabled = 1")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as con:
            rows = con.execute(f"SELECT * FROM workflows{where} ORDER BY name,id", params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["graph"] = json.loads(item.pop("graph_json"))
            result.append(item)
        return result

    def get_workflow(self, workflow_id: int):
        with self._connect() as con:
            row = con.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["graph"] = json.loads(item.pop("graph_json"))
        return item

    def save_workflow(self, username: str, name: str, graph: dict, enabled: bool = True, workflow_id=None):
        now = now_utc_iso()
        encoded = json.dumps(graph, separators=(",", ":"), sort_keys=True)
        with self._lock, self._connect() as con:
            if workflow_id is None:
                cur = con.execute(
                    "INSERT INTO workflows(username,name,enabled,graph_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (username, name, int(enabled), encoded, now, now),
                )
                return int(cur.lastrowid)
            cur = con.execute(
                "UPDATE workflows SET name=?,enabled=?,graph_json=?,updated_at=? WHERE id=? AND username=?",
                (name, int(enabled), encoded, now, workflow_id, username),
            )
            return int(workflow_id) if cur.rowcount else None

    def delete_workflow(self, workflow_id: int, username: str):
        with self._lock, self._connect() as con:
            cur = con.execute("DELETE FROM workflows WHERE id=? AND username=?", (workflow_id, username))
            if cur.rowcount:
                con.execute("DELETE FROM workflow_state WHERE workflow_id=?", (workflow_id,))
        return bool(cur.rowcount)

    def get_workflow_state(self, workflow_id: int, node_id: str):
        with self._connect() as con:
            row = con.execute(
                "SELECT active,last_fired_at FROM workflow_state WHERE workflow_id=? AND node_id=?",
                (workflow_id, node_id),
            ).fetchone()
        return dict(row) if row else {"active": 0, "last_fired_at": None}

    def set_workflow_state(self, workflow_id: int, node_id: str, active: bool, fired_at=None):
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT INTO workflow_state(workflow_id,node_id,active,last_fired_at) VALUES(?,?,?,?) "
                "ON CONFLICT(workflow_id,node_id) DO UPDATE SET active=excluded.active, "
                "last_fired_at=COALESCE(excluded.last_fired_at,workflow_state.last_fired_at)",
                (workflow_id, node_id, int(active), fired_at),
            )

    def mark_workflow_run(self, workflow_id: int, error: str | None = None):
        with self._lock, self._connect() as con:
            con.execute(
                "UPDATE workflows SET last_run_at=?,last_error=? WHERE id=?",
                (now_utc_iso(), error, workflow_id),
            )
