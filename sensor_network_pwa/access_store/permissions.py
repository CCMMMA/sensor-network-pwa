"""Station policies and per-user download and control rights."""

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

    def set_user_instrument_access(self, username: str, instrument_uuid: str, allow: bool):
        username = username.strip()
        instrument_uuid = instrument_uuid.strip()
        if not username or not instrument_uuid:
            return False, "Username and instrument UUID are required"

        with self._lock, self._connect() as con:
            user_exists = con.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
            if not user_exists:
                return False, "User not found"

            if allow:
                con.execute(
                    "INSERT OR IGNORE INTO user_instruments(username,instrument_uuid) VALUES(?,?)",
                    (username, instrument_uuid),
                )
            else:
                con.execute(
                    "DELETE FROM user_instruments WHERE username = ? AND instrument_uuid = ?",
                    (username, instrument_uuid),
                )
        return True, "Access updated"

    def get_user_instruments(self, username: str):
        with self._connect() as con:
            rows = con.execute(
                "SELECT instrument_uuid FROM user_instruments WHERE username = ? ORDER BY instrument_uuid",
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

    def can_download(self, user, instrument_uuid: str):
        policy = self.get_policy(instrument_uuid)
        if policy == "open":
            return True
        if user is None:
            return False
        if user.get("role") == "admin":
            return True
        if policy == "account":
            return True
        if policy == "restricted":
            allowed = set(self.get_user_instruments(user["username"]))
            return instrument_uuid in allowed
        return False

    def list_all_user_instruments(self):
        """{username: [instrument_uuid, ...]} for every user with an assignment."""
        out: dict[str, list[str]] = {}
        with self._connect() as con:
            for row in con.execute("SELECT username,instrument_uuid FROM user_instruments ORDER BY instrument_uuid"):
                out.setdefault(row["username"], []).append(row["instrument_uuid"])
        return out

    def list_all_user_station_controls(self):
        """{username: [station_uuid, ...]} for every user with chart-control rights."""
        out: dict[str, list[str]] = {}
        with self._connect() as con:
            for row in con.execute("SELECT username,station_uuid FROM user_station_controls ORDER BY station_uuid"):
                out.setdefault(row["username"], []).append(row["station_uuid"])
        return out

    def replace_user_permissions(self, username: str, stations, access, control):
        """Set one user's data access and chart control for the listed stations only."""
        username = username.strip()
        stations = [str(s).strip() for s in stations if str(s).strip()]
        access, control = set(access), set(control)
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
                if station in access:
                    con.execute(
                        "INSERT INTO user_instruments(username,instrument_uuid) VALUES(?,?)", (username, station)
                    )
                if station in control:
                    con.execute(
                        "INSERT INTO user_station_controls(username,station_uuid) VALUES(?,?)", (username, station)
                    )
        return True, f"Station rights of {username} saved"

    def replace_station_permissions(self, station_uuid: str, usernames, access, control):
        """Set one station's data access and chart control for the listed users only."""
        station_uuid = station_uuid.strip()
        if not station_uuid:
            return False, "Station UUID is required"
        access, control = set(access), set(control)
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
                if username in access:
                    con.execute(
                        "INSERT INTO user_instruments(username,instrument_uuid) VALUES(?,?)", (username, station_uuid)
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
            if policy == "account":
                rows = con.execute(
                    "SELECT email FROM users WHERE role = 'user' AND active = 1 AND email IS NOT NULL AND email <> ''"
                ).fetchall()
                emails.update([r["email"] for r in rows if isinstance(r["email"], str) and r["email"].strip()])
            elif policy == "restricted":
                rows = con.execute(
                    """
                    SELECT u.email
                    FROM users u
                    JOIN user_instruments ui ON ui.username = u.username
                    WHERE ui.instrument_uuid = ? AND u.active = 1 AND u.email IS NOT NULL AND u.email <> ''
                    """,
                    (station_uuid,),
                ).fetchall()
                emails.update([r["email"] for r in rows if isinstance(r["email"], str) and r["email"].strip()])
            return sorted(emails)
