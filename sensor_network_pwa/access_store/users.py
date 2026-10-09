"""User accounts, authentication and passwords."""

import sqlite3

from werkzeug.security import check_password_hash, generate_password_hash

from sensor_network_pwa.access_store.base import StoreBase
from sensor_network_pwa.log import logger
from sensor_network_pwa.timeutil import now_utc_iso
from sensor_network_pwa.validation import is_valid_email, validate_password_strength

WEAK_ADMIN_PASSWORDS = frozenset({"admin", "change-me", "change-me-now", "replace-with-strong-password"})


class UsersMixin(StoreBase):
    def ensure_admin(self, username: str, password: str):
        readonly_message = (
            f"Cannot initialize admin user: auth DB is read-only ({self.db_path}). "
            "Set authDbPath to a writable location."
        )
        with self._lock, self._readonly_as_runtime_error(readonly_message), self._connect() as con:
            row = con.execute("SELECT username FROM users WHERE username = ?", (username,)).fetchone()
            if row is not None:
                return
            weak = password in WEAK_ADMIN_PASSWORDS
            # Gunicorn workers start together: on a new database another one may insert first.
            cur = con.execute(
                "INSERT OR IGNORE INTO users(username,password_hash,email,role,active,created_at,force_password_change)"
                " VALUES(?,?,?,?,?,?,?)",
                (username, generate_password_hash(password), "", "admin", 1, now_utc_iso(), 1 if weak else 0),
            )
            if cur.rowcount == 0:
                return
            logger.info("Created default admin user '%s'", username)
            if weak:
                logger.warning(
                    "Admin user '%s' was created with a default/sample password; "
                    "a password change is required at first login",
                    username,
                )

    def get_user(self, username: str):
        with self._connect() as con:
            row = con.execute(
                "SELECT username,email,role,active,created_at,force_password_change FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            return dict(row) if row else None

    def list_users(self):
        with self._connect() as con:
            rows = con.execute(
                "SELECT username,email,role,active,created_at,force_password_change FROM users ORDER BY username"
            ).fetchall()
            return [dict(r) for r in rows]

    def find_active_user_by_identity(self, identity: str):
        identity = identity.strip()
        if not identity:
            return None
        with self._connect() as con:
            row = con.execute(
                """
                SELECT username,email,role,active,created_at,force_password_change
                FROM users
                WHERE active = 1 AND (username = ? OR lower(email) = lower(?))
                LIMIT 1
                """,
                (identity, identity),
            ).fetchone()
        return dict(row) if row else None

    def authenticate(self, username: str, password: str):
        with self._connect() as con:
            row = con.execute(
                "SELECT username,password_hash,email,role,active,created_at,force_password_change"
                " FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        if row is None:
            return None
        if int(row["active"]) != 1:
            return None
        if not check_password_hash(row["password_hash"], password):
            return None
        return {
            "username": row["username"],
            "email": row["email"],
            "role": row["role"],
            "active": int(row["active"]),
            "created_at": row["created_at"],
            "force_password_change": int(row["force_password_change"] or 0),
        }

    def create_user(self, username: str, password: str, email: str, role: str = "user", active: int = 1):
        username = username.strip()
        if not username:
            return False, "Username is required"
        if self.get_user(username):
            return False, "User already exists"
        ok, password_msg = validate_password_strength(password)
        if not ok:
            return False, password_msg
        role = "admin" if role == "admin" else "user"
        with self._lock:
            try:
                with self._connect() as con:
                    con.execute(
                        "INSERT INTO users(username,password_hash,email,role,active,created_at) VALUES(?,?,?,?,?,?)",
                        (username, generate_password_hash(password), email.strip(), role, int(active), now_utc_iso()),
                    )
                return True, "User created"
            except sqlite3.IntegrityError:
                return False, "User already exists"
            except sqlite3.OperationalError as e:
                if self._is_readonly_error(e):
                    return (
                        False,
                        f"Auth database is read-only ({self.db_path}). "
                        "Configure a writable authDbPath (for example /data/collector_auth.sqlite).",
                    )
                return False, f"Database error: {e}"

    def username_exists(self, username: str):
        username = username.strip()
        if not username:
            return False
        with self._connect() as con:
            row = con.execute("SELECT 1 FROM users WHERE username = ? LIMIT 1", (username,)).fetchone()
        return bool(row)

    def update_user(self, username: str, email=None, role=None, active=None):
        """Change a user's email, role or active flag; the last active admin is protected."""
        username = username.strip()
        with self._lock, self._connect() as con:
            row = con.execute("SELECT role,active FROM users WHERE username = ?", (username,)).fetchone()
            if row is None:
                return False, "User not found"
            new_role = row["role"] if role is None else ("admin" if role == "admin" else "user")
            new_active = int(row["active"]) if active is None else (1 if active else 0)
            was_admin = row["role"] == "admin" and int(row["active"]) == 1
            if was_admin and not (new_role == "admin" and new_active == 1):
                others = con.execute(
                    "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND active = 1 AND username <> ?",
                    (username,),
                ).fetchone()["n"]
                if others == 0:
                    return False, "At least one active administrator is required"
            if email is not None:
                email = email.strip()
                if email and not is_valid_email(email):
                    return False, "Email address is not valid"
                con.execute("UPDATE users SET email = ? WHERE username = ?", (email, username))
            con.execute("UPDATE users SET role = ?, active = ? WHERE username = ?", (new_role, new_active, username))
        return True, f"User {username} updated"

    def set_force_password_change(self, username: str, force: bool):
        with self._lock, self._connect() as con:
            cur = con.execute(
                "UPDATE users SET force_password_change = ? WHERE username = ?",
                (1 if force else 0, username.strip()),
            )
            if cur.rowcount <= 0:
                return False, "User not found"
        return True, "Password-change policy updated"

    def change_password(self, username: str, new_password: str, force_password_change: bool = False):
        ok, password_msg = validate_password_strength(new_password)
        if not ok:
            return False, password_msg
        with self._lock, self._connect() as con:
            cur = con.execute(
                "UPDATE users SET password_hash = ?, force_password_change = ? WHERE username = ?",
                (generate_password_hash(new_password), 1 if force_password_change else 0, username.strip()),
            )
            if cur.rowcount <= 0:
                return False, "User not found"
        return True, "Password updated"

    def list_admin_emails(self):
        with self._connect() as con:
            rows = con.execute(
                "SELECT email FROM users WHERE role = 'admin' AND active = 1 AND email IS NOT NULL AND email <> ''"
            ).fetchall()
            return [r["email"] for r in rows if isinstance(r["email"], str) and r["email"].strip()]
