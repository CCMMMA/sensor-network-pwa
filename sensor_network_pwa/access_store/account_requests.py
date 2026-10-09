"""Account requests and their single-use onboarding tokens."""

import secrets
import sqlite3
from datetime import timedelta

from werkzeug.security import generate_password_hash

from sensor_network_pwa.access_store.users import UsersMixin
from sensor_network_pwa.timeutil import now_utc_iso, parse_iso_ts, utc_now
from sensor_network_pwa.validation import is_valid_email, validate_password_strength


class AccountRequestsMixin(UsersMixin):
    def create_account_request(self, email: str, message: str):
        email = email.strip()
        if not email:
            return False, "Email is required"
        if not is_valid_email(email):
            return False, "Email address is not valid"
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT INTO account_requests(username,email,password_hash,message,status,created_at,reviewed_by)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    "",
                    email,
                    "",
                    message.strip(),
                    "pending",
                    now_utc_iso(),
                    None,
                ),
            )
        return True, "Request submitted"

    def list_account_requests(self, status=None):
        query = "SELECT id,username,email,message,status,created_at,reviewed_by,reviewed_at FROM account_requests"
        params: tuple = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY created_at DESC"
        with self._connect() as con:
            rows = con.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def approve_request(self, request_id: int, admin_username: str):
        with self._lock, self._connect() as con:
            req = con.execute(
                "SELECT id,username,email,password_hash,status FROM account_requests WHERE id = ?",
                (request_id,),
            ).fetchone()
            if req is None:
                return False, "Request not found"
            if req["status"] != "pending":
                return False, f"Request already {req['status']}"
            con.execute(
                "UPDATE account_requests SET status = 'approved', reviewed_by = ?, reviewed_at = ? WHERE id = ?",
                (admin_username, now_utc_iso(), request_id),
            )
            return True, "Request approved"

    def reject_request(self, request_id: int, admin_username: str):
        with self._lock, self._connect() as con:
            req = con.execute("SELECT status FROM account_requests WHERE id = ?", (request_id,)).fetchone()
            if req is None:
                return False, "Request not found"
            if req["status"] != "pending":
                return False, f"Request already {req['status']}"
            con.execute(
                "UPDATE account_requests SET status = 'rejected', reviewed_by = ?, reviewed_at = ? WHERE id = ?",
                (admin_username, now_utc_iso(), request_id),
            )
            return True, "Request rejected"

    def get_account_request(self, request_id: int):
        with self._connect() as con:
            row = con.execute(
                "SELECT id,username,email,message,status,created_at,reviewed_by,reviewed_at"
                " FROM account_requests WHERE id = ?",
                (request_id,),
            ).fetchone()
        return dict(row) if row else None

    def create_account_request_token(self, request_id: int, ttl_hours: int = 48):
        token = secrets.token_urlsafe(32)
        now = utc_now()
        expires = now + timedelta(hours=max(1, ttl_hours))
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT INTO account_request_tokens(token,request_id,expires_at,created_at,used_at)"
                " VALUES(?,?,?,?,NULL)",
                (
                    token,
                    int(request_id),
                    expires.isoformat().replace("+00:00", "Z"),
                    now.isoformat().replace("+00:00", "Z"),
                ),
            )
        return token

    def get_account_request_for_token(self, token: str):
        if not token:
            return None
        now = utc_now()
        with self._connect() as con:
            row = con.execute(
                "SELECT token,request_id,expires_at,used_at FROM account_request_tokens WHERE token = ?",
                (token,),
            ).fetchone()
        if row is None or row["used_at"]:
            return None
        exp = parse_iso_ts(row["expires_at"])
        if exp is None or exp < now:
            return None
        request_row = self.get_account_request(int(row["request_id"]))
        if not request_row or request_row.get("status") != "approved":
            return None
        return request_row

    def complete_account_request(self, token: str, username: str, password: str):
        username = username.strip()
        if not username:
            return False, "Username is required"
        if self.username_exists(username):
            return False, "Username already exists"
        ok, password_msg = validate_password_strength(password)
        if not ok:
            return False, password_msg
        request_row = self.get_account_request_for_token(token)
        if request_row is None:
            return False, "Invalid or expired onboarding link"
        with self._lock:
            try:
                with self._connect() as con:
                    # Claim the token first so concurrent requests cannot both use it.
                    cur = con.execute(
                        "UPDATE account_request_tokens SET used_at = ? WHERE token = ? AND used_at IS NULL",
                        (now_utc_iso(), token),
                    )
                    if cur.rowcount != 1:
                        return False, "Invalid or expired onboarding link"
                    con.execute(
                        "INSERT INTO users(username,password_hash,email,role,active,created_at) VALUES(?,?,?,?,?,?)",
                        (username, generate_password_hash(password), request_row["email"], "user", 1, now_utc_iso()),
                    )
                    con.execute(
                        "UPDATE account_requests SET username = ?, status = 'completed' WHERE id = ?",
                        (username, int(request_row["id"])),
                    )
            except sqlite3.IntegrityError:
                return False, "Username already exists"
        return True, "Account created"
