"""Single-use account tokens and long-lived API tokens."""

import hashlib
import secrets
from datetime import timedelta

from sensor_network_pwa.access_store.users import UsersMixin
from sensor_network_pwa.timeutil import now_utc_iso, parse_iso_ts, utc_iso, utc_now


class TokensMixin(UsersMixin):
    @staticmethod
    def _api_token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _api_token_row(row):
        return {
            key: row[key]
            for key in (
                "id",
                "username",
                "name",
                "token_prefix",
                "created_at",
                "updated_at",
                "last_used_at",
                "expires_at",
            )
        }

    def create_api_token(self, username: str, name: str, expires_at: str | None = None):
        token = "snp_" + secrets.token_urlsafe(32)
        now = now_utc_iso()
        with self._lock, self._connect() as con:
            cur = con.execute(
                "INSERT INTO api_tokens(username,name,token_hash,token_prefix,created_at,updated_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (username.strip(), name.strip(), self._api_token_hash(token), token[:12], now, now, expires_at),
            )
            token_id = cur.lastrowid
        return {**self.get_api_token(token_id, username), "token": token}

    def list_api_tokens(self, username: str):
        with self._connect() as con:
            rows = con.execute(
                "SELECT id,username,name,token_prefix,created_at,updated_at,last_used_at,expires_at "
                "FROM api_tokens WHERE username=? ORDER BY name,id",
                (username.strip(),),
            ).fetchall()
        return [self._api_token_row(row) for row in rows]

    def get_api_token(self, token_id: int, username: str):
        with self._connect() as con:
            row = con.execute(
                "SELECT id,username,name,token_prefix,created_at,updated_at,last_used_at,expires_at "
                "FROM api_tokens WHERE id=? AND username=?",
                (token_id, username.strip()),
            ).fetchone()
        return self._api_token_row(row) if row else None

    def update_api_token(self, token_id: int, username: str, name: str, expires_at: str | None):
        with self._lock, self._connect() as con:
            cur = con.execute(
                "UPDATE api_tokens SET name=?,expires_at=?,updated_at=? WHERE id=? AND username=?",
                (name.strip(), expires_at, now_utc_iso(), token_id, username.strip()),
            )
        return cur.rowcount == 1

    def delete_api_token(self, token_id: int, username: str):
        with self._lock, self._connect() as con:
            cur = con.execute("DELETE FROM api_tokens WHERE id=? AND username=?", (token_id, username.strip()))
        return cur.rowcount == 1

    def authenticate_api_token(self, token: str):
        if not token or not token.startswith("snp_"):
            return None
        now = utc_now()
        token_hash = self._api_token_hash(token)
        with self._connect() as con:
            row = con.execute(
                "SELECT id,username,expires_at FROM api_tokens WHERE token_hash=?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            expires = parse_iso_ts(row["expires_at"]) if row["expires_at"] else None
            if expires is not None and expires <= now:
                return None
            token_id = row["id"]
            username = row["username"]
        user = self.get_user(username)
        if not user or not int(user.get("active") or 0):
            return None
        if user.get("role") != "admin" and not user.get("can_api"):
            return None
        with self._lock, self._connect() as con:
            con.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?", (utc_iso(now), token_id))
        return user

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
