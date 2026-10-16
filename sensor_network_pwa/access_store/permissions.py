"""Station policies and per-user browse, download and control rights."""

from sensor_network_pwa.access_store.users import UsersMixin
from sensor_network_pwa.timeutil import now_utc_iso


class PermissionsMixin(UsersMixin):
    def set_policy(self, instrument_uuid: str, policy: str, updated_by: str):
        if policy not in ("open", "account", "restricted"):
            return False, "Invalid policy"
        instrument_uuid = instrument_uuid.strip()
        if not instrument_uuid:
            return False, "instrument UUID is required"

        with self._lock, self._connect() as con:
            con.execute(
                """
                    INSERT INTO instrument_policies(instrument_uuid, policy, updated_at, updated_by)
                    VALUES(?,?,?,?)
                    ON CONFLICT(instrument_uuid)
                    DO UPDATE SET policy=excluded.policy, updated_at=excluded.updated_at, updated_by=excluded.updated_by
                    """,
                (instrument_uuid, policy, now_utc_iso(), updated_by),
            )
        return True, "Policy updated"

    def get_policy(self, instrument_uuid: str):
        with self._connect() as con:
            row = con.execute(
                "SELECT policy FROM instrument_policies WHERE instrument_uuid = ?",
                (instrument_uuid,),
            ).fetchone()
        return row["policy"] if row else "account"

    def list_policies(self, instrument_uuids):
        result = {}
        with self._connect() as con:
            rows = con.execute("SELECT instrument_uuid, policy FROM instrument_policies").fetchall()
            for row in rows:
                result[row["instrument_uuid"]] = row["policy"]
        for uid in instrument_uuids:
            result.setdefault(uid, "account")
        return result

    def set_user_instrument_permission(self, username: str, instrument_uuid: str, permission: str, allow: bool):
        username = username.strip()
        instrument_uuid = instrument_uuid.strip()
        if not username or not instrument_uuid:
            return False, "Username and instrument UUID are required"
        if permission not in ("can_browse", "can_download"):
            return False, "Invalid station permission"

        with self._lock, self._connect() as con:
            user_exists = con.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
            if not user_exists:
                return False, "User not found"

            con.execute(
                "INSERT OR IGNORE INTO user_instruments"
                "(username,instrument_uuid,can_browse,can_download) VALUES(?,?,0,0)",
                (username, instrument_uuid),
            )
            con.execute(
                f"UPDATE user_instruments SET {permission} = ? WHERE username = ? AND instrument_uuid = ?",
                (1 if allow else 0, username, instrument_uuid),
            )
            con.execute(
                "DELETE FROM user_instruments WHERE username = ? AND instrument_uuid = ?"
                " AND can_browse = 0 AND can_download = 0",
                (username, instrument_uuid),
            )
        return True, "Permission updated"

    def set_user_instrument_access(self, username: str, instrument_uuid: str, allow: bool):
        """Compatibility helper: grant or revoke both historical data rights."""
        ok, msg = self.set_user_instrument_permission(username, instrument_uuid, "can_browse", allow)
        if not ok:
            return ok, msg
        return self.set_user_instrument_permission(username, instrument_uuid, "can_download", allow)

    def get_user_instruments(self, username: str):
        with self._connect() as con:
            rows = con.execute(
                "SELECT instrument_uuid FROM user_instruments"
                " WHERE username = ? AND (can_browse = 1 OR can_download = 1) ORDER BY instrument_uuid",
                (username,),
            ).fetchall()
            return [r["instrument_uuid"] for r in rows]

    def get_user_download_stations(self, username: str):
        with self._connect() as con:
            rows = con.execute(
                "SELECT instrument_uuid FROM user_instruments"
                " WHERE username = ? AND can_download = 1 ORDER BY instrument_uuid",
                (username,),
            ).fetchall()
            return [r["instrument_uuid"] for r in rows]

    def set_user_station_control(self, username: str, station_uuid: str, allow: bool):
        username = username.strip()
        station_uuid = station_uuid.strip()
        if not username or not station_uuid:
            return False, "Username and station UUID are required"

        with self._lock, self._connect() as con:
            user_exists = con.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
            if not user_exists:
                return False, "User not found"

            if allow:
                con.execute(
                    "INSERT OR IGNORE INTO user_station_controls(username,station_uuid) VALUES(?,?)",
                    (username, station_uuid),
                )
            else:
                con.execute(
                    "DELETE FROM user_station_controls WHERE username = ? AND station_uuid = ?",
                    (username, station_uuid),
                )
        return True, "Control rights updated"

    def get_user_control_stations(self, username: str):
        with self._connect() as con:
            rows = con.execute(
                "SELECT station_uuid FROM user_station_controls WHERE username = ? ORDER BY station_uuid",
                (username,),
            ).fetchall()
            return [r["station_uuid"] for r in rows]

    def can_control_station(self, user, station_uuid: str):
        if user is None:
            return False
        if user.get("role") == "admin":
            return True
        allowed = set(self.get_user_control_stations(user["username"]))
        return station_uuid in allowed

    def _has_data_permission(self, user, instrument_uuid: str, column: str):
        policy = self.get_policy(instrument_uuid)
        if policy == "open":
            return True
        if user is None:
            return False
        if user.get("role") == "admin":
            return True
        with self._connect() as con:
            row = con.execute(
                f"SELECT {column} AS allowed FROM user_instruments WHERE username = ? AND instrument_uuid = ?",
                (user["username"], instrument_uuid),
            ).fetchone()
        return bool(row and row["allowed"])

    def can_browse(self, user, instrument_uuid: str):
        # Downloading an interval necessarily includes reading its rows.
        return self._has_data_permission(user, instrument_uuid, "can_browse") or self._has_data_permission(
            user, instrument_uuid, "can_download"
        )

    def can_download(self, user, instrument_uuid: str):
        return self._has_data_permission(user, instrument_uuid, "can_download")

    def list_all_user_instruments(self):
        """{username: [instrument_uuid, ...]} for every user with an assignment."""
        out: dict[str, list[str]] = {}
        with self._connect() as con:
            for row in con.execute(
                "SELECT username,instrument_uuid FROM user_instruments"
                " WHERE can_browse = 1 OR can_download = 1 ORDER BY instrument_uuid"
            ):
                out.setdefault(row["username"], []).append(row["instrument_uuid"])
        return out

    def list_all_user_download_stations(self):
        out: dict[str, list[str]] = {}
        with self._connect() as con:
            for row in con.execute(
                "SELECT username,instrument_uuid FROM user_instruments WHERE can_download = 1 ORDER BY instrument_uuid"
            ):
                out.setdefault(row["username"], []).append(row["instrument_uuid"])
        return out

    def list_all_user_station_controls(self):
        """{username: [station_uuid, ...]} for every user with chart-control rights."""
        out: dict[str, list[str]] = {}
        with self._connect() as con:
            for row in con.execute("SELECT username,station_uuid FROM user_station_controls ORDER BY station_uuid"):
                out.setdefault(row["username"], []).append(row["station_uuid"])
        return out

    def replace_user_permissions(self, username: str, stations, browse, download, control):
        """Set one user's station permissions for the listed stations only."""
        username = username.strip()
        stations = [str(s).strip() for s in stations if str(s).strip()]
        browse, download, control = set(browse), set(download), set(control)
        with self._lock, self._connect() as con:
            if not con.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                return False, "User not found"
            for station in stations:
                con.execute(
                    "DELETE FROM user_instruments WHERE username = ? AND instrument_uuid = ?", (username, station)
                )
                con.execute(
                    "DELETE FROM user_station_controls WHERE username = ? AND station_uuid = ?", (username, station)
                )
                if station in browse or station in download:
                    con.execute(
                        "INSERT INTO user_instruments"
                        "(username,instrument_uuid,can_browse,can_download) VALUES(?,?,?,?)",
                        (username, station, int(station in browse), int(station in download)),
                    )
                if station in control:
                    con.execute(
                        "INSERT INTO user_station_controls(username,station_uuid) VALUES(?,?)", (username, station)
                    )
        return True, f"Station rights of {username} saved"

    def replace_station_permissions(self, station_uuid: str, usernames, browse, download, control):
        """Set one station's permissions for the listed users only."""
        station_uuid = station_uuid.strip()
        if not station_uuid:
            return False, "Station UUID is required"
        browse, download, control = set(browse), set(download), set(control)
        with self._lock, self._connect() as con:
            known = {row["username"] for row in con.execute("SELECT username FROM users")}
            for username in usernames:
                if username not in known:
                    continue
                con.execute(
                    "DELETE FROM user_instruments WHERE username = ? AND instrument_uuid = ?", (username, station_uuid)
                )
                con.execute(
                    "DELETE FROM user_station_controls WHERE username = ? AND station_uuid = ?",
                    (username, station_uuid),
                )
                if username in browse or username in download:
                    con.execute(
                        "INSERT INTO user_instruments"
                        "(username,instrument_uuid,can_browse,can_download) VALUES(?,?,?,?)",
                        (username, station_uuid, int(username in browse), int(username in download)),
                    )
                if username in control:
                    con.execute(
                        "INSERT INTO user_station_controls(username,station_uuid) VALUES(?,?)",
                        (username, station_uuid),
                    )
        return True, f"User rights for {station_uuid} saved"

    def list_station_user_emails(self, station_uuid: str):
        station_uuid = station_uuid.strip()
        with self._connect() as con:
            policy = self.get_policy(station_uuid)
            emails = set(self.list_admin_emails())
            if policy in ("account", "restricted"):
                rows = con.execute(
                    """
                    SELECT u.email
                    FROM users u
                    JOIN user_instruments ui ON ui.username = u.username
                    WHERE ui.instrument_uuid = ? AND (ui.can_browse = 1 OR ui.can_download = 1)
                      AND u.active = 1 AND u.email IS NOT NULL AND u.email <> ''
                    """,
                    (station_uuid,),
                ).fetchall()
                emails.update([r["email"] for r in rows if isinstance(r["email"], str) and r["email"].strip()])
            return sorted(emails)
