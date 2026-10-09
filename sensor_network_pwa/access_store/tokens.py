"""Single-use login and password-reset tokens."""

import secrets
from datetime import timedelta

from sensor_network_pwa.access_store.users import UsersMixin
from sensor_network_pwa.timeutil import now_utc_iso, parse_iso_ts, utc_iso, utc_now


class TokensMixin(UsersMixin):
    def create_login_token(self, username: str, ttl_minutes: int = 60):
        token = secrets.token_urlsafe(32)
        now = utc_now()
        expires = now + timedelta(minutes=max(1, ttl_minutes))
        with self._lock, self._connect() as con:
            # Reminder emails create a token each; keep the table from growing.
            con.execute("DELETE FROM login_tokens WHERE expires_at < ?", (utc_iso(now - timedelta(days=1)),))
            con.execute(
                "INSERT INTO login_tokens(token,username,expires_at,created_at,used_at) VALUES(?,?,?,?,NULL)",
                (
                    token,
                    username.strip(),
                    expires.isoformat().replace("+00:00", "Z"),
                    now.isoformat().replace("+00:00", "Z"),
                ),
            )
        return token

    def consume_login_token(self, token: str):
        if not token:
            return None
        now = utc_now()
        with self._lock, self._connect() as con:
            row = con.execute(
                "SELECT token,username,expires_at,used_at FROM login_tokens WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None:
                return None
            if row["used_at"]:
                return None
            exp = parse_iso_ts(row["expires_at"])
            if exp is None or exp < now:
                return None
            cur = con.execute(
                "UPDATE login_tokens SET used_at = ? WHERE token = ? AND used_at IS NULL",
                (now_utc_iso(), token),
            )
            if cur.rowcount != 1:
                return None
            username = row["username"]
        user = self.get_user(username)
        return user if user and int(user.get("active") or 0) == 1 else None

    def create_password_reset_token(self, username: str, ttl_minutes: int = 30):
        token = secrets.token_urlsafe(32)
        now = utc_now()
        expires = now + timedelta(minutes=max(1, ttl_minutes))
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT INTO password_reset_tokens(token,username,expires_at,created_at,used_at) VALUES(?,?,?,?,NULL)",
                (
                    token,
                    username.strip(),
                    expires.isoformat().replace("+00:00", "Z"),
                    now.isoformat().replace("+00:00", "Z"),
                ),
            )
        return token

    def get_password_reset_user(self, token: str):
        if not token:
            return None
        now = utc_now()
        with self._connect() as con:
            row = con.execute(
                "SELECT token,username,expires_at,used_at FROM password_reset_tokens WHERE token = ?",
                (token,),
            ).fetchone()
        if row is None or row["used_at"]:
            return None
        exp = parse_iso_ts(row["expires_at"])
        if exp is None or exp < now:
            return None
        return self.get_user(row["username"])

    def consume_password_reset_token(self, token: str):
        user = self.get_password_reset_user(token)
        if user is None:
            return None
        with self._lock, self._connect() as con:
            cur = con.execute(
                "UPDATE password_reset_tokens SET used_at = ? WHERE token = ? AND used_at IS NULL",
                (now_utc_iso(), token),
            )
            if cur.rowcount != 1:
                return None
        return user
