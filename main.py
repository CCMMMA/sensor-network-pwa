import argparse
import csv
import functools
import gzip
import hashlib
import io
import json
import logging
import math
import mimetypes
import os
import re
import secrets
import signal
import sqlite3
import smtplib
import statistics
import struct
import sys
import tempfile
import threading
import zipfile
import zlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from flask import Flask, abort, flash, jsonify, redirect, render_template_string, request, send_file, session, url_for
from influxdb_client import InfluxDBClient
from markupsafe import escape
from werkzeug.security import check_password_hash, generate_password_hash


# ----------------------------
# Logging
# ----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("sensor_network_pwa")


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def send_email(cfg: dict, recipients, subject: str, body_text: str):
    recipients = [r.strip() for r in (recipients or []) if isinstance(r, str) and r.strip()]
    if not recipients:
        return False
    if not cfg.get("smtp_enabled"):
        logger.info("SMTP disabled; skipped email subject=%s recipients=%d", subject, len(recipients))
        return False
    if not str(cfg.get("smtp_host", "") or "").strip():
        logger.info("SMTP host not configured; skipped email subject=%s recipients=%d", subject, len(recipients))
        return False

    try:
        msg = EmailMessage()
        msg["Subject"] = " ".join(str(subject).split())
        msg["From"] = cfg.get("smtp_from", "")
        msg["To"] = ", ".join(recipients)
        msg.set_content(body_text)

        smtp_host = cfg.get("smtp_host", "")
        smtp_port = int(cfg.get("smtp_port", 25))
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as smtp:
            if cfg.get("smtp_use_tls", True):
                smtp.starttls()
            if cfg.get("smtp_user"):
                smtp.login(cfg.get("smtp_user", ""), cfg.get("smtp_pass", ""))
            smtp.send_message(msg)
        logger.info("Email sent subject=%s recipients=%d", subject, len(recipients))
        return True
    except Exception as e:
        logger.warning("Email send failed subject=%s recipients=%d err=%s", subject, len(recipients), e)
        return False


# ----------------------------
# Config helpers
# ----------------------------
def parse_boolish(value, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off", ""):
        return False
    return bool(default)


def cfg_value(raw: dict, keys, env_name=None, default=None):
    for key in keys:
        if key in raw and raw.get(key) is not None:
            return raw.get(key)
    if env_name:
        env = os.getenv(env_name)
        if env is not None:
            return env
    return default


def cfg_required(raw: dict, keys, env_name):
    value = cfg_value(raw, keys, env_name=env_name, default=None)
    if value is None or value == "":
        raise RuntimeError(f"Missing required config key(s) {keys} (or env {env_name})")
    return value


def parse_args():
    parser = argparse.ArgumentParser(
        description="Web application for the sensor network (reads the collector CSV storage and InfluxDB)"
    )
    parser.add_argument("--config", default="config.json", help="Config file path (default: config.json)")
    parser.add_argument(
        "--watchdog-only",
        action="store_true",
        help="Run only the anomaly watchdog (use next to a Gunicorn deployment of webapp_wsgi)",
    )
    return parser.parse_args()


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise RuntimeError("Config file must contain a JSON object")

    log_level = str(cfg_value(raw, ["logLevel", "log_level"], env_name="LOG_LEVEL", default="INFO")).upper()

    # The storage root is written by sensor-network-collector; the web application only reads it.
    storage_root = cfg_value(raw, ["pathStorage", "storage_root"], env_name="STORAGE_ROOT", default=None)

    # InfluxDB is optional here: it is queried only when it is configured.
    influx_url = cfg_value(raw, ["influxdbUrl", "influxdb_url"], env_name="INFLUXDB_URL", default="")
    enable_influx = parse_boolish(
        cfg_value(raw, ["influxdb", "influxdb_enabled"], env_name="INFLUXDB_ENABLED", default=bool(influx_url)),
        bool(influx_url),
    )

    smtp_port_raw = cfg_value(raw, ["smtpPort"], env_name="SMTP_PORT", default=None)
    smtp_user_raw = cfg_value(raw, ["smtpUser"], env_name="SMTP_USER", default=None)
    smtp_pass_raw = cfg_value(raw, ["smtpPass"], env_name="SMTP_PASS", default=None)
    smtp_use_tls_raw = cfg_value(raw, ["smtpUseTls"], env_name="SMTP_USE_TLS", default=None)

    smtp_user = str(smtp_user_raw or "")
    smtp_pass = str(smtp_pass_raw or "")
    smtp_port = int(smtp_port_raw) if smtp_port_raw not in (None, "") else 25
    if smtp_use_tls_raw is None:
        smtp_use_tls = not (smtp_port == 25 and not smtp_user)
    else:
        smtp_use_tls = parse_boolish(smtp_use_tls_raw, True)

    cfg = {
        "log_level": log_level,
        "enable_influx": enable_influx,
        "storage_root": str(storage_root) if storage_root else None,
        "influx_measurement": str(
            cfg_value(raw, ["influxMeasurement", "influx_measurement"], env_name="INFLUX_MEASUREMENT", default="mqtt_data")
        ),
        "http_host": str(cfg_value(raw, ["httpHost"], env_name="HTTP_HOST", default="0.0.0.0")),
        "http_port": int(cfg_value(raw, ["httpPort"], env_name="HTTP_PORT", default=8080)),
        "auth_db_path": str(
            cfg_value(raw, ["authDbPath"], env_name="AUTH_DB_PATH", default="")
            or (str(Path(storage_root) / "collector_auth.sqlite") if storage_root else "collector_auth.sqlite")
        ),
        "web_session_secret": str(cfg_value(raw, ["webSessionSecret"], env_name="WEB_SESSION_SECRET", default="") or ""),
        "admin_user": str(cfg_value(raw, ["adminUser"], env_name="ADMIN_USER", default="admin") or "admin"),
        "admin_password": str(
            cfg_value(raw, ["adminPassword"], env_name="ADMIN_PASSWORD", default="admin") or "admin"
        ),
        "web_app_logo": str(cfg_value(raw, ["webAppLogo"], env_name="WEB_APP_LOGO", default="") or ""),
        "web_app_name": str(
            cfg_value(raw, ["webAppName"], env_name="WEB_APP_NAME", default="") or ""
        ).strip() or "Sensor Network Data Portal",
        "web_app_short_name": str(
            cfg_value(raw, ["webAppShortName"], env_name="WEB_APP_SHORT_NAME", default="") or ""
        ).strip() or "Sensor Network",
        "web_app_link": str(cfg_value(raw, ["webAppLink"], env_name="WEB_APP_LINK", default="") or "").strip(),
        "web_info_link": str(cfg_value(raw, ["webInfoLink"], env_name="WEB_INFO_LINK", default="") or "").strip(),
        "base_url": str(cfg_value(raw, ["baseUrl"], env_name="BASE_URL", default="") or "").strip(),
        "smtp_enabled": parse_boolish(cfg_value(raw, ["smtpEnabled"], env_name="SMTP_ENABLED", default=False), False),
        "smtp_host": str(cfg_value(raw, ["smtpHost"], env_name="SMTP_HOST", default="") or "").strip(),
        "smtp_port": smtp_port,
        "smtp_user": smtp_user,
        "smtp_pass": smtp_pass,
        "smtp_from": str(cfg_value(raw, ["smtpFrom"], env_name="SMTP_FROM", default="") or "").strip(),
        "smtp_use_tls": smtp_use_tls,
        "watchdog_interval_sec": int(
            cfg_value(raw, ["watchdogIntervalSec"], env_name="WATCHDOG_INTERVAL_SEC", default=60)
        ),
    }

    # Only the units in each entry's meta are used here, to label fields in tables and charts.
    raw_map = cfg_value(raw, ["signalkPathMap", "signalk_path_map"], env_name="SIGNALK_PATH_MAP", default={})
    if isinstance(raw_map, str):
        try:
            raw_map = json.loads(raw_map)
        except json.JSONDecodeError:
            logger.warning("Invalid SIGNALK_PATH_MAP JSON, using empty map")
            raw_map = {}

    parsed_map = {}
    if isinstance(raw_map, dict):
        for k, v in raw_map.items():
            if not isinstance(v, dict):
                continue
            meta = v.get("meta")
            if meta is None and "meta:" in v:
                meta = v.get("meta:")
            meta = sanitize_signalk_meta(meta)
            if meta is not None:
                parsed_map[str(k)] = {"meta": meta}

    cfg["signalk_path_map"] = parsed_map

    if cfg["enable_influx"]:
        cfg["influxdb_url"] = str(cfg_required(raw, ["influxdbUrl", "influxdb_url"], "INFLUXDB_URL"))
        cfg["influxdb_token"] = str(cfg_required(raw, ["influxdbToken", "influxdb_token"], "INFLUXDB_TOKEN"))
        cfg["influxdb_org"] = str(cfg_required(raw, ["influxdbOrg", "influxdb_org"], "INFLUXDB_ORG"))
        cfg["influxdb_bucket"] = str(cfg_required(raw, ["influxdbBucket", "influxdb_bucket"], "INFLUXDB_BUCKET"))

    if cfg["smtp_enabled"] and not cfg["smtp_from"]:
        raise RuntimeError("SMTP enabled but smtpFrom is missing")

    if not cfg["base_url"]:
        cfg["base_url"] = f"http://{cfg['http_host']}:{cfg['http_port']}"

    cfg["config_path"] = str(Path(path).resolve())
    return cfg


def apply_log_level(cfg: dict):
    logging.getLogger().setLevel(getattr(logging, cfg["log_level"], logging.INFO))


def utc_now():
    return datetime.now(timezone.utc)


# ----------------------------
# Web GUI auth/policy store
# ----------------------------
# Built-in default and documented sample values of adminPassword.
# Documented sample values of webSessionSecret: public, so never used to sign sessions.
PLACEHOLDER_SESSION_SECRETS = frozenset(
    {
        "replace-with-a-strong-random-secret",
        "replace-with-strong-random-secret",
        "replace-with-strong-secret",
    }
)

WEAK_ADMIN_PASSWORDS = frozenset(
    {"admin", "change-me", "change-me-now", "replace-with-strong-password"}
)


class AccessStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._verify_writable()

    @contextmanager
    def _connect(self):
        # The collector watchdog and every web worker share this file.
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def _init_schema(self):
        with self._lock:
            try:
                with self._connect() as con:
                    con.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS users (
                            username TEXT PRIMARY KEY,
                            password_hash TEXT NOT NULL,
                            email TEXT,
                            role TEXT NOT NULL DEFAULT 'user',
                            active INTEGER NOT NULL DEFAULT 1,
                            created_at TEXT NOT NULL,
                            force_password_change INTEGER NOT NULL DEFAULT 0
                        );

                        CREATE TABLE IF NOT EXISTS account_requests (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            username TEXT NOT NULL,
                            email TEXT,
                            password_hash TEXT NOT NULL,
                            message TEXT,
                            status TEXT NOT NULL DEFAULT 'pending',
                            created_at TEXT NOT NULL,
                            reviewed_by TEXT
                        );

                        CREATE TABLE IF NOT EXISTS instrument_policies (
                            instrument_uuid TEXT PRIMARY KEY,
                            policy TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            updated_by TEXT
                        );

                        CREATE TABLE IF NOT EXISTS user_instruments (
                            username TEXT NOT NULL,
                            instrument_uuid TEXT NOT NULL,
                            PRIMARY KEY (username, instrument_uuid)
                        );

                        CREATE TABLE IF NOT EXISTS user_station_controls (
                            username TEXT NOT NULL,
                            station_uuid TEXT NOT NULL,
                            PRIMARY KEY (username, station_uuid)
                        );

                        CREATE TABLE IF NOT EXISTS login_tokens (
                            token TEXT PRIMARY KEY,
                            username TEXT NOT NULL,
                            expires_at TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            used_at TEXT
                        );

                        CREATE TABLE IF NOT EXISTS password_reset_tokens (
                            token TEXT PRIMARY KEY,
                            username TEXT NOT NULL,
                            expires_at TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            used_at TEXT
                        );

                        CREATE TABLE IF NOT EXISTS account_request_tokens (
                            token TEXT PRIMARY KEY,
                            request_id INTEGER NOT NULL,
                            expires_at TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            used_at TEXT
                        );

                        CREATE TABLE IF NOT EXISTS anomalies (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            station_uuid TEXT NOT NULL,
                            anomaly_type TEXT NOT NULL,
                            message TEXT NOT NULL,
                            severity TEXT NOT NULL DEFAULT 'warning',
                            status TEXT NOT NULL DEFAULT 'open',
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            resolved_at TEXT
                        );

                        CREATE TABLE IF NOT EXISTS anomaly_silence (
                            station_uuid TEXT NOT NULL,
                            anomaly_type TEXT NOT NULL,
                            silenced_until TEXT NOT NULL,
                            silenced_by TEXT NOT NULL,
                            created_at TEXT NOT NULL,
                            PRIMARY KEY (station_uuid, anomaly_type)
                        );

                        CREATE TABLE IF NOT EXISTS station_logos (
                            station_uuid TEXT PRIMARY KEY,
                            logo_path TEXT NOT NULL,
                            uploaded_by TEXT NOT NULL,
                            uploaded_at TEXT NOT NULL
                        );

                        CREATE TABLE IF NOT EXISTS station_chart_settings (
                            station_uuid TEXT NOT NULL,
                            series_key TEXT NOT NULL,
                            y_min REAL,
                            y_max REAL,
                            y_step REAL,
                            updated_at TEXT NOT NULL,
                            updated_by TEXT NOT NULL,
                            PRIMARY KEY (station_uuid, series_key)
                        );

                        CREATE TABLE IF NOT EXISTS app_settings (
                            key TEXT PRIMARY KEY,
                            value TEXT NOT NULL
                        );

                        CREATE TABLE IF NOT EXISTS write_probe (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            created_at TEXT NOT NULL
                        );
                        """
                    )
                    # Backward-compatible migrations for existing DBs.
                    try:
                        con.execute("ALTER TABLE users ADD COLUMN force_password_change INTEGER NOT NULL DEFAULT 0")
                    except sqlite3.OperationalError:
                        pass
                    try:
                        con.execute("ALTER TABLE account_requests ADD COLUMN reviewed_at TEXT")
                    except sqlite3.OperationalError:
                        pass
            except sqlite3.OperationalError as e:
                if self._is_readonly_error(e):
                    raise RuntimeError(
                        f"Auth DB is read-only: {self.db_path}. "
                        "Set authDbPath to a writable location (for example under /data)."
                    ) from e
                raise

    def _is_readonly_error(self, err: Exception) -> bool:
        msg = str(err).lower()
        return "readonly" in msg or "read-only" in msg

    def _verify_writable(self):
        """Fail fast on startup if the auth DB is not writable."""
        with self._lock:
            try:
                with self._connect() as con:
                    cur = con.execute(
                        "INSERT INTO write_probe(created_at) VALUES(?)",
                        (now_utc_iso(),),
                    )
                    probe_id = cur.lastrowid
                    if probe_id is not None:
                        con.execute("DELETE FROM write_probe WHERE id = ?", (probe_id,))
            except sqlite3.OperationalError as e:
                if self._is_readonly_error(e):
                    raise RuntimeError(
                        f"Auth DB is read-only: {self.db_path}. "
                        "Set authDbPath to a writable location (for example under /data)."
                    ) from e
                raise

    def ensure_admin(self, username: str, password: str):
        with self._lock:
            try:
                with self._connect() as con:
                    row = con.execute("SELECT username FROM users WHERE username = ?", (username,)).fetchone()
                    if row is None:
                        weak = password in WEAK_ADMIN_PASSWORDS
                        con.execute(
                            "INSERT INTO users(username,password_hash,email,role,active,created_at,force_password_change)"
                            " VALUES(?,?,?,?,?,?,?)",
                            (username, generate_password_hash(password), "", "admin", 1, now_utc_iso(), 1 if weak else 0),
                        )
                        logger.info("Created default admin user '%s'", username)
                        if weak:
                            logger.warning(
                                "Admin user '%s' was created with a default/sample password; "
                                "a password change is required at first login",
                                username,
                            )
            except sqlite3.OperationalError as e:
                if self._is_readonly_error(e):
                    raise RuntimeError(
                        f"Cannot initialize admin user: auth DB is read-only ({self.db_path}). "
                        "Set authDbPath to a writable location."
                    ) from e
                raise

    def get_or_create_session_secret(self) -> str:
        """Random session secret shared by every process that opens this database."""
        with self._lock:
            with self._connect() as con:
                con.execute(
                    "INSERT OR IGNORE INTO app_settings(key,value) VALUES('session_secret',?)",
                    (secrets.token_hex(32),),
                )
                row = con.execute("SELECT value FROM app_settings WHERE key = 'session_secret'").fetchone()
        return row["value"]

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
                "SELECT username,password_hash,email,role,active,created_at,force_password_change FROM users WHERE username = ?",
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
                        f"Auth database is read-only ({self.db_path}). Configure a writable authDbPath (for example /data/collector_auth.sqlite).",
                    )
                return False, f"Database error: {e}"

    def username_exists(self, username: str):
        username = username.strip()
        if not username:
            return False
        with self._connect() as con:
            row = con.execute("SELECT 1 FROM users WHERE username = ? LIMIT 1", (username,)).fetchone()
        return bool(row)

    def create_account_request(self, email: str, message: str):
        email = email.strip()
        if not email:
            return False, "Email is required"
        if not is_valid_email(email):
            return False, "Email address is not valid"
        with self._lock:
            with self._connect() as con:
                con.execute(
                    "INSERT INTO account_requests(username,email,password_hash,message,status,created_at,reviewed_by) VALUES(?,?,?,?,?,?,?)",
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
        params = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY created_at DESC"
        with self._connect() as con:
            rows = con.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def approve_request(self, request_id: int, admin_username: str):
        with self._lock:
            with self._connect() as con:
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
        with self._lock:
            with self._connect() as con:
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
                "SELECT id,username,email,message,status,created_at,reviewed_by,reviewed_at FROM account_requests WHERE id = ?",
                (request_id,),
            ).fetchone()
        return dict(row) if row else None

    def create_account_request_token(self, request_id: int, ttl_hours: int = 48):
        token = secrets.token_urlsafe(32)
        now = utc_now()
        expires = now + timedelta(hours=max(1, ttl_hours))
        with self._lock:
            with self._connect() as con:
                con.execute(
                    "INSERT INTO account_request_tokens(token,request_id,expires_at,created_at,used_at) VALUES(?,?,?,?,NULL)",
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

    def set_policy(self, instrument_uuid: str, policy: str, updated_by: str):
        if policy not in ("open", "account", "restricted"):
            return False, "Invalid policy"
        instrument_uuid = instrument_uuid.strip()
        if not instrument_uuid:
            return False, "instrument UUID is required"

        with self._lock:
            with self._connect() as con:
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

        with self._lock:
            with self._connect() as con:
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

        with self._lock:
            with self._connect() as con:
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
        out = {}
        with self._connect() as con:
            for row in con.execute("SELECT username,instrument_uuid FROM user_instruments ORDER BY instrument_uuid"):
                out.setdefault(row["username"], []).append(row["instrument_uuid"])
        return out

    def list_all_user_station_controls(self):
        """{username: [station_uuid, ...]} for every user with chart-control rights."""
        out = {}
        with self._connect() as con:
            for row in con.execute("SELECT username,station_uuid FROM user_station_controls ORDER BY station_uuid"):
                out.setdefault(row["username"], []).append(row["station_uuid"])
        return out

    def replace_user_permissions(self, username: str, stations, access, control):
        """Set one user's data access and chart control for the listed stations only."""
        username = username.strip()
        stations = [str(s).strip() for s in stations if str(s).strip()]
        access, control = set(access), set(control)
        with self._lock:
            with self._connect() as con:
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
                        con.execute("INSERT INTO user_instruments(username,instrument_uuid) VALUES(?,?)", (username, station))
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
        with self._lock:
            with self._connect() as con:
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

    def update_user(self, username: str, email=None, role=None, active=None):
        """Change a user's email, role or active flag; the last active admin is protected."""
        username = username.strip()
        with self._lock:
            with self._connect() as con:
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
        with self._lock:
            with self._connect() as con:
                cur = con.execute(
                    "UPDATE users SET force_password_change = ? WHERE username = ?",
                    (1 if force else 0, username.strip()),
                )
                if cur.rowcount <= 0:
                    return False, "User not found"
        return True, "Password-change policy updated"

    def change_password(self, username: str, new_password: str):
        ok, password_msg = validate_password_strength(new_password)
        if not ok:
            return False, password_msg
        with self._lock:
            with self._connect() as con:
                cur = con.execute(
                    "UPDATE users SET password_hash = ?, force_password_change = 0 WHERE username = ?",
                    (generate_password_hash(new_password), username.strip()),
                )
                if cur.rowcount <= 0:
                    return False, "User not found"
        return True, "Password updated"

    def create_login_token(self, username: str, ttl_minutes: int = 60):
        token = secrets.token_urlsafe(32)
        now = utc_now()
        expires = now + timedelta(minutes=max(1, ttl_minutes))
        with self._lock:
            with self._connect() as con:
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
        with self._lock:
            with self._connect() as con:
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
        with self._lock:
            with self._connect() as con:
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
        with self._lock:
            with self._connect() as con:
                cur = con.execute(
                    "UPDATE password_reset_tokens SET used_at = ? WHERE token = ? AND used_at IS NULL",
                    (now_utc_iso(), token),
                )
                if cur.rowcount != 1:
                    return None
        return user

    def list_admin_emails(self):
        with self._connect() as con:
            rows = con.execute(
                "SELECT email FROM users WHERE role = 'admin' AND active = 1 AND email IS NOT NULL AND email <> ''"
            ).fetchall()
            return [r["email"] for r in rows if isinstance(r["email"], str) and r["email"].strip()]

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

    def list_station_user_contacts(self, station_uuid: str):
        station_uuid = station_uuid.strip()
        contacts = {}
        with self._connect() as con:
            admin_rows = con.execute(
                "SELECT username,email FROM users WHERE role='admin' AND active=1 AND email IS NOT NULL AND email <> ''"
            ).fetchall()
            for r in admin_rows:
                contacts[r["email"]] = {"username": r["username"], "email": r["email"], "role": "admin"}

            policy = self.get_policy(station_uuid)
            if policy == "account":
                rows = con.execute(
                    "SELECT username,email FROM users WHERE role='user' AND active=1 AND email IS NOT NULL AND email <> ''"
                ).fetchall()
                for r in rows:
                    contacts[r["email"]] = {"username": r["username"], "email": r["email"], "role": "user"}
            elif policy == "restricted":
                rows = con.execute(
                    """
                    SELECT u.username,u.email
                    FROM users u
                    JOIN user_instruments ui ON ui.username = u.username
                    WHERE ui.instrument_uuid = ? AND u.active=1 AND u.email IS NOT NULL AND u.email <> ''
                    """,
                    (station_uuid,),
                ).fetchall()
                for r in rows:
                    contacts[r["email"]] = {"username": r["username"], "email": r["email"], "role": "user"}
        return list(contacts.values())

    def upsert_anomaly(self, station_uuid: str, anomaly_type: str, message: str, severity: str = "warning"):
        now = now_utc_iso()
        with self._lock:
            with self._connect() as con:
                row = con.execute(
                    """
                    SELECT id, status
                    FROM anomalies
                    WHERE station_uuid = ? AND anomaly_type = ? AND status = 'open'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (station_uuid, anomaly_type),
                ).fetchone()
                if row:
                    con.execute(
                        "UPDATE anomalies SET message = ?, updated_at = ? WHERE id = ?",
                        (message, now, row["id"]),
                    )
                    return row["id"], False
                cur = con.execute(
                    """
                    INSERT INTO anomalies(station_uuid, anomaly_type, message, severity, status, created_at, updated_at, resolved_at)
                    VALUES(?,?,?,?, 'open',?,?,NULL)
                    """,
                    (station_uuid, anomaly_type, message, severity, now, now),
                )
                return cur.lastrowid, True

    def resolve_anomaly(self, station_uuid: str, anomaly_type: str, message: str = "resolved"):
        now = now_utc_iso()
        with self._lock:
            with self._connect() as con:
                open_rows = con.execute(
                    "SELECT id FROM anomalies WHERE station_uuid = ? AND anomaly_type = ? AND status = 'open'",
                    (station_uuid, anomaly_type),
                ).fetchall()
                if not open_rows:
                    return False
                con.execute(
                    """
                    UPDATE anomalies
                    SET status='resolved', message=?, updated_at=?, resolved_at=?
                    WHERE station_uuid = ? AND anomaly_type = ? AND status = 'open'
                    """,
                    (message, now, now, station_uuid, anomaly_type),
                )
                return True

    def list_open_anomalies(self):
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT id,station_uuid,anomaly_type,message,severity,status,created_at,updated_at,resolved_at
                FROM anomalies
                WHERE status = 'open'
                ORDER BY updated_at DESC
                """
            ).fetchall()
            return [dict(r) for r in rows]

    def list_anomalies_for_user(self, user):
        if not user:
            return []
        with self._connect() as con:
            if user.get("role") == "admin":
                rows = con.execute(
                    """
                    SELECT id,station_uuid,anomaly_type,message,severity,status,created_at,updated_at,resolved_at
                    FROM anomalies
                    ORDER BY updated_at DESC
                    LIMIT 1000
                    """
                ).fetchall()
                return [dict(r) for r in rows]
            rows = con.execute(
                """
                SELECT a.id,a.station_uuid,a.anomaly_type,a.message,a.severity,a.status,
                       a.created_at,a.updated_at,a.resolved_at
                FROM anomalies a
                LEFT JOIN instrument_policies p ON p.instrument_uuid = a.station_uuid
                LEFT JOIN user_instruments ui ON ui.instrument_uuid = a.station_uuid AND ui.username = ?
                WHERE COALESCE(p.policy, 'account') <> 'restricted' OR ui.username IS NOT NULL
                ORDER BY a.updated_at DESC
                LIMIT 1000
                """,
                (user["username"],),
            ).fetchall()
            return [dict(r) for r in rows]

    def set_anomaly_silence(self, station_uuid: str, anomaly_type: str, silenced_by: str, hours: int):
        now = utc_now()
        hours = min(24, max(1, int(hours)))
        until = now + timedelta(hours=hours)
        with self._lock:
            with self._connect() as con:
                con.execute(
                    """
                    INSERT INTO anomaly_silence(station_uuid, anomaly_type, silenced_until, silenced_by, created_at)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(station_uuid, anomaly_type)
                    DO UPDATE SET silenced_until=excluded.silenced_until, silenced_by=excluded.silenced_by, created_at=excluded.created_at
                    """,
                    (station_uuid, anomaly_type, until.isoformat().replace("+00:00", "Z"), silenced_by, now_utc_iso()),
                )
        return until

    def get_anomaly_silenced_until(self, station_uuid: str, anomaly_type: str):
        with self._connect() as con:
            row = con.execute(
                "SELECT silenced_until FROM anomaly_silence WHERE station_uuid = ? AND anomaly_type = ?",
                (station_uuid, anomaly_type),
            ).fetchone()
            if row is None:
                return None
            return parse_iso_ts(row["silenced_until"])

    def set_station_logo(self, station_uuid: str, logo_path: str, username: str):
        with self._lock:
            with self._connect() as con:
                con.execute(
                    """
                    INSERT INTO station_logos(station_uuid, logo_path, uploaded_by, uploaded_at)
                    VALUES(?,?,?,?)
                    ON CONFLICT(station_uuid)
                    DO UPDATE SET logo_path=excluded.logo_path, uploaded_by=excluded.uploaded_by, uploaded_at=excluded.uploaded_at
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

        with self._lock:
            with self._connect() as con:
                con.execute("DELETE FROM station_chart_settings WHERE station_uuid = ?", (station_uuid,))
                if normalized:
                    con.executemany(
                        """
                        INSERT INTO station_chart_settings(station_uuid, series_key, y_min, y_max, y_step, updated_at, updated_by)
                        VALUES(?,?,?,?,?,?,?)
                        """,
                        normalized,
                    )
        return True


def collect_instruments(storage_root: str):
    root = Path(storage_root)
    if not root.exists() or not root.is_dir():
        return []
    instruments = []
    for p in root.iterdir():
        if not p.is_dir():
            continue
        if p.name.startswith("_") or p.name.startswith("."):
            continue
        if next(p.rglob("*.csv"), None) is None:
            continue
        instruments.append(p.name)
    return sorted(instruments)


def find_latest_csv_file(storage_root: str, instrument_uuid: str):
    root = Path(storage_root) / instrument_uuid
    if not root.exists() or not root.is_dir():
        return None

    def _descending_dirs(parent: Path):
        dirs = [p for p in parent.iterdir() if p.is_dir() and not p.name.startswith(".")]
        return sorted(dirs, key=lambda p: p.name, reverse=True)

    for year_dir in _descending_dirs(root):
        for month_dir in _descending_dirs(year_dir):
            for day_dir in _descending_dirs(month_dir):
                csv_files = sorted(
                    [p for p in day_dir.iterdir() if p.is_file() and p.suffix.lower() == ".csv"],
                    key=lambda p: p.name,
                    reverse=True,
                )
                if csv_files:
                    return csv_files[0]

    for path in root.rglob("*.csv"):
        return path
    return None


def iter_csv_rows(csv_path: Path):
    # csv.reader with zip() is about twice as fast as csv.DictReader on the hourly files.
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header:
            return
        width = len(header)
        for values in reader:
            if not values:
                continue
            if len(values) < width:
                values = values + [None] * (width - len(values))
            # zip() drops the surplus values of a row longer than the header.
            yield dict(zip(header, values))


def read_latest_station_row(csv_path: Path):
    try:
        last_row = None
        for row in iter_csv_rows(csv_path):
            last_row = row
        return last_row
    except Exception:
        return None


def parse_date_ymd(value: str):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def extract_date_from_name(name: str):
    # UUID_YYYYMMDDZHH00.csv
    m = re.search(r"_(\d{8})Z\d{4}\.csv$", name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


_CSV_FILE_HOUR_RE = re.compile(r"_(\d{4})(\d{2})(\d{2})Z(\d{2})\d{2}\.csv$")


def _csv_file_hour(name: str):
    """Start of the hour covered by UUID_YYYYMMDDZHH00.csv, or None."""
    m = _CSV_FILE_HOUR_RE.search(name)
    if not m:
        return None
    try:
        year, month, day, hour = (int(g) for g in m.groups())
        return datetime(year, month, day, hour, tzinfo=timezone.utc)
    except ValueError:
        return None


def _as_utc_bound(value, end_of_day: bool):
    """Accept a date or a datetime as a time bound."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    moment = datetime.max.time() if end_of_day else datetime.min.time()
    return datetime.combine(value, moment, tzinfo=timezone.utc)


def iter_csv_files_newest_first(storage_root: str, instrument_uuid: str, from_date=None, to_date=None):
    """Yield the station's hourly files, newest first.

    Year, month and day directories outside the requested period are not opened, so
    the cost depends on the period and not on the length of the station's history.
    """
    root = Path(storage_root) / instrument_uuid
    start = _as_utc_bound(from_date, end_of_day=False)
    end = _as_utc_bound(to_date, end_of_day=True)
    start_key = (start.year, start.month, start.day) if start else None
    end_key = (end.year, end.month, end.day) if end else None

    def walk(directory: Path, date_key: tuple):
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name, reverse=True)
        except OSError:
            return
        files = []
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                key = date_key
                # <station>/YYYY/MM/DD: compare as much of the date as the path gives.
                if key is not None and len(key) < 3 and entry.name.isdigit():
                    key = key + (int(entry.name),)
                    if start_key and key < start_key[: len(key)]:
                        continue
                    if end_key and key > end_key[: len(key)]:
                        continue
                else:
                    key = None
                yield from walk(entry, key)
            elif entry.suffix.lower() == ".csv":
                if start or end:
                    hour = _csv_file_hour(entry.name)
                    if hour is None:
                        continue
                    if start and hour + timedelta(hours=1) <= start:
                        continue
                    if end and hour > end:
                        continue
                files.append(entry)
        # Files next to date directories do not follow the layout: treat them as oldest.
        yield from files

    if root.is_dir():
        yield from walk(root, ())


def list_csv_files_for_instrument(storage_root: str, instrument_uuid: str, from_date=None, to_date=None):
    return sorted(iter_csv_files_newest_first(storage_root, instrument_uuid, from_date=from_date, to_date=to_date))


def make_zip_for_download(storage_root: str, instrument_uuids, from_date=None, to_date=None):
    tmp = tempfile.NamedTemporaryFile(prefix="collector_download_", suffix=".zip", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()

    count = 0
    try:
        with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for instrument_uuid in instrument_uuids:
                files = list_csv_files_for_instrument(
                    storage_root, instrument_uuid, from_date=from_date, to_date=to_date
                )
                for f in files:
                    rel = f.relative_to(Path(storage_root))
                    zf.write(f, arcname=str(rel))
                    count += 1
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return tmp_path, count




def _to_float(value):
    try:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            out = float(value)
        else:
            s = str(value).strip()
            if not s:
                return None
            out = float(s)
        # NaN/inf readings are not plottable and break axis and JSON handling.
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _is_missing_sensor_value(value):
    if value is None:
        return True
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("", "nan", "none", "null", "n/a", "na", "-"):
            return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return False


def _extract_lat_lon_from_row(row: dict):
    # Direct latitude/longitude columns
    key_pairs = [
        ("latitude", "longitude"),
        ("lat", "lon"),
        ("lat", "lng"),
    ]
    for k_lat, k_lon in key_pairs:
        lat = _to_float(row.get(k_lat))
        lon = _to_float(row.get(k_lon))
        if lat is not None and lon is not None:
            return lat, lon

    # Nested JSON in 'position' column
    pos_raw = row.get("position")
    if isinstance(pos_raw, str) and pos_raw.strip().startswith("{"):
        try:
            pos = json.loads(pos_raw)
            lat = _to_float(pos.get("latitude"))
            lon = _to_float(pos.get("longitude"))
            if lat is not None and lon is not None:
                return lat, lon
        except Exception:
            pass

    return None, None


DEFAULT_FIELD_UNITS = {
    "TempIn": "C",
    "TempOut": "C",
    "HumIn": "%",
    "HumOut": "%",
    "Barometer": "hPa",
    "BarTrend": "hPa/h",
    "WindSpeed": "m/s",
    "WindSpeed10Min": "m/s",
    "WindDir": "deg",
    "RainRate": "mm/h",
    "RainStorm": "mm",
    "RainDay": "mm",
    "RainMonth": "mm",
    "RainYear": "mm",
    "ETDay": "mm",
    "ETMonth": "mm",
    "ETYear": "mm",
    "SolarRad": "W/m2",
    "BatteryVolts": "V",
    "temp": "C",
    "heat_index": "C",
    "dew_point": "C",
    "wet_bulb": "C",
    "hum": "%",
    "bar": "hPa",
    "pm_1": "ug/m3",
    "pm_2p5": "ug/m3",
    "pm_10": "ug/m3",
    "pm_2p5_1_hour": "ug/m3",
    "pm_2p5_3_hour": "ug/m3",
    "pm_2p5_24_hour": "ug/m3",
    "pm_2p5_nowcast": "ug/m3",
    "pm_10_1_hour": "ug/m3",
    "pm_10_3_hour": "ug/m3",
    "pm_10_24_hour": "ug/m3",
    "pm_10_nowcast": "ug/m3",
    "aqi_val": "AQI",
    "aqi_1_hour_val": "AQI",
    "aqi_nowcast_val": "AQI",
}


def get_field_units(cfg: dict):
    out = dict(DEFAULT_FIELD_UNITS)
    path_map = cfg.get("signalk_path_map", {})
    for key, entry in (path_map.items() if isinstance(path_map, dict) else []):
        key_str = str(key)
        if key_str in DEFAULT_FIELD_UNITS:
            continue
        if isinstance(entry, dict):
            meta = entry.get("meta")
            if isinstance(meta, dict):
                units = meta.get("units")
                if isinstance(units, str) and units.strip():
                    out[key_str] = units.strip()
    return out


def compose_external_url(base_url: str, path: str, query=None) -> str:
    root = str(base_url or "").strip().rstrip("/")
    suffix = "/" + str(path or "").lstrip("/")
    out = f"{root}{suffix}" if root else suffix
    if query:
        return f"{out}?{urlencode(query)}"
    return out


def json_for_script(value) -> str:
    """Serialize JSON for embedding inside an HTML <script> element."""
    return (
        json.dumps(value, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def sanitize_signalk_meta(meta):
    if not isinstance(meta, dict):
        return None
    cleaned = {}
    for key, value in meta.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        cleaned[str(key)] = value
    return cleaned or None


def request_preference_cookie(request_obj, param_name: str, cookie_name: str, normalizer, default: str):
    raw = request_obj.args.get(param_name)
    if raw is not None and str(raw).strip():
        return normalizer(raw)
    cookie_val = request_obj.cookies.get(cookie_name, "")
    if cookie_val:
        return normalizer(cookie_val)
    return default


def parse_iso_ts(value: str):
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def safe_filename(name: str):
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "").strip())
    return base[:180] or "file"


def is_valid_email(value: str) -> bool:
    return len(value) <= 254 and re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+", value) is not None


def validate_password_strength(password: str):
    if not isinstance(password, str) or not password:
        return False, "Password is required"
    if len(password) < 12:
        return False, "Password must be at least 12 characters long"
    checks = [
        (r"[A-Z]", "one uppercase letter"),
        (r"[a-z]", "one lowercase letter"),
        (r"[0-9]", "one digit"),
        (r"[^A-Za-z0-9]", "one special character"),
    ]
    missing = [label for pattern, label in checks if re.search(pattern, password) is None]
    if missing:
        return False, "Password must include " + ", ".join(missing)
    return True, ""


def shift_months(dt: datetime, months: int):
    y = dt.year + ((dt.month - 1 + months) // 12)
    m = ((dt.month - 1 + months) % 12) + 1
    d = min(dt.day, [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1])
    return dt.replace(year=y, month=m, day=d)


TREND_INTERVALS = [
    ("1m", "last minute"),
    ("10m", "10 minutes"),
    ("hour", "hour"),
    ("3h", "3 hours"),
    ("6h", "6 hours"),
    ("12h", "12 hours"),
    ("24h", "24 hours"),
    ("72h", "72 hours"),
    ("week", "one week"),
]

PUBLIC_TREND_WINDOWS = [
    ("1m", "last minute"),
    ("10m", "10 minutes"),
    ("hour", "hour"),
    ("3h", "3 hours"),
    ("6h", "6 hours"),
    ("12h", "12 hours"),
    ("24h", "24 hours"),
    ("72h", "72 hours"),
    ("week", "one week"),
]


def normalize_interval(value: str):
    raw = (value or "").strip().lower()
    aliases = {
        "1m": "1m",
        "last_minute": "1m",
        "10m": "10m",
        "10minutes": "10m",
        "hour": "hour",
        "1h": "hour",
        "3h": "3h",
        "6h": "6h",
        "12h": "12h",
        "24h": "24h",
        "day": "24h",
        "72h": "72h",
        "week": "week",
        "1w": "week",
        "month": "month",
        "year": "year",
        "custom": "custom",
    }
    return aliases.get(raw, "hour")


def normalize_public_window(value: str):
    normalized = normalize_interval(value)
    allowed = {k for k, _ in PUBLIC_TREND_WINDOWS}
    return normalized if normalized in allowed else "hour"


def interval_start(anchor: datetime, interval: str):
    interval = normalize_interval(interval)
    if interval == "1m":
        return anchor - timedelta(minutes=1)
    if interval == "10m":
        return anchor - timedelta(minutes=10)
    if interval == "hour":
        return anchor - timedelta(hours=1)
    if interval == "3h":
        return anchor - timedelta(hours=3)
    if interval == "6h":
        return anchor - timedelta(hours=6)
    if interval == "12h":
        return anchor - timedelta(hours=12)
    if interval == "24h":
        return anchor - timedelta(days=1)
    if interval == "72h":
        return anchor - timedelta(hours=72)
    if interval == "week":
        return anchor - timedelta(weeks=1)
    if interval == "month":
        return shift_months(anchor, -1)
    if interval == "year":
        return shift_months(anchor, -12)
    return anchor - timedelta(days=1)


def shift_anchor(anchor: datetime, interval: str, steps: int):
    interval = normalize_interval(interval)
    if interval == "1m":
        return anchor + timedelta(minutes=steps)
    if interval == "10m":
        return anchor + timedelta(minutes=10 * steps)
    if interval == "hour":
        return anchor + timedelta(hours=steps)
    if interval == "3h":
        return anchor + timedelta(hours=3 * steps)
    if interval == "6h":
        return anchor + timedelta(hours=6 * steps)
    if interval == "12h":
        return anchor + timedelta(hours=12 * steps)
    if interval == "24h":
        return anchor + timedelta(days=steps)
    if interval == "72h":
        return anchor + timedelta(hours=72 * steps)
    if interval == "week":
        return anchor + timedelta(weeks=steps)
    if interval == "month":
        return shift_months(anchor, steps)
    if interval == "year":
        return shift_months(anchor, 12 * steps)
    return anchor + timedelta(days=steps)


def get_station_preview(storage_root: str, instrument_uuid: str):
    latest_csv = find_latest_csv_file(storage_root, instrument_uuid)
    if latest_csv is None:
        return {"latitude": None, "longitude": None, "last_timestamp": None, "rows": 0, "name": instrument_uuid}

    latest_row = read_latest_station_row(latest_csv)
    if not latest_row:
        return {"latitude": None, "longitude": None, "last_timestamp": None, "rows": 0, "name": instrument_uuid}

    lat, lon = _extract_lat_lon_from_row(latest_row)
    station_name = instrument_uuid
    maybe_name = latest_row.get("name")
    if isinstance(maybe_name, str) and maybe_name.strip():
        station_name = maybe_name.strip()

    return {
        "latitude": lat,
        "longitude": lon,
        "last_timestamp": latest_row.get("timestamp"),
        "rows": 0,
        "name": station_name,
    }


def load_station_rows(storage_root: str, instrument_uuid: str, from_date=None, to_date=None, limit=400):
    """Rows in time order. from_date/to_date are dates or datetimes selecting the hourly files."""
    limited = limit is not None and limit > 0
    chunks = []
    total = 0
    # Newest files first, so a limited read neither lists nor parses the whole history.
    for csv_path in iter_csv_files_newest_first(storage_root, instrument_uuid, from_date=from_date, to_date=to_date):
        try:
            chunk = list(iter_csv_rows(csv_path))
        except Exception:
            continue
        chunks.append(chunk)
        total += len(chunk)
        if limited and total >= limit:
            break

    out = [row for chunk in reversed(chunks) for row in chunk]
    if limited and len(out) > limit:
        out = out[-limit:]
    return out


def extract_numeric_series(rows, excluded=None):
    excluded = set(excluded or [])
    numeric_keys = set()
    for row in rows:
        for k, v in row.items():
            if k in excluded:
                continue
            if _to_float(v) is not None:
                numeric_keys.add(k)
    return sorted(numeric_keys)


STATION_BROWSER_MAX_CHART_POINTS = 3000

DEFAULT_CHART_COLORS = [
    "#0b57d0",
    "#1f9d55",
    "#d95f02",
    "#7b1fa2",
    "#c2185b",
    "#00838f",
    "#5d4037",
    "#455a64",
]


def normalize_chart_color(value, fallback="#0b57d0"):
    raw = str(value or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", raw):
        return raw.lower()
    return fallback


def build_table_column_stats(rows, columns):
    stats = []
    for col in columns:
        samples = []
        for row in rows:
            value = _to_float(row.get(col))
            if value is None:
                continue
            timestamp = str(row.get("timestamp") or "")
            samples.append({"value": value, "timestamp": timestamp})
        values = [sample["value"] for sample in samples]
        if not values:
            continue
        min_sample = min(samples, key=lambda sample: sample["value"])
        max_sample = max(samples, key=lambda sample: sample["value"])
        avg = statistics.fmean(values)
        stddev = statistics.pstdev(values) if len(values) > 1 else 0.0
        stats.append(
            {
                "column": col,
                "min": round(min_sample["value"], 6),
                "min_at": min_sample["timestamp"],
                "max": round(max_sample["value"], 6),
                "max_at": max_sample["timestamp"],
                "avg": round(avg, 6),
                "stddev": round(stddev, 6),
                "count": len(values),
            }
        )
    return stats


PUBLIC_METRIC_SPECS = [
    {"key": "temperature", "label": "Temperature", "aliases": ["TempOut", "temperature", "outside_temp", "temp"], "unit": "C"},
    {"key": "humidity", "label": "Humidity", "aliases": ["HumOut", "humidity", "hum"], "unit": "%"},
    {"key": "pressure", "label": "Pressure", "aliases": ["Barometer", "pressure", "bar"], "unit": "hPa"},
    {"key": "wind_speed", "label": "Wind Speed", "aliases": ["WindSpeed", "wind_speed"], "unit": "m/s"},
    {"key": "wind_direction", "label": "Wind Direction", "aliases": ["WindDir", "wind_dir"], "unit": "deg"},
    {"key": "rain_rate", "label": "Rain Rate", "aliases": ["RainRate", "rain_rate"], "unit": "mm/h"},
    {"key": "aqi_current", "label": "Current AQI", "aliases": ["aqi_val", "AQI", "CurrentAQI", "aqi"], "unit": ""},
    {"key": "aqi_1h", "label": "1 Hour AQI", "aliases": ["aqi_1_hour_val", "AQI1h", "AQI_1h"], "unit": ""},
    {"key": "aqi_nowcast", "label": "NowCast AQI", "aliases": ["aqi_nowcast_val", "NowCastAQI"], "unit": ""},
    {"key": "pm1", "label": "PM1", "aliases": ["pm_1", "PM1"], "unit": "ug/m3"},
    {"key": "pm2_5", "label": "PM2.5", "aliases": ["pm_2p5", "pm2_5", "PM2_5", "PM2.5"], "unit": "ug/m3"},
    {"key": "pm10", "label": "PM10", "aliases": ["pm_10", "PM10"], "unit": "ug/m3"},
]

# Rows searched, newest first, for the current value of a dashboard card.
PUBLIC_CARD_LOOKBACK_ROWS = 10

PUBLIC_SERIES_SPECS = [
    {
        "key": "temperature",
        "label": "Temperature Trend",
        "aliases": ["TempOut", "temperature", "outside_temp", "temp"],
        "unit": "C",
        "axis": {"auto": True},
    },
    {
        "key": "humidity",
        "label": "Humidity Trend",
        "aliases": ["HumOut", "humidity", "hum"],
        "unit": "%",
        "axis": {"min": 0, "max": 100},
    },
    {
        "key": "pressure",
        "label": "Pressure Trend",
        "aliases": ["Barometer", "pressure", "bar"],
        "unit": "hPa",
        "axis": {"auto": True},
    },
    {
        "key": "wind_speed",
        "label": "Wind Speed Trend",
        "aliases": ["WindSpeed", "wind_speed"],
        "unit": "m/s",
        "axis": {"auto": True, "floor_zero": True},
    },
    {
        "key": "wind_direction",
        "label": "Wind Direction Trend",
        "aliases": ["WindDir", "wind_dir"],
        "unit": "deg",
        "axis": {"min": 0, "max": 360},
    },
    {
        "key": "rain_rate",
        "label": "Rain Rate Trend",
        "aliases": ["RainRate", "rain_rate"],
        "unit": "mm/h",
        "axis": {"auto": True, "floor_zero": True},
    },
    {
        "key": "aqi_trend",
        "label": "Air Quality Index",
        "aliases": ["aqi_val", "AQI", "CurrentAQI", "aqi"],
        "unit": "",
        "axis": {"min": 0, "max": 300},
    },
]

PUBLIC_EXTRA_CHART_SPECS = [
    {
        "key": "particulate_matter",
        "label": "Particulate Matter",
        "unit": "ug/m3",
        "axis": {"auto": True, "floor_zero": True},
    },
]


def _first_numeric_for_aliases(row: dict, aliases):
    for alias in aliases:
        if alias in row:
            v = _to_float(row.get(alias))
            if v is not None:
                return v
    lowered = {str(k).lower(): k for k in row.keys()}
    for alias in aliases:
        key = lowered.get(str(alias).lower())
        if key is None:
            continue
        v = _to_float(row.get(key))
        if v is not None:
            return v
    return None


def _coerce_axis_value(value):
    if value in ("", None):
        return None
    return _to_float(value)


def _nice_axis_step(raw_step: float):
    if raw_step <= 0:
        return 1.0
    exponent = math.floor(math.log10(raw_step))
    fraction = raw_step / (10 ** exponent)
    if fraction <= 1:
        nice_fraction = 1
    elif fraction <= 2:
        nice_fraction = 2
    elif fraction <= 5:
        nice_fraction = 5
    else:
        nice_fraction = 10
    return nice_fraction * (10 ** exponent)


def _calc_axis_settings(values, axis_spec=None):
    axis_spec = axis_spec or {}
    explicit_min = _coerce_axis_value(axis_spec.get("min")) if "min" in axis_spec else None
    explicit_max = _coerce_axis_value(axis_spec.get("max")) if "max" in axis_spec else None
    explicit_step = _coerce_axis_value(axis_spec.get("step")) if "step" in axis_spec else None
    auto = bool(axis_spec.get("auto"))

    if not values and explicit_min is None and explicit_max is None:
        return None, None, explicit_step

    if values:
        lo = min(values)
        hi = max(values)
    else:
        lo = explicit_min if explicit_min is not None else 0.0
        hi = explicit_max if explicit_max is not None else lo + 1.0

    if lo == hi:
        pad = max(1.0, abs(lo) * 0.15)
        lo -= pad
        hi += pad
    elif auto or explicit_min is None or explicit_max is None:
        pad = (hi - lo) * 0.15
        lo -= pad
        hi += pad

    if axis_spec.get("floor_zero"):
        lo = max(0.0, lo)

    if explicit_step is not None and explicit_step > 0:
        step = explicit_step
    else:
        span = max(hi - lo, 1e-9)
        target_ticks = max(3, int(axis_spec.get("ticks") or 6))
        step = _nice_axis_step(span / target_ticks)

    if explicit_min is not None:
        y_min = explicit_min
    else:
        y_min = math.floor(lo / step) * step
        if axis_spec.get("floor_zero"):
            y_min = max(0.0, y_min)

    if explicit_max is not None:
        y_max = explicit_max
    else:
        y_max = math.ceil(hi / step) * step

    if y_min == y_max:
        y_max = y_min + step

    return round(y_min, 6), round(y_max, 6), round(step, 6)


def get_chart_setting_catalog():
    out = []
    for spec in PUBLIC_SERIES_SPECS + PUBLIC_EXTRA_CHART_SPECS:
        out.append(
            {
                "key": spec["key"],
                "label": spec["label"],
                "unit": spec.get("unit", ""),
                "aliases": list(spec.get("aliases") or []),
                "axis": dict(spec.get("axis") or {}),
            }
        )
    return out


def resolve_station_chart_specs(access_store: AccessStore, instrument_uuid: str):
    overrides = access_store.get_station_chart_settings(instrument_uuid) if access_store else {}
    resolved = []
    for spec in get_chart_setting_catalog():
        merged_axis = dict(spec.get("axis") or {})
        override = overrides.get(spec["key"]) or {}
        if override.get("y_min") is not None:
            merged_axis["min"] = float(override["y_min"])
        if override.get("y_max") is not None:
            merged_axis["max"] = float(override["y_max"])
        if override.get("y_step") is not None:
            merged_axis["step"] = float(override["y_step"])
        resolved.append(
            {
                "key": spec["key"],
                "label": spec["label"],
                "unit": spec.get("unit", ""),
                "aliases": list(spec.get("aliases") or []),
                "axis": merged_axis,
                "saved": {
                    "y_min": override.get("y_min"),
                    "y_max": override.get("y_max"),
                    "y_step": override.get("y_step"),
                    "updated_at": override.get("updated_at"),
                    "updated_by": override.get("updated_by"),
                },
            }
        )
    return resolved


def normalize_station_chart_settings_map(raw_settings):
    valid_keys = {spec["key"] for spec in get_chart_setting_catalog()}
    out = {}
    for series_key, raw in (raw_settings or {}).items():
        key = str(series_key or "").strip()
        if not key or key not in valid_keys:
            continue
        item = raw or {}
        y_min = _coerce_axis_value(item.get("y_min"))
        y_max = _coerce_axis_value(item.get("y_max"))
        y_step = _coerce_axis_value(item.get("y_step"))
        if y_step is not None and y_step <= 0:
            raise ValueError(f"Invalid y_step for {key}")
        if y_min is not None and y_max is not None and y_max <= y_min:
            raise ValueError(f"y_max must be greater than y_min for {key}")
        if y_min is None and y_max is None and y_step is None:
            continue
        out[key] = {
            "y_min": y_min,
            "y_max": y_max,
            "y_step": y_step,
        }
    return out


def export_station_chart_settings_payload(access_store: AccessStore, station_uuid: str):
    saved = access_store.get_station_chart_settings(station_uuid) if access_store else {}
    series = {}
    for spec in get_chart_setting_catalog():
        item = saved.get(spec["key"]) or {}
        series[spec["key"]] = {
            "label": spec["label"],
            "unit": spec.get("unit", ""),
            "y_min": item.get("y_min"),
            "y_max": item.get("y_max"),
            "y_step": item.get("y_step"),
            "updated_at": item.get("updated_at"),
            "updated_by": item.get("updated_by"),
        }
    return {
        "station_uuid": station_uuid,
        "exported_at": now_utc_iso(),
        "series": series,
    }


def parse_station_chart_settings_payload(payload: dict):
    if not isinstance(payload, dict):
        raise ValueError("Invalid JSON payload")
    raw_series = payload.get("series")
    if not isinstance(raw_series, dict):
        raise ValueError("Missing 'series' object")
    normalized_input = {}
    for series_key, raw in raw_series.items():
        if not isinstance(raw, dict):
            continue
        normalized_input[series_key] = {
            "y_min": raw.get("y_min"),
            "y_max": raw.get("y_max"),
            "y_step": raw.get("y_step"),
        }
    return normalize_station_chart_settings_map(normalized_input)


def _influx_range_start_for_window(window: str):
    window = normalize_public_window(window)
    if window == "1m":
        return "-10m"
    if window == "10m":
        return "-2h"
    if window == "hour":
        return "-12h"
    if window == "3h":
        return "-24h"
    if window == "6h":
        return "-48h"
    if window == "12h":
        return "-72h"
    if window == "24h":
        return "-7d"
    if window == "72h":
        return "-14d"
    if window == "week":
        return "-21d"
    return "-14d"


def _query_influx_station_rows(cfg: dict, instrument_uuid: str, window: str, start: datetime = None):
    if not cfg or not cfg.get("enable_influx"):
        return []
    influx_client = runtime.get("influx_client")
    if influx_client is None:
        return []

    # Query from the start of the displayed window when it is known; the relative
    # ranges are much wider, to cover stations whose latest data is old.
    if start is not None:
        range_start = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        range_start = _influx_range_start_for_window(window)
    query = f"""
from(bucket: {json.dumps(cfg["influxdb_bucket"])})
  |> range(start: {range_start})
  |> filter(fn: (r) => r._measurement == {json.dumps(cfg["influx_measurement"])} and r.uuid == {json.dumps(instrument_uuid)})
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
"""
    try:
        tables = influx_client.query_api().query(query=query, org=cfg["influxdb_org"])
    except Exception as e:
        logger.warning("Influx query failed for public dashboard station=%s: %s", instrument_uuid, e)
        return []

    out = []
    for table in tables:
        for record in table.records:
            values = dict(record.values or {})
            ts = values.get("_time")
            if isinstance(ts, datetime):
                ts_iso = ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            else:
                ts_iso = str(ts or "")
            row = {"timestamp": ts_iso, "uuid": instrument_uuid}
            for key, value in values.items():
                if key.startswith("_") or key in ("result", "table"):
                    continue
                row[key] = value
            out.append(row)
    out.sort(key=lambda row: row.get("timestamp", ""))
    return out


def _decimate_timeseries(rows_ts, max_points: int):
    if not max_points or len(rows_ts) <= max_points:
        return rows_ts
    step = max(1, math.ceil(len(rows_ts) / max_points))
    sampled = rows_ts[::step]
    if sampled and sampled[-1] != rows_ts[-1]:
        sampled.append(rows_ts[-1])
    return sampled


def _merge_station_rows(*row_sets):
    merged = {}
    for rows in row_sets:
        for row in rows or []:
            ts = str(row.get("timestamp") or "").strip()
            if not ts:
                continue
            if ts not in merged:
                merged[ts] = dict(row)
            else:
                combined = dict(row)
                combined.update({k: v for k, v in merged[ts].items() if v is not None and v != ""})
                merged[ts] = combined
    return [merged[k] for k in sorted(merged.keys())]


def _aqi_status(value):
    v = _to_float(value)
    if v is None:
        return None
    if v <= 50:
        return {"level": "good", "label": "Good conditions", "color": "#198754"}
    if v <= 100:
        return {"level": "warning", "label": "Warning conditions", "color": "#ffc107"}
    return {"level": "bad", "label": "Bad conditions", "color": "#dc3545"}


def _build_series_stats(points, label: str = ""):
    clean = []
    for point in points or []:
        if not isinstance(point, dict):
            continue
        y = _to_float(point.get("y"))
        x = str(point.get("x") or "").strip()
        if y is None or not x:
            continue
        clean.append({"x": x, "y": float(y)})
    if not clean:
        return None
    min_point = min(clean, key=lambda item: item["y"])
    max_point = max(clean, key=lambda item: item["y"])
    current_point = clean[-1]
    return {
        "label": label,
        "current": round(current_point["y"], 3),
        "current_at": current_point["x"],
        "min": round(min_point["y"], 3),
        "min_at": min_point["x"],
        "max": round(max_point["y"], 3),
        "max_at": max_point["x"],
    }


def build_public_station_snapshot(
    storage_root: str,
    instrument_uuid: str,
    window: str = "hour",
    max_points: int = 240,
    cfg: dict = None,
    access_store: AccessStore = None,
):
    window = normalize_public_window(window)
    cfg = cfg or runtime.get("config") or {}
    access_store = access_store or runtime.get("access_store")
    preview = get_station_preview(storage_root, instrument_uuid)
    preview_ts = parse_iso_ts(preview.get("last_timestamp") or "")
    latest_hint = preview_ts or utc_now()
    storage_window_start = interval_start(latest_hint, window)
    # Only the hourly files overlapping the window are read.
    storage_rows = load_station_rows(
        storage_root,
        instrument_uuid,
        from_date=storage_window_start,
        to_date=latest_hint,
        limit=None,
    )
    influx_rows = _query_influx_station_rows(
        cfg, instrument_uuid, window, start=storage_window_start if preview_ts else None
    )
    rows = _merge_station_rows(influx_rows, storage_rows)

    if not rows:
        return {
            "instrument_uuid": instrument_uuid,
            "station_name": preview.get("name") or instrument_uuid,
            "last_timestamp": None,
            "rows": 0,
            "window_start": None,
            "window_end": None,
            "location": {
                "latitude": preview.get("latitude"),
                "longitude": preview.get("longitude"),
            },
            "cards": [],
            "series": [],
            "aqi_status": None,
            "window": window,
        }

    rows_ts_all = []
    for row in rows:
        ts = parse_iso_ts(row.get("timestamp", ""))
        if ts is None:
            continue
        rows_ts_all.append((ts, row))
    rows_ts_all.sort(key=lambda x: x[0])

    latest_row = rows_ts_all[-1][1] if rows_ts_all else rows[-1]
    latest_dt = rows_ts_all[-1][0] if rows_ts_all else parse_iso_ts(latest_row.get("timestamp", "")) or utc_now()
    latest_ts = latest_dt.isoformat().replace("+00:00", "Z")

    win_start = interval_start(latest_dt, window)
    rows_ts = [(ts, row) for ts, row in rows_ts_all if ts >= win_start]
    rows_ts = _decimate_timeseries(rows_ts, max_points)

    # A station can interleave rows from different devices (weather, air quality), so
    # a value missing from the latest row is taken from the few rows before it.
    recent_rows = [row for _, row in reversed(rows_ts_all[-PUBLIC_CARD_LOOKBACK_ROWS:])] or [latest_row]

    def latest_value(aliases):
        for row in recent_rows:
            value = _first_numeric_for_aliases(row, aliases)
            if value is not None:
                return value
        return None

    cards = []
    for spec in PUBLIC_METRIC_SPECS:
        value = latest_value(spec["aliases"])
        cards.append(
            {
                "key": spec["key"],
                "label": spec["label"],
                "value": None if value is None else round(value, 2),
                "unit": spec["unit"],
            }
        )

    aqi_status = _aqi_status(latest_value(["aqi_val", "AQI", "CurrentAQI", "aqi"]))

    chart_specs = {spec["key"]: spec for spec in resolve_station_chart_specs(access_store, instrument_uuid)}

    series = []
    for spec in PUBLIC_SERIES_SPECS:
        resolved_spec = chart_specs.get(spec["key"], spec)
        labels = []
        values = []
        points = []
        for ts, row in rows_ts:
            value = _first_numeric_for_aliases(row, spec["aliases"])
            if value is None:
                continue
            iso_ts = ts.isoformat().replace("+00:00", "Z")
            rounded = round(value, 3)
            labels.append(iso_ts)
            values.append(rounded)
            points.append({"x": iso_ts, "y": rounded})
        if values:
            y_min, y_max, y_step = _calc_axis_settings(values, resolved_spec.get("axis"))
            series.append(
                {
                    "key": spec["key"],
                    "label": resolved_spec["label"],
                    "unit": resolved_spec["unit"],
                    "y_min": y_min,
                    "y_max": y_max,
                    "y_step": y_step,
                    "labels": labels,
                    "values": values,
                    "points": points,
                    "stats": _build_series_stats(points, resolved_spec["label"]),
                }
            )

    wind_dir_spec = chart_specs.get("wind_direction", next((spec for spec in PUBLIC_SERIES_SPECS if spec["key"] == "wind_direction"), {}))
    wind_speed_spec = chart_specs.get("wind_speed", next((spec for spec in PUBLIC_SERIES_SPECS if spec["key"] == "wind_speed"), {}))
    wind_dir_points = []
    wind_dir_values = []
    wind_speed_points = []
    wind_speed_values = []
    for ts, row in rows_ts:
        iso_ts = ts.isoformat().replace("+00:00", "Z")
        wind_dir = _first_numeric_for_aliases(row, ["WindDir", "wind_dir"])
        if wind_dir is not None:
            rounded = round(wind_dir, 3)
            wind_dir_values.append(rounded)
            wind_dir_points.append({"x": iso_ts, "y": rounded})
        wind_speed = _first_numeric_for_aliases(row, ["WindSpeed", "wind_speed"])
        if wind_speed is not None:
            rounded = round(wind_speed, 3)
            wind_speed_values.append(rounded)
            wind_speed_points.append({"x": iso_ts, "y": rounded})
    if wind_dir_points or wind_speed_points:
        dir_min, dir_max, dir_step = _calc_axis_settings(wind_dir_values, wind_dir_spec.get("axis"))
        speed_min, speed_max, speed_step = _calc_axis_settings(wind_speed_values, wind_speed_spec.get("axis"))
        wind_stats = []
        dir_stats = _build_series_stats(wind_dir_points, "Wind Direction")
        speed_stats = _build_series_stats(wind_speed_points, "Wind Speed")
        if dir_stats:
            wind_stats.append(dir_stats)
        if speed_stats:
            wind_stats.append(speed_stats)
        series.append(
            {
                "key": "wind_combined",
                "label": "Wind",
                "unit": "",
                "labels": [ts.isoformat().replace("+00:00", "Z") for ts, _ in rows_ts],
                "datasets": [
                    {
                        "label": "Wind Direction",
                        "points": wind_dir_points,
                        "type": "line",
                        "yAxisID": "wind_direction",
                    },
                    {
                        "label": "Wind Speed",
                        "points": wind_speed_points,
                        "type": "bar",
                        "yAxisID": "wind_speed",
                    },
                ],
                "axes": {
                    "wind_direction": {
                        "position": "left",
                        "unit": wind_dir_spec.get("unit", "deg"),
                        "y_min": dir_min,
                        "y_max": dir_max,
                        "y_step": dir_step,
                    },
                    "wind_speed": {
                        "position": "right",
                        "unit": wind_speed_spec.get("unit", "m/s"),
                        "y_min": speed_min,
                        "y_max": speed_max,
                        "y_step": speed_step,
                    },
                },
                "stats": wind_stats,
            }
        )

    # Particulate matter multi-series trend (only when available).
    pm_series_specs = [
        {"label": "PM1", "aliases": ["pm_1", "PM1"]},
        {"label": "PM2.5", "aliases": ["pm_2p5", "pm2_5", "PM2_5", "PM2.5"]},
        {"label": "PM10", "aliases": ["pm_10", "PM10"]},
    ]
    pm_labels = []
    pm_datasets = []
    pm_all_values = []
    for item in pm_series_specs:
        labels = []
        values = []
        points = []
        for ts, row in rows_ts:
            value = _first_numeric_for_aliases(row, item["aliases"])
            if value is None:
                continue
            iso_ts = ts.isoformat().replace("+00:00", "Z")
            rounded = round(value, 3)
            labels.append(iso_ts)
            values.append(rounded)
            points.append({"x": iso_ts, "y": rounded})
            pm_all_values.append(float(value))
        if values:
            if len(labels) > len(pm_labels):
                pm_labels = labels
            pm_datasets.append({"label": item["label"], "values": values, "points": points})

    if pm_datasets:
        pm_spec = chart_specs.get("particulate_matter", {"axis": {"auto": True, "floor_zero": True}})
        y_min, y_max, y_step = _calc_axis_settings(pm_all_values, pm_spec.get("axis"))
        dataset_stats = []
        for dataset in pm_datasets:
            dataset_stats.append(_build_series_stats(dataset.get("points") or [], dataset.get("label", "")))
        series.append(
            {
                "key": "particulate_matter",
                "label": pm_spec.get("label", "Particulate Matter"),
                "unit": pm_spec.get("unit", "ug/m3"),
                "y_min": y_min,
                "y_max": y_max,
                "y_step": y_step,
                "labels": pm_labels,
                "datasets": pm_datasets,
                "stats": [item for item in dataset_stats if item],
            }
        )

    return {
        "instrument_uuid": instrument_uuid,
        "station_name": latest_row.get("name") or preview.get("name") or instrument_uuid,
        "last_timestamp": latest_ts,
        "rows": len(rows_ts),
        "window": window,
        "window_start": win_start.isoformat().replace("+00:00", "Z"),
        "window_end": latest_dt.isoformat().replace("+00:00", "Z"),
        "location": {
            "latitude": preview.get("latitude"),
            "longitude": preview.get("longitude"),
        },
        "cards": cards,
        "series": series,
        "aqi_status": aqi_status,
    }


def _normalize_alarm_value(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value).strip().lower()


def _battery_alarms_from_row(row: dict):
    alarms = []
    battery_info = []
    for key, raw in row.items():
        lk = str(key).lower()
        if "battery" not in lk:
            continue

        fval = _to_float(raw)
        sval = _normalize_alarm_value(raw)
        if fval is not None:
            battery_info.append(f"{key}={round(fval, 3)}")
        elif sval:
            battery_info.append(f"{key}={raw}")

        if "volt" in lk and fval is not None and fval < 3.0:
            alarms.append(f"low_battery:{key}")
        elif any(t in lk for t in ("status", "level", "percent", "pct")) and fval is not None and fval <= 20:
            alarms.append(f"low_battery:{key}")
        elif any(t in lk for t in ("status", "state")) and sval in ("low", "critical", "bad", "false", "0"):
            alarms.append(f"low_battery:{key}")
    return alarms, battery_info


def compute_station_usual_update_seconds(rows):
    timestamps = []
    for row in rows:
        ts = parse_iso_ts(row.get("timestamp", ""))
        if ts is not None:
            timestamps.append(ts)
    timestamps.sort()
    if len(timestamps) < 3:
        return None
    deltas = []
    for i in range(1, len(timestamps)):
        delta = (timestamps[i] - timestamps[i - 1]).total_seconds()
        if delta > 0:
            deltas.append(delta)
    if len(deltas) < 2:
        return None
    deltas.sort()
    return int(deltas[len(deltas) // 2])


def evaluate_station_anomalies(station_uuid: str, rows, now_dt: datetime):
    latest_ts = None
    latest_row = None
    for row in rows:
        ts = parse_iso_ts(row.get("timestamp", ""))
        if ts is None:
            continue
        if latest_ts is None or ts > latest_ts:
            latest_ts = ts
            latest_row = row

    alarms = []
    values = {}
    battery_info = []
    missing_fields = []
    expected_keys = set()
    usual_update_seconds = compute_station_usual_update_seconds(rows)
    failure_threshold_seconds = None
    if usual_update_seconds is not None:
        failure_threshold_seconds = max(2 * usual_update_seconds, 60)

    for row in rows:
        for key, raw in row.items():
            if key in ("timestamp", "topic", "uuid", "name"):
                continue
            if isinstance(raw, str) and len(raw) > 180:
                continue
            expected_keys.add(key)

    if latest_ts is None or latest_row is None:
        alarms.append("lost_connectivity:no_data")
    else:
        age_seconds = int((now_dt - latest_ts).total_seconds())
        if failure_threshold_seconds is None:
            failure_threshold_seconds = 15 * 60
        if age_seconds > failure_threshold_seconds:
            alarms.append(f"lost_connectivity:{age_seconds}s(threshold={failure_threshold_seconds}s)")

        batt_alarms, battery_info = _battery_alarms_from_row(latest_row)
        alarms.extend(batt_alarms)

        numeric_count = 0
        for key in expected_keys:
            raw = latest_row.get(key)
            if _is_missing_sensor_value(raw):
                missing_fields.append(key)
                continue
            values[key] = raw
            if _to_float(raw) is not None:
                numeric_count += 1
        if numeric_count < 2:
            alarms.append("sensor_failure:too_few_numeric_values")
        if missing_fields:
            short = ",".join(missing_fields[:8])
            suffix = "" if len(missing_fields) <= 8 else ",..."
            alarms.append(f"sensor_failure:missing_values[{len(missing_fields)}]={short}{suffix}")

    return {
        "latest_ts": latest_ts,
        "values": values,
        "battery_info": battery_info,
        "missing_fields": missing_fields,
        "expected_keys": expected_keys,
        "alarms": alarms,
        "usual_update_seconds": usual_update_seconds,
        "failure_threshold_seconds": failure_threshold_seconds,
    }


def build_admin_network_dashboard(storage_root: str):
    now = utc_now()
    stations = []
    value_keys = set()

    for instrument_uuid in collect_instruments(storage_root):
        preview = get_station_preview(storage_root, instrument_uuid)
        rows = load_station_rows(storage_root, instrument_uuid, limit=500)
        summary = evaluate_station_anomalies(instrument_uuid, rows, now)
        latest_ts = summary["latest_ts"]
        value_keys.update(summary["expected_keys"])

        stations.append(
            {
                "uuid": instrument_uuid,
                "name": preview.get("name") or instrument_uuid,
                "lastTimestamp": latest_ts.isoformat().replace("+00:00", "Z") if latest_ts else None,
                "ageSeconds": (int((now - latest_ts).total_seconds()) if latest_ts else None),
                "usualUpdateSeconds": summary["usual_update_seconds"],
                "failureThresholdSeconds": summary["failure_threshold_seconds"],
                "status": "ALARM" if summary["alarms"] else "OK",
                "alarms": summary["alarms"],
                "batteryInfo": ", ".join(summary["battery_info"]) if summary["battery_info"] else "",
                "missingFields": summary["missing_fields"],
                "missingCount": len(summary["missing_fields"]),
                "values": summary["values"],
            }
        )

    stations.sort(key=lambda s: (0 if s["status"] == "ALARM" else 1, s["uuid"]))
    value_columns = sorted(value_keys)
    return {
        "updatedAt": now.isoformat().replace("+00:00", "Z"),
        "valueColumns": value_columns,
        "stations": stations,
    }


def anomaly_base_type(anomaly_code: str):
    return str(anomaly_code or "").split(":", 1)[0]


def run_watchdog_loop(cfg: dict, access_store: AccessStore, stop_event: threading.Event):
    storage_root = cfg.get("storage_root")
    if not storage_root:
        logger.info("Watchdog disabled: storage is not configured")
        return

    interval = max(10, int(cfg.get("watchdog_interval_sec", 60)))
    logger.info("Watchdog started (interval=%ss)", interval)

    while not stop_event.is_set():
        try:
            _run_watchdog_scan(cfg, access_store, storage_root)
        except Exception:
            logger.exception("Watchdog scan failed; retrying in %ss", interval)
        stop_event.wait(interval)


def _run_watchdog_scan(cfg: dict, access_store: AccessStore, storage_root: str):
    now = utc_now()
    for instrument_uuid in collect_instruments(storage_root):
        try:
            rows = load_station_rows(storage_root, instrument_uuid, limit=500)
            summary = evaluate_station_anomalies(instrument_uuid, rows, now)

            active_types = set()
            for alarm in summary["alarms"]:
                anomaly_type = anomaly_base_type(alarm)
                active_types.add(anomaly_type)
                anomaly_id, is_new = access_store.upsert_anomaly(
                    instrument_uuid,
                    anomaly_type,
                    alarm,
                    severity="critical" if anomaly_type in ("lost_connectivity", "sensor_failure") else "warning",
                )
                silenced_until = access_store.get_anomaly_silenced_until(instrument_uuid, anomaly_type)
                if is_new and (silenced_until is None or silenced_until <= now):
                    contacts = access_store.list_station_user_contacts(instrument_uuid)
                    for c in contacts:
                        token = access_store.create_login_token(c["username"], ttl_minutes=60)
                        fast_link = compose_external_url(cfg["base_url"], "fast-login", {"token": token})
                        body = (
                            f"Station: {instrument_uuid}\n"
                            f"Anomaly: {alarm}\n"
                            f"Detected at: {now_utc_iso()}\n\n"
                            f"Open anomalies and optionally silence for up to 24h:\n"
                            f"{compose_external_url(cfg['base_url'], 'anomalies')}\n\n"
                            f"Fast login link (expires in 60 minutes):\n{fast_link}\n"
                        )
                        send_email(
                            cfg,
                            [c["email"]],
                            f"[Sensor Network Alarm] {instrument_uuid} - {anomaly_type}",
                            body,
                        )

            # Resolve anomalies that are no longer active.
            for open_anomaly in access_store.list_open_anomalies():
                if open_anomaly["station_uuid"] != instrument_uuid:
                    continue
                if open_anomaly["anomaly_type"] not in active_types:
                    access_store.resolve_anomaly(instrument_uuid, open_anomaly["anomaly_type"], "resolved")
        except Exception:
            # One unreadable station must not stop the others from being checked.
            logger.exception("Watchdog check failed for station=%s", instrument_uuid)


# ----------------------------
# Station data page
# ----------------------------
STATION_BROWSER_INTERVALS = frozenset({code for code, _ in TREND_INTERVALS} | {"custom"})
STATION_BROWSER_PAGE_SIZES = ("50", "100", "250", "1000")
STATION_BROWSER_MAX_CUSTOM_DAYS = 31

STATION_BROWSER_TEMPLATE = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Station {{ station_name }} ({{ instrument_uuid }})</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
  <style>
    .uuid { font-family: var(--bs-font-monospace); font-size: .8rem; }
    .param-list { max-height: 24rem; overflow-y: auto; }
    .param-row { display: flex; align-items: center; gap: .5rem; padding: .2rem .5rem; border-bottom: 1px solid var(--bs-border-color-translucent); }
    .param-row.plotted { background: var(--bs-primary-bg-subtle); }
    .param-name { flex: 1; min-width: 0; overflow-wrap: anywhere; font-size: .875rem; }
    .chart-box { position: relative; height: 24rem; }
    .series-row { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem; padding: .25rem 0; }
    .series-row input[type=color] { width: 2.2rem; padding: .1rem; }
    .data-wrap { max-height: 30rem; overflow: auto; }
    #dataTable { font-size: .78rem; white-space: nowrap; }
    #dataTable thead th { position: sticky; top: 0; background: var(--bs-light); z-index: 1; }
    .column-list { max-height: 14rem; overflow-y: auto; columns: 14rem; }
    .stats-table { font-size: .85rem; }
  </style>
  <style id="hiddenColumnStyle"></style>
</head>
<body class="bg-light">
<div class="container-fluid py-3" style="max-width: 1500px;">

  <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-3">
    <div class="d-flex align-items-center gap-3">
      {% if app_logo_url %}<img src="{{ app_logo_url }}" alt="App logo" style="max-height:48px;">{% endif %}
      <div>
        <nav class="small"><a href="{{ url_for('index') }}">Home</a> / Station data</nav>
        <h1 class="h3 mb-0">{{ station_name }}</h1>
        <div class="uuid text-muted">{{ instrument_uuid }}</div>
      </div>
      {% if station_logo_url %}<img src="{{ station_logo_url }}" alt="Station logo" style="max-height:48px;">{% endif %}
    </div>
    <div class="d-flex flex-wrap gap-2">
      <a class="btn btn-outline-secondary btn-sm" href="{{ url_for('public_station', instrument_uuid=instrument_uuid) }}">Live dashboard</a>
      {% if can_control %}
        <a class="btn btn-outline-secondary btn-sm" href="{{ url_for('station_chart_settings', instrument_uuid=instrument_uuid) }}">Dashboard axis settings</a>
      {% endif %}
    </div>
  </div>

  {# ------------------------------ Time range ------------------------------ #}
  <div class="card shadow-sm mb-3" id="intervalPanel"><div class="card-body">
    <div class="d-flex flex-wrap align-items-end gap-3">
      <form method="get" id="intervalForm">
        <label class="form-label mb-1" for="intervalSelect">Time window</label>
        <select class="form-select form-select-sm" name="interval" id="intervalSelect">
          {% for code, label in interval_options %}
            <option value="{{ code }}" {% if code==interval %}selected{% endif %}>{{ label }}</option>
          {% endfor %}
          {% if interval == 'custom' %}<option value="custom" selected disabled>custom dates</option>{% endif %}
        </select>
      </form>
      <div class="btn-group btn-group-sm" role="group" aria-label="Move in time">
        <a class="btn btn-outline-primary" href="{{ url_for('browse_station', instrument_uuid=instrument_uuid, **prev_args) }}">&larr; Earlier</a>
        <a class="btn btn-outline-primary" href="{{ url_for('browse_station', instrument_uuid=instrument_uuid, **next_args) }}">Later &rarr;</a>
        <a class="btn btn-outline-primary {% if is_latest and interval != 'custom' %}disabled{% endif %}" href="{{ url_for('browse_station', instrument_uuid=instrument_uuid, interval=(interval if interval != 'custom' else 'hour')) }}">Latest</a>
      </div>
      <form method="get" class="d-flex flex-wrap align-items-end gap-2 ms-lg-auto">
        <div>
          <label class="form-label mb-1" for="fromDate">From (UTC)</label>
          <input class="form-control form-control-sm" type="date" id="fromDate" name="from_date" value="{{ range_args.get('from_date', '') }}" required>
        </div>
        <div>
          <label class="form-label mb-1" for="toDate">To (UTC)</label>
          <input class="form-control form-control-sm" type="date" id="toDate" name="to_date" value="{{ range_args.get('to_date', '') }}" required>
        </div>
        <button class="btn btn-outline-primary btn-sm" type="submit" title="At most {{ max_custom_days }} days">Show these dates</button>
      </form>
    </div>
    <p class="mb-0 mt-3" id="intervalSummary">
      <b>{{ total_rows }}</b> rows from <b>{{ win_start }}</b> to <b>{{ win_end }}</b> (UTC).
      {% if chart_step > 1 %}<span class="text-muted">The chart shows one sample every {{ chart_step }}; the table and the statistics use all {{ total_rows }} rows.</span>{% endif %}
    </p>
  </div></div>

  <script id="stationBrowseState" type="application/json">{{ station_browse_state_json|safe }}</script>

  {% if not total_rows %}
    <div class="alert alert-info">No data in this time range. Use <b>Earlier</b>, <b>Later</b> or <b>Latest</b> to move, or pick other dates.</div>
  {% else %}

  {# ------------------------------ Chart ------------------------------ #}
  <div class="card shadow-sm mb-3"><div class="card-body">
    <h2 class="h5">Chart</h2>
    {% if not numeric_cols %}
      <p class="text-muted mb-0">No numeric parameters in this time range.</p>
    {% else %}
    <div class="row g-3">
      <div class="col-lg-4 col-xl-3">
        <label class="form-label mb-1" for="paramSearch">Parameters</label>
        <input class="form-control form-control-sm mb-2" id="paramSearch" type="search" placeholder="Search {{ numeric_cols|length }} parameters">
        <div class="param-list border rounded bg-white" id="paramList"></div>
        <div class="form-text"><b>L</b> / <b>R</b> plot a parameter on the left or right axis; press again to remove it. Parameters with the same unit share an axis.</div>
      </div>
      <div class="col-lg-8 col-xl-9">
        <div class="chart-box"><canvas id="chart"></canvas></div>
        <div id="chartEmpty" class="text-muted d-none">Choose a parameter with <b>L</b> or <b>R</b> to plot it.</div>

        <div class="mt-3" id="seriesList"></div>
        <div class="mt-2" id="axisList"></div>

        <div class="d-flex flex-wrap gap-2 mt-3">
          <button class="btn btn-primary btn-sm" type="button" data-bs-toggle="collapse" data-bs-target="#publicationPanel">Download plot&hellip;</button>
          <a class="btn btn-outline-primary btn-sm" id="plottedCsvLink" href="#">Plotted data (CSV)</a>
          <button class="btn btn-outline-secondary btn-sm" type="button" id="chartClearBtn">Clear chart</button>
          <button class="btn btn-outline-secondary btn-sm ms-auto" type="button" id="chartExportBtn">Save chart setup</button>
          <label class="btn btn-outline-secondary btn-sm mb-0" for="chartImportFile">Load chart setup</label>
          <input id="chartImportFile" type="file" accept="application/json,.json" hidden>
        </div>

        <div class="collapse mt-3" id="publicationPanel">
          <div class="border rounded p-3 bg-white">
            <h3 class="h6">Publication-quality plot</h3>
            <div class="row g-2">
              <div class="col-sm-6 col-xl-3">
                <label class="form-label mb-1" for="pubSize">Figure size</label>
                <select class="form-select form-select-sm" id="pubSize">
                  <option value="90x60">Single column, 90 &times; 60 mm</option>
                  <option value="140x85">1.5 columns, 140 &times; 85 mm</option>
                  <option value="190x100" selected>Double column, 190 &times; 100 mm</option>
                  <option value="254x143">Slide 16:9, 254 &times; 143 mm</option>
                </select>
              </div>
              <div class="col-sm-6 col-xl-2">
                <label class="form-label mb-1" for="pubFont">Font</label>
                <select class="form-select form-select-sm" id="pubFont">
                  <option value="Helvetica, Arial, sans-serif">Sans-serif (Helvetica)</option>
                  <option value="'Times New Roman', Times, serif">Serif (Times)</option>
                </select>
              </div>
              <div class="col-sm-6 col-xl-2">
                <label class="form-label mb-1" for="pubFontSize">Text size</label>
                <select class="form-select form-select-sm" id="pubFontSize">
                  <option value="7">7 pt</option>
                  <option value="8" selected>8 pt</option>
                  <option value="9">9 pt</option>
                  <option value="10">10 pt</option>
                  <option value="12">12 pt</option>
                </select>
              </div>
              <div class="col-sm-6 col-xl-2">
                <label class="form-label mb-1" for="pubDpi">PNG resolution</label>
                <select class="form-select form-select-sm" id="pubDpi">
                  <option value="300">300 dpi</option>
                  <option value="600" selected>600 dpi</option>
                  <option value="1200">1200 dpi</option>
                </select>
              </div>
              <div class="col-sm-12 col-xl-3">
                <label class="form-label mb-1" for="pubTitle">Title (optional)</label>
                <input class="form-control form-control-sm" id="pubTitle" value="{{ station_name }}">
              </div>
            </div>
            <div class="d-flex flex-wrap gap-3 mt-2">
              <div class="form-check"><input class="form-check-input" type="checkbox" id="pubLegend" checked><label class="form-check-label" for="pubLegend">Legend</label></div>
              <div class="form-check"><input class="form-check-input" type="checkbox" id="pubGrid" checked><label class="form-check-label" for="pubGrid">Grid</label></div>
              <div class="form-check"><input class="form-check-input" type="checkbox" id="pubMono"><label class="form-check-label" for="pubMono">Black and white (line styles instead of colours)</label></div>
            </div>
            <div class="d-flex flex-wrap gap-2 mt-3">
              <button class="btn btn-primary btn-sm" type="button" id="pubSvgBtn">Download SVG (vector)</button>
              <button class="btn btn-primary btn-sm" type="button" id="pubPngBtn">Download PNG</button>
            </div>
            <div class="form-text">SVG is a vector file that stays sharp at any size and can be edited or converted to PDF/EPS; PNG is saved at the chosen resolution with its physical size. Times are UTC.</div>
          </div>
        </div>
      </div>
    </div>
    {% endif %}
  </div></div>

  {# ------------------------------ Data ------------------------------ #}
  <div class="card shadow-sm mb-3" id="data"><div class="card-body">
    <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-2">
      <h2 class="h5 mb-0">Data</h2>
      <div class="d-flex flex-wrap gap-2">
        <a class="btn btn-primary btn-sm" id="rangeCsvLink" href="{{ url_for('export_station_csv', instrument_uuid=instrument_uuid, **range_args) }}" data-base="{{ url_for('export_station_csv', instrument_uuid=instrument_uuid, **range_args) }}">Download this range (CSV)</a>
        <button class="btn btn-outline-primary btn-sm" type="button" data-bs-toggle="collapse" data-bs-target="#zipPanel">Raw files (ZIP)&hellip;</button>
      </div>
    </div>

    <div class="collapse mb-3" id="zipPanel">
      <form method="post" action="{{ url_for('download') }}" class="border rounded p-3 bg-white d-flex flex-wrap align-items-end gap-2">
        <input type="hidden" name="instrument" value="{{ instrument_uuid }}"/>
        <div>
          <label class="form-label mb-1" for="zipFrom">From (UTC)</label>
          <input class="form-control form-control-sm" type="date" id="zipFrom" name="from_date" value="{{ win_start[:10] }}">
        </div>
        <div>
          <label class="form-label mb-1" for="zipTo">To (UTC)</label>
          <input class="form-control form-control-sm" type="date" id="zipTo" name="to_date" value="{{ win_end[:10] }}">
        </div>
        <button class="btn btn-primary btn-sm" type="submit">Download ZIP</button>
        <div class="form-text w-100">The original hourly CSV files of the station, for any period. Leave both dates empty for the whole archive.</div>
      </form>
    </div>

    <div class="d-flex flex-wrap align-items-end gap-3 mb-2">
      <form method="get" action="{{ url_for('browse_station', instrument_uuid=instrument_uuid) }}#data" id="tablePrefsForm" class="d-flex flex-wrap align-items-end gap-2">
        {% for key, value in range_args.items() %}<input type="hidden" name="{{ key }}" value="{{ value }}">{% endfor %}
        <div>
          <label class="form-label mb-1" for="tablePageSize">Rows per page</label>
          <select class="form-select form-select-sm" id="tablePageSize" name="page_size">
            {% for size in page_sizes %}<option value="{{ size }}" {% if size|int == page_size %}selected{% endif %}>{{ size }}</option>{% endfor %}
          </select>
        </div>
        <div>
          <label class="form-label mb-1" for="tableOrder">Order</label>
          <select class="form-select form-select-sm" id="tableOrder" name="order">
            <option value="asc" {% if order == 'asc' %}selected{% endif %}>Oldest first</option>
            <option value="desc" {% if order == 'desc' %}selected{% endif %}>Newest first</option>
          </select>
        </div>
        <noscript><button class="btn btn-outline-primary btn-sm" type="submit">Apply</button></noscript>
      </form>
      <button class="btn btn-outline-secondary btn-sm" type="button" data-bs-toggle="collapse" data-bs-target="#columnPanel">Columns <span class="badge text-bg-secondary" id="columnCount"></span></button>
      <nav class="ms-auto" aria-label="Table pages">
        {% set page_args = dict(range_args, page_size=page_size, order=order) %}
        <ul class="pagination pagination-sm mb-0">
          <li class="page-item {% if page <= 1 %}disabled{% endif %}"><a class="page-link" href="{{ url_for('browse_station', instrument_uuid=instrument_uuid, page=1, **page_args) }}#data">First</a></li>
          <li class="page-item {% if page <= 1 %}disabled{% endif %}"><a class="page-link" href="{{ url_for('browse_station', instrument_uuid=instrument_uuid, page=page-1, **page_args) }}#data">Previous</a></li>
          <li class="page-item disabled"><span class="page-link">Page {{ page }} of {{ page_count }}</span></li>
          <li class="page-item {% if page >= page_count %}disabled{% endif %}"><a class="page-link" href="{{ url_for('browse_station', instrument_uuid=instrument_uuid, page=page+1, **page_args) }}#data">Next</a></li>
          <li class="page-item {% if page >= page_count %}disabled{% endif %}"><a class="page-link" href="{{ url_for('browse_station', instrument_uuid=instrument_uuid, page=page_count, **page_args) }}#data">Last</a></li>
        </ul>
      </nav>
    </div>

    <div class="collapse mb-2" id="columnPanel">
      <div class="border rounded p-2 bg-white">
        <div class="d-flex gap-2 mb-2">
          <button class="btn btn-outline-secondary btn-sm" type="button" id="columnsAll">Show all</button>
          <button class="btn btn-outline-secondary btn-sm" type="button" id="columnsPlotted">Only timestamp and plotted</button>
          <span class="form-text">The CSV of this range contains the columns shown here.</span>
        </div>
        <div class="column-list">
          {% for c in all_table_columns %}
            <div class="form-check">
              <input class="form-check-input column-toggle" type="checkbox" id="col-{{ loop.index0 }}" data-index="{{ loop.index0 }}" value="{{ c }}" checked>
              <label class="form-check-label small" for="col-{{ loop.index0 }}">{{ c }}</label>
            </div>
          {% endfor %}
        </div>
      </div>
    </div>

    <div class="small text-muted mb-1">Rows {{ start_idx + 1 }}&ndash;{{ end_idx }} of {{ total_rows }}</div>
    <div class="data-wrap border rounded bg-white" id="tableWrap">
      <table class="table table-sm table-striped table-hover mb-0" id="dataTable">
        <thead><tr>
          {% for c in all_table_columns %}
            <th class="c{{ loop.index0 }}">{{ c }}{% if c in numeric_cols and units_map.get(c) %} <span class="text-muted fw-normal">[{{ units_map.get(c) }}]</span>{% endif %}</th>
          {% endfor %}
        </tr></thead>
        <tbody>
          {% for r in table_rows %}
            <tr>{% for c in all_table_columns %}<td class="c{{ loop.index0 }}">{{ r.get(c) if r.get(c) is not none else '' }}</td>{% endfor %}</tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div></div>

  {# ------------------------------ Statistics ------------------------------ #}
  <div class="card shadow-sm mb-3" id="statisticsSection"><div class="card-body">
    <h2 class="h5">Statistics of this range</h2>
    {% if table_column_stats %}
      <div class="table-responsive">
      <table class="table table-sm table-hover stats-table mb-0">
        <thead><tr><th>Parameter</th><th>Unit</th><th class="text-end">Samples</th><th class="text-end">Minimum</th><th>at</th><th class="text-end">Maximum</th><th>at</th><th class="text-end">Mean</th><th class="text-end">Std. deviation</th></tr></thead>
        <tbody>
          {% for stat in table_column_stats %}
            <tr>
              <td>{{ stat.column }}</td>
              <td>{{ units_map.get(stat.column, '') }}</td>
              <td class="text-end">{{ stat.count }}</td>
              <td class="text-end">{{ stat.min }}</td><td class="text-muted">{{ stat.min_at or '-' }}</td>
              <td class="text-end">{{ stat.max }}</td><td class="text-muted">{{ stat.max_at or '-' }}</td>
              <td class="text-end">{{ stat.avg }}</td>
              <td class="text-end">{{ stat.stddev }}</td>
            </tr>
          {% endfor %}
        </tbody>
      </table>
      </div>
    {% else %}
      <p class="text-muted mb-0">No numeric parameters in this time range.</p>
    {% endif %}
  </div></div>
  {% endif %}

  {% if user %}
  <div class="card shadow-sm mb-3"><div class="card-body">
    <h2 class="h6">Station logo</h2>
    <form method="post" action="{{ url_for('upload_station_logo', instrument_uuid=instrument_uuid) }}" enctype="multipart/form-data" class="d-flex flex-wrap gap-2">
      <input class="form-control form-control-sm w-auto" type="file" name="logo" accept="image/*" required>
      <button class="btn btn-outline-secondary btn-sm" type="submit">Upload logo</button>
    </form>
  </div></div>
  {% endif %}
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
(function () {
  const stationUuid = {{ instrument_uuid | tojson }};
  const defaultColors = {{ default_chart_colors | tojson }};
  const winStart = Date.parse({{ win_start | tojson }});
  const winEnd = Date.parse({{ win_end | tojson }});
  let state = {};
  try {
    state = JSON.parse(document.getElementById('stationBrowseState').textContent || '{}');
  } catch (_) {
    state = {};
  }
  const labels = Array.isArray(state.chart_labels) ? state.chart_labels : [];
  const times = labels.map((label) => Date.parse(label));
  const seriesValues = state.numeric_series_aligned || {};
  const units = state.units_map || {};
  const numericCols = Array.isArray(state.numeric_cols) ? state.numeric_cols : [];
  const tableColumns = Array.isArray(state.all_table_columns) ? state.all_table_columns : [];
  const el = (id) => document.getElementById(id);

  function setCookie(name, value) {
    document.cookie = `${name}=${encodeURIComponent(value)}; path=/; max-age=31536000; samesite=lax`;
  }
  function readStored(key) {
    try {
      const raw = window.localStorage.getItem(key);
      return raw ? JSON.parse(raw) : null;
    } catch (_) {
      return null;
    }
  }
  function writeStored(key, value) {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch (_) {
      // The page works without localStorage; the choice is just not remembered.
    }
  }
  function download(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function compactTime(ms) {
    return new Date(ms).toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
  }
  const fileStem = `${stationUuid.replace(/[^A-Za-z0-9._-]+/g, '_')}_${compactTime(winStart)}_${compactTime(winEnd)}`;

  // ---------------------------------------------------------------- time range and table controls
  const intervalSelect = el('intervalSelect');
  if (intervalSelect) {
    intervalSelect.addEventListener('change', () => {
      setCookie('station_trend_window', intervalSelect.value);
      intervalSelect.form.requestSubmit();
    });
  }
  [['tablePageSize', 'station_page_size'], ['tableOrder', 'station_row_order']].forEach(([id, cookie]) => {
    const select = el(id);
    if (!select) return;
    select.addEventListener('change', () => {
      setCookie(cookie, select.value);
      select.form.requestSubmit();
    });
  });

  // ---------------------------------------------------------------- chart setup (remembered per station)
  const chartStorageKey = `station_chart_config:${stationUuid}`;
  const chartConfig = { left: [], right: [] };

  function unitOf(field) {
    return units[field] || '';
  }
  function fieldLabel(field) {
    return unitOf(field) ? `${field} [${unitOf(field)}]` : field;
  }
  function numberOrNull(value) {
    if (value === null || value === undefined || value === '') return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }
  function plottedItems() {
    return [...chartConfig.left, ...chartConfig.right];
  }
  function nextColor() {
    const used = new Set(plottedItems().map((item) => item.color));
    return defaultColors.find((color) => !used.has(color)) || defaultColors[plottedItems().length % defaultColors.length];
  }
  function newItem(field) {
    // Axis ranges start automatic, so they follow the data when the time range changes.
    return { field, type: 'line', color: nextColor(), min: null, max: null, step: null };
  }
  function loadChartConfig(source) {
    chartConfig.left = [];
    chartConfig.right = [];
    const seen = new Set();
    ['left', 'right'].forEach((side) => {
      const items = source && Array.isArray(source[side]) ? source[side] : [];
      items.forEach((raw) => {
        if (!raw || typeof raw !== 'object') return;
        const field = String(raw.field || '');
        if (!numericCols.includes(field) || seen.has(field)) return;
        seen.add(field);
        const step = numberOrNull(raw.step);
        chartConfig[side].push({
          field,
          type: raw.type === 'bar' ? 'bar' : 'line',
          color: /^#[0-9a-fA-F]{6}$/.test(String(raw.color || '')) ? String(raw.color).toLowerCase() : nextColor(),
          min: numberOrNull(raw.min),
          max: numberOrNull(raw.max),
          step: step !== null && step > 0 ? step : null
        });
      });
    });
  }
  function saveChartConfig() {
    writeStored(chartStorageKey, chartConfig);
  }

  const stored = readStored(chartStorageKey);
  if (stored && typeof stored === 'object') {
    loadChartConfig(stored);
  } else {
    // First visit: start from the usual pair, or from the first parameter.
    const pick = (names) => names.find((name) => numericCols.includes(name));
    const first = pick(['TempOut', 'temp', 'temperature']) || numericCols[0];
    const second = pick(['HumOut', 'hum', 'humidity']);
    if (first) chartConfig.left.push(newItem(first));
    if (second && second !== first) chartConfig.right.push(newItem(second));
  }

  // ---------------------------------------------------------------- axes: one per unit and side
  function niceStep(rough) {
    if (!(rough > 0)) return 1;
    const exponent = Math.floor(Math.log10(rough));
    const fraction = rough / (10 ** exponent);
    const nice = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
    return nice * (10 ** exponent);
  }
  function axisGroups() {
    const groups = [];
    ['left', 'right'].forEach((side) => {
      chartConfig[side].forEach((item) => {
        const unit = unitOf(item.field);
        // Parameters without a unit cannot be assumed comparable: each gets its own axis.
        let group = unit ? groups.find((g) => g.side === side && g.unit === unit) : null;
        if (!group) {
          group = { id: `y${groups.length}`, side, unit, items: [] };
          groups.push(group);
        }
        group.items.push(item);
      });
    });
    groups.forEach((group) => {
      const explicit = (key) => {
        const item = group.items.find((it) => it[key] !== null && it[key] !== undefined);
        return item ? item[key] : null;
      };
      let lo = Infinity;
      let hi = -Infinity;
      group.items.forEach((item) => {
        (seriesValues[item.field] || []).forEach((v) => {
          if (v === null || v === undefined) return;
          if (v < lo) lo = v;
          if (v > hi) hi = v;
        });
      });
      if (!Number.isFinite(lo)) { lo = 0; hi = 1; }
      if (lo === hi) { const pad = Math.max(1, Math.abs(lo) * 0.1); lo -= pad; hi += pad; }
      const exMin = explicit('min');
      const exMax = explicit('max');
      const exStep = explicit('step');
      const spanLo = exMin !== null ? exMin : lo;
      const spanHi = exMax !== null ? exMax : hi;
      // A step that would draw more than 40 ticks is ignored.
      const stepFits = exStep !== null && exStep > 0 && (spanHi - spanLo) / exStep <= 40;
      const step = stepFits ? exStep : niceStep(Math.max(spanHi - spanLo, 1e-9) / 5);
      let min = exMin !== null ? exMin : Math.floor(lo / step) * step;
      let max = exMax !== null ? exMax : Math.ceil(hi / step) * step;
      if (!(max > min)) max = min + step;
      group.min = Number(min.toPrecision(12));
      group.max = Number(max.toPrecision(12));
      group.step = step;
      group.explicit = { min: exMin, max: exMax, step: exStep };
      group.label = group.items.length === 1 ? fieldLabel(group.items[0].field) : group.unit;
    });
    return groups;
  }
  function decimalsFor(step) {
    return Math.min(6, Math.max(0, -Math.floor(Math.log10(step) + 1e-9)));
  }

  // ---------------------------------------------------------------- time axis
  const TIME_STEPS = [1, 5, 10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800]
    .map((seconds) => seconds * 1000);
  function timeTicks(min, max, maxCount) {
    const span = max - min;
    if (!(span > 0)) return { step: 0, values: [] };
    const step = TIME_STEPS.find((s) => span / s <= Math.max(2, maxCount)) || TIME_STEPS[TIME_STEPS.length - 1];
    const values = [];
    for (let t = Math.ceil(min / step) * step; t <= max; t += step) values.push(t);
    return { step, values };
  }
  function timeLabel(ms, step, previousMs) {
    const iso = new Date(ms).toISOString();
    const day = iso.slice(0, 10);
    if (step >= 86400000) return [day];
    const time = step < 60000 ? iso.slice(11, 19) : iso.slice(11, 16);
    const dayChanged = previousMs === null || new Date(previousMs).toISOString().slice(0, 10) !== day;
    return dayChanged ? [time, day] : [time];
  }

  // ---------------------------------------------------------------- Chart.js model (screen and PNG)
  const DASHES = [[], [6, 3], [2, 2], [8, 3, 2, 3], [1, 3], [10, 4]];
  function buildChartConfig(pub) {
    const groups = axisGroups();
    const mono = Boolean(pub && pub.mono);
    const datasets = [];
    groups.forEach((group) => {
      group.items.forEach((item) => {
        const color = mono ? '#000000' : item.color;
        const values = seriesValues[item.field] || [];
        datasets.push({
          type: item.type === 'bar' ? 'bar' : 'line',
          label: fieldLabel(item.field),
          data: times.map((t, i) => ({ x: t, y: values[i] === undefined ? null : values[i] })),
          borderColor: color,
          backgroundColor: item.type === 'bar' ? (mono ? '#00000055' : `${item.color}99`) : color,
          borderWidth: item.type === 'bar' ? 0 : (pub ? 1 : 1.5),
          borderDash: mono && item.type !== 'bar' ? DASHES[datasets.length % DASHES.length] : [],
          pointRadius: 0,
          pointHoverRadius: pub ? 0 : 3,
          pointStyle: item.type === 'bar' ? 'rect' : 'line',
          // Bars are drawn first, so lines stay readable on top of them.
          order: item.type === 'bar' ? 1 : 0,
          spanGaps: true,
          tension: 0,
          yAxisID: group.id
        });
      });
    });
    const ink = pub ? '#000000' : undefined;
    // Set on every text element: a chart-level font does not reach them all.
    const font = pub ? { family: pub.fontFamily, size: pub.fontPx } : undefined;
    const legendEntries = datasets.map((dataset) => ({
      label: dataset.label,
      bar: dataset.type === 'bar',
      color: dataset.borderColor,
      fill: dataset.backgroundColor,
      dash: dataset.borderDash
    }));
    const header = pub ? publicationHeader(pub, legendEntries) : null;
    const gridColor = pub ? '#d9d9d9' : undefined;
    const showGrid = pub ? pub.grid : true;
    let xStep = 0;
    const scales = {
      x: {
        type: 'linear',
        min: winStart,
        max: winEnd,
        offset: false,
        title: { display: true, text: 'Time (UTC)', color: ink, font },
        grid: { display: showGrid, color: gridColor },
        border: { color: ink },
        afterBuildTicks: (axis) => {
          const ticks = timeTicks(axis.min, axis.max, Math.floor(axis.width / (pub ? pub.fontPx * 7 : 90)));
          xStep = ticks.step;
          axis.ticks = ticks.values.map((value) => ({ value }));
        },
        ticks: {
          color: ink,
          font,
          maxRotation: 0,
          autoSkip: false,
          callback: (value, index, ticks) => timeLabel(value, xStep, index > 0 ? ticks[index - 1].value : null)
        }
      }
    };
    groups.forEach((group, index) => {
      const decimals = decimalsFor(group.step);
      scales[group.id] = {
        type: 'linear',
        position: group.side,
        min: group.min,
        max: group.max,
        title: { display: true, text: group.label, color: ink, font },
        border: { color: ink },
        grid: { display: showGrid, color: gridColor, drawOnChartArea: index === 0 },
        ticks: { color: ink, font, stepSize: group.step, callback: (value) => Number(value).toFixed(decimals) }
      };
    });
    return {
      type: 'line',
      data: { datasets },
      options: {
        responsive: !pub,
        maintainAspectRatio: false,
        animation: false,
        parsing: false,
        normalized: true,
        devicePixelRatio: pub ? pub.dpi / 96 : undefined,
        color: ink,
        layout: { padding: { top: header ? header.height : 0 } },
        interaction: { mode: 'index', intersect: false },
        plugins: {
          // The publication plot draws its own title and legend (see publicationHeader).
          legend: { display: !pub && datasets.length > 1, labels: { usePointStyle: true, pointStyleWidth: 28 } },
          tooltip: {
            enabled: !pub,
            callbacks: {
              title: (items) => (items.length ? new Date(items[0].parsed.x).toISOString().replace('.000Z', 'Z') : '')
            }
          }
        },
        scales
      },
      plugins: pub ? [{
        id: 'publicationFrame',
        beforeDraw: (chart) => {
          const ctx = chart.ctx;
          ctx.save();
          ctx.fillStyle = '#ffffff';
          ctx.fillRect(0, 0, chart.width, chart.height);
          ctx.restore();
        },
        afterDraw: (chart) => header.draw(chart.ctx, chart.chartArea.left, chart.chartArea.right, chart.width)
      }] : []
    };
  }

  // Title and legend of the publication PNG: dashed line samples and bar swatches,
  // laid out like the SVG export.
  function publicationHeader(pub, entries) {
    const fs = pub.fontPx;
    const rowHeight = fs * 1.5;
    const titleHeight = pub.title ? fs * 1.5 : 0;
    const available = pub.widthPx - fs * 8;
    const measure = document.createElement('canvas').getContext('2d');
    measure.font = `${fs}px ${pub.fontFamily}`;
    const rows = [[]];
    if (pub.legend) {
      let cursor = 0;
      entries.forEach((entry) => {
        const width = fs * 2.6 + measure.measureText(entry.label).width + fs;
        if (cursor + width > available && rows[rows.length - 1].length) {
          rows.push([]);
          cursor = 0;
        }
        rows[rows.length - 1].push({ entry, x: cursor, width });
        cursor += width;
      });
    }
    const legendHeight = pub.legend ? rows.length * rowHeight : 0;
    return {
      height: titleHeight + legendHeight + fs * 0.4,
      draw(ctx, left, right, fullWidth) {
        ctx.save();
        ctx.fillStyle = '#000000';
        ctx.textBaseline = 'alphabetic';
        if (pub.title) {
          ctx.font = `bold ${fs}px ${pub.fontFamily}`;
          ctx.textAlign = 'center';
          ctx.fillText(pub.title, fullWidth / 2, fs * 1.1);
        }
        ctx.font = `${fs}px ${pub.fontFamily}`;
        ctx.textAlign = 'left';
        if (pub.legend) {
          rows.forEach((row, rowIndex) => {
            const rowWidth = row.reduce((sum, cell) => sum + cell.width, 0);
            const startX = left + Math.max(0, (right - left - rowWidth) / 2);
            const y = titleHeight + rowIndex * rowHeight + fs;
            row.forEach((cell) => {
              const x = startX + cell.x;
              if (cell.entry.bar) {
                ctx.fillStyle = cell.entry.fill;
                ctx.fillRect(x, y - fs * 0.7, fs * 2, fs * 0.7);
              } else {
                ctx.beginPath();
                ctx.strokeStyle = cell.entry.color;
                ctx.lineWidth = 1.2;
                ctx.setLineDash(cell.entry.dash || []);
                ctx.moveTo(x, y - fs * 0.35);
                ctx.lineTo(x + fs * 2, y - fs * 0.35);
                ctx.stroke();
                ctx.setLineDash([]);
              }
              ctx.fillStyle = '#000000';
              ctx.fillText(cell.entry.label, x + fs * 2.4, y);
            });
          });
        }
        ctx.restore();
      }
    };
  }

  const chartCanvas = el('chart');
  let stationChart = null;
  function renderChart() {
    if (!chartCanvas || !window.Chart) return;
    const hasSeries = plottedItems().length > 0;
    el('chartEmpty').classList.toggle('d-none', hasSeries);
    chartCanvas.parentElement.classList.toggle('d-none', !hasSeries);
    if (stationChart) {
      stationChart.destroy();
      stationChart = null;
    }
    if (hasSeries) stationChart = new Chart(chartCanvas, buildChartConfig(null));
  }

  // ---------------------------------------------------------------- parameter picker
  function sideOf(field) {
    if (chartConfig.left.some((item) => item.field === field)) return 'left';
    if (chartConfig.right.some((item) => item.field === field)) return 'right';
    return null;
  }
  function setSide(field, side) {
    const current = sideOf(field);
    let item = null;
    if (current) {
      item = chartConfig[current].find((it) => it.field === field);
      chartConfig[current] = chartConfig[current].filter((it) => it.field !== field);
    }
    if (side && side !== current) chartConfig[side].push(item || newItem(field));
    refreshChartUi();
  }
  function renderParamList() {
    const list = el('paramList');
    if (!list) return;
    const term = (el('paramSearch').value || '').trim().toLowerCase();
    list.textContent = '';
    const matching = numericCols.filter((field) => !term || fieldLabel(field).toLowerCase().includes(term));
    // Plotted parameters first, so the current selection is always in view.
    matching.sort((a, b) => Number(Boolean(sideOf(b))) - Number(Boolean(sideOf(a))));
    matching.forEach((field) => {
      const side = sideOf(field);
      const row = document.createElement('div');
      row.className = `param-row${side ? ' plotted' : ''}`;
      const name = document.createElement('span');
      name.className = 'param-name';
      name.textContent = fieldLabel(field);
      const group = document.createElement('div');
      group.className = 'btn-group btn-group-sm';
      [['left', 'L', 'left axis'], ['right', 'R', 'right axis']].forEach(([target, text, title]) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = `btn ${side === target ? 'btn-primary' : 'btn-outline-secondary'}`;
        button.textContent = text;
        button.title = side === target ? `Remove ${field} from the chart` : `Plot ${field} on the ${title}`;
        button.setAttribute('aria-pressed', side === target ? 'true' : 'false');
        button.addEventListener('click', () => setSide(field, target));
        group.appendChild(button);
      });
      row.append(name, group);
      list.appendChild(row);
    });
    if (!matching.length) {
      const empty = document.createElement('div');
      empty.className = 'text-muted small p-2';
      empty.textContent = 'No parameter matches the search.';
      list.appendChild(empty);
    }
  }

  function renderSeriesList() {
    const list = el('seriesList');
    if (!list) return;
    list.textContent = '';
    ['left', 'right'].forEach((side) => {
      chartConfig[side].forEach((item) => {
        const row = document.createElement('div');
        row.className = 'series-row border-bottom';
        const color = document.createElement('input');
        color.type = 'color';
        color.className = 'form-control form-control-sm form-control-color';
        color.value = item.color;
        color.title = `Colour of ${item.field}`;
        color.addEventListener('change', () => { item.color = color.value; refreshChartUi(false); });
        const name = document.createElement('span');
        name.className = 'fw-semibold small flex-grow-1';
        name.textContent = fieldLabel(item.field);
        const type = document.createElement('select');
        type.className = 'form-select form-select-sm w-auto';
        type.setAttribute('aria-label', `Chart type of ${item.field}`);
        [['line', 'Line'], ['bar', 'Bars']].forEach(([value, text]) => type.add(new Option(text, value, false, item.type === value)));
        type.addEventListener('change', () => { item.type = type.value; refreshChartUi(false); });
        const axis = document.createElement('select');
        axis.className = 'form-select form-select-sm w-auto';
        axis.setAttribute('aria-label', `Axis of ${item.field}`);
        [['left', 'Left axis'], ['right', 'Right axis']].forEach(([value, text]) => axis.add(new Option(text, value, false, side === value)));
        axis.addEventListener('change', () => setSide(item.field, axis.value));
        const remove = document.createElement('button');
        remove.type = 'button';
        remove.className = 'btn btn-outline-danger btn-sm';
        remove.textContent = 'Remove';
        remove.addEventListener('click', () => setSide(item.field, null));
        row.append(color, name, type, axis, remove);
        list.appendChild(row);
      });
    });
  }

  function renderAxisList() {
    const list = el('axisList');
    if (!list) return;
    list.textContent = '';
    axisGroups().forEach((group) => {
      const row = document.createElement('div');
      row.className = 'series-row';
      const name = document.createElement('span');
      name.className = 'small text-muted flex-grow-1';
      name.textContent = `${group.side === 'left' ? 'Left' : 'Right'} axis · ${group.label || 'no unit'}`;
      row.appendChild(name);
      const decimals = decimalsFor(group.step);
      [['min', 'Min', group.min.toFixed(decimals)], ['max', 'Max', group.max.toFixed(decimals)], ['step', 'Step', String(group.step)]]
        .forEach(([key, text, auto]) => {
          const wrap = document.createElement('div');
          wrap.className = 'input-group input-group-sm w-auto';
          const tag = document.createElement('span');
          tag.className = 'input-group-text';
          tag.textContent = text;
          const input = document.createElement('input');
          input.type = 'number';
          input.step = 'any';
          input.className = 'form-control';
          input.style.width = '6.5rem';
          input.placeholder = `auto (${auto})`;
          input.setAttribute('aria-label', `${text} of the ${name.textContent}`);
          if (group.explicit[key] !== null) input.value = group.explicit[key];
          input.addEventListener('change', () => {
            let value = numberOrNull(input.value);
            if (key === 'step' && value !== null && value <= 0) value = null;
            // The range belongs to the axis, so every series on it carries the same values.
            group.items.forEach((item) => { item[key] = value; });
            refreshChartUi(false);
            renderAxisList();
          });
          wrap.append(tag, input);
          row.appendChild(wrap);
        });
      const auto = document.createElement('button');
      auto.type = 'button';
      auto.className = 'btn btn-outline-secondary btn-sm';
      auto.textContent = 'Auto range';
      auto.addEventListener('click', () => {
        group.items.forEach((item) => { item.min = null; item.max = null; item.step = null; });
        refreshChartUi(false);
        renderAxisList();
      });
      row.appendChild(auto);
      list.appendChild(row);
    });
  }

  function updateCsvLinks() {
    const hidden = hiddenColumns();
    const range = el('rangeCsvLink');
    if (range) {
      const url = new URL(range.dataset.base, window.location.origin);
      if (hidden.size) tableColumns.filter((c) => !hidden.has(c)).forEach((c) => url.searchParams.append('col', c));
      range.href = url.pathname + url.search;
    }
    const plotted = el('plottedCsvLink');
    if (plotted && range) {
      const url = new URL(range.dataset.base, window.location.origin);
      const fields = plottedItems().map((item) => item.field);
      ['timestamp', ...fields].forEach((c) => url.searchParams.append('col', c));
      plotted.href = url.pathname + url.search;
      plotted.classList.toggle('disabled', fields.length === 0);
    }
  }

  function refreshChartUi(rebuildLists = true) {
    saveChartConfig();
    if (rebuildLists) {
      renderParamList();
      renderSeriesList();
    }
    renderAxisList();
    renderChart();
    updateCsvLinks();
  }

  if (el('paramSearch')) el('paramSearch').addEventListener('input', renderParamList);
  if (el('chartClearBtn')) {
    el('chartClearBtn').addEventListener('click', () => {
      chartConfig.left = [];
      chartConfig.right = [];
      refreshChartUi();
    });
  }
  if (el('chartExportBtn')) {
    el('chartExportBtn').addEventListener('click', () => {
      const payload = { instrument_uuid: stationUuid, exported_at: new Date().toISOString(), chart_config: chartConfig };
      download(new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' }), `${fileStem.split('_')[0]}_chart_setup.json`);
    });
  }
  if (el('chartImportFile')) {
    el('chartImportFile').addEventListener('change', async (event) => {
      const input = event.target;
      const file = input.files && input.files[0];
      if (!file) return;
      try {
        const payload = JSON.parse(await file.text());
        if (!payload || typeof payload.chart_config !== 'object') throw new Error('invalid');
        loadChartConfig(payload.chart_config);
        refreshChartUi();
      } catch (_) {
        window.alert('This file is not a chart setup saved from this page.');
      } finally {
        input.value = '';
      }
    });
  }

  // ---------------------------------------------------------------- table columns (remembered per station)
  const columnStorageKey = `station_hidden_columns:${stationUuid}`;
  const columnToggles = Array.from(document.querySelectorAll('.column-toggle'));
  function hiddenColumns() {
    return new Set(columnToggles.filter((toggle) => !toggle.checked).map((toggle) => toggle.value));
  }
  function applyColumns(remember = true) {
    const rules = columnToggles
      .filter((toggle) => !toggle.checked)
      .map((toggle) => `#dataTable .c${toggle.dataset.index}{display:none}`);
    el('hiddenColumnStyle').textContent = rules.join('\n');
    const count = el('columnCount');
    if (count) count.textContent = `${columnToggles.length - rules.length}/${columnToggles.length}`;
    if (remember) writeStored(columnStorageKey, Array.from(hiddenColumns()));
    updateCsvLinks();
  }
  const storedHidden = readStored(columnStorageKey);
  if (Array.isArray(storedHidden)) {
    const hide = new Set(storedHidden);
    columnToggles.forEach((toggle) => { toggle.checked = !hide.has(toggle.value); });
    if (columnToggles.length && columnToggles.every((toggle) => !toggle.checked)) {
      columnToggles.forEach((toggle) => { toggle.checked = true; });
    }
  }
  columnToggles.forEach((toggle) => toggle.addEventListener('change', () => applyColumns()));
  if (el('columnsAll')) {
    el('columnsAll').addEventListener('click', () => {
      columnToggles.forEach((toggle) => { toggle.checked = true; });
      applyColumns();
    });
  }
  if (el('columnsPlotted')) {
    el('columnsPlotted').addEventListener('click', () => {
      const keep = new Set(['timestamp', ...plottedItems().map((item) => item.field)]);
      columnToggles.forEach((toggle) => { toggle.checked = keep.has(toggle.value); });
      applyColumns();
    });
  }

  // ---------------------------------------------------------------- publication export
  function publicationSettings() {
    const [widthMm, heightMm] = el('pubSize').value.split('x').map(Number);
    const fontPt = Number(el('pubFontSize').value);
    return {
      widthMm,
      heightMm,
      widthPx: (widthMm / 25.4) * 96,
      heightPx: (heightMm / 25.4) * 96,
      fontPt,
      fontPx: (fontPt * 96) / 72,
      fontFamily: el('pubFont').value,
      dpi: Number(el('pubDpi').value),
      title: el('pubTitle').value.trim(),
      legend: el('pubLegend').checked,
      grid: el('pubGrid').checked,
      mono: el('pubMono').checked
    };
  }

  // PNG files carry their resolution in a pHYs chunk; the canvas does not write one.
  function crc32(bytes) {
    let crc = -1;
    for (let i = 0; i < bytes.length; i += 1) {
      crc ^= bytes[i];
      for (let k = 0; k < 8; k += 1) crc = (crc >>> 1) ^ (0xEDB88320 & -(crc & 1));
    }
    return (crc ^ -1) >>> 0;
  }
  function withPngResolution(png, dpi) {
    const pixelsPerMetre = Math.round(dpi / 0.0254);
    const chunk = new Uint8Array(21);
    const view = new DataView(chunk.buffer);
    view.setUint32(0, 9);
    chunk.set([0x70, 0x48, 0x59, 0x73], 4);
    view.setUint32(8, pixelsPerMetre);
    view.setUint32(12, pixelsPerMetre);
    chunk[16] = 1;
    view.setUint32(17, crc32(chunk.subarray(4, 17)));
    const headerEnd = 33;
    const out = new Uint8Array(png.length + chunk.length);
    out.set(png.subarray(0, headerEnd), 0);
    out.set(chunk, headerEnd);
    out.set(png.subarray(headerEnd), headerEnd + chunk.length);
    return out;
  }

  function renderPng(pub) {
    return new Promise((resolve, reject) => {
      const canvas = document.createElement('canvas');
      canvas.width = Math.round(pub.widthPx);
      canvas.height = Math.round(pub.heightPx);
      canvas.style.width = `${Math.round(pub.widthPx)}px`;
      canvas.style.height = `${Math.round(pub.heightPx)}px`;
      const holder = document.createElement('div');
      holder.style.cssText = 'position:fixed;left:-100000px;top:0;';
      holder.appendChild(canvas);
      document.body.appendChild(holder);
      // devicePixelRatio = dpi / 96 makes Chart.js draw every line and letter at the print resolution.
      const chart = new Chart(canvas, buildChartConfig(pub));
      canvas.toBlob(async (blob) => {
        try {
          if (!blob) throw new Error('empty');
          resolve(withPngResolution(new Uint8Array(await blob.arrayBuffer()), pub.dpi));
        } catch (error) {
          reject(error);
        } finally {
          chart.destroy();
          holder.remove();
        }
      }, 'image/png');
    });
  }

  async function downloadPng() {
    if (!plottedItems().length) return;
    const pub = publicationSettings();
    try {
      download(new Blob([await renderPng(pub)], { type: 'image/png' }), `${fileStem}_${pub.dpi}dpi.png`);
    } catch (_) {
      window.alert('The image is too large for this browser. Choose a lower resolution or a smaller figure.');
    }
  }

  function buildSvg(pub) {
    const W = pub.widthPx;
    const H = pub.heightPx;
    const fs = pub.fontPx;
    const groups = axisGroups();
    const leftGroups = groups.filter((g) => g.side === 'left');
    const rightGroups = groups.filter((g) => g.side === 'right');
    const esc = (text) => String(text).replace(/[&<>"]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));
    const n = (value) => Number(value.toFixed(2));
    const textWidth = (text) => String(text).length * fs * 0.56;
    const axisWidth = (group) => {
      const decimals = decimalsFor(group.step);
      const widest = Math.max(textWidth(group.min.toFixed(decimals)), textWidth(group.max.toFixed(decimals)));
      return widest + fs * 2.9;
    };
    const stroke = 0.75;
    const out = [];
    const text = (x, y, value, anchor = 'middle', extra = '') =>
      out.push(`<text x="${n(x)}" y="${n(y)}" text-anchor="${anchor}"${extra}>${esc(value)}</text>`);

    const series = [];
    groups.forEach((group) => group.items.forEach((item) => series.push({ group, item })));
    series.forEach((entry, index) => {
      entry.color = pub.mono ? '#000000' : entry.item.color;
      entry.dash = pub.mono && entry.item.type !== 'bar' ? DASHES[index % DASHES.length] : [];
    });

    let top = fs * 0.6;
    if (pub.title) top += fs * 1.5;
    const padLeft = Math.max(fs, leftGroups.reduce((sum, g) => sum + axisWidth(g), 0));
    const padRight = Math.max(fs * 1.5, rightGroups.reduce((sum, g) => sum + axisWidth(g), 0));
    const legendRows = [[]];
    if (pub.legend) {
      let cursor = 0;
      series.forEach((entry) => {
        const width = fs * 2.6 + textWidth(fieldLabel(entry.item.field)) + fs;
        if (cursor + width > W - padLeft - padRight && legendRows[legendRows.length - 1].length) {
          legendRows.push([]);
          cursor = 0;
        }
        legendRows[legendRows.length - 1].push({ entry, x: cursor, width });
        cursor += width;
      });
    }
    const legendHeight = pub.legend ? legendRows.length * fs * 1.5 : 0;
    const x0 = padLeft;
    const x1 = W - padRight;
    const y0 = top + legendHeight + fs * 0.4;
    const y1 = H - fs * 4.4;
    const xs = (t) => x0 + ((t - winStart) / (winEnd - winStart)) * (x1 - x0);

    out.push(`<rect width="${n(W)}" height="${n(H)}" fill="#ffffff"/>`);
    if (pub.title) text(W / 2, fs * 1.3, pub.title, 'middle', ' font-weight="bold"');

    if (pub.legend) {
      legendRows.forEach((row, rowIndex) => {
        const rowWidth = row.reduce((sum, cell) => sum + cell.width, 0);
        const startX = x0 + Math.max(0, (x1 - x0 - rowWidth) / 2);
        const y = top + rowIndex * fs * 1.5 + fs * 0.9;
        row.forEach((cell) => {
          const lx = startX + cell.x;
          if (cell.entry.item.type === 'bar') {
            out.push(`<rect x="${n(lx)}" y="${n(y - fs * 0.7)}" width="${n(fs * 2)}" height="${n(fs * 0.7)}" fill="${cell.entry.color}" fill-opacity="${pub.mono ? 0.35 : 0.6}"/>`);
          } else {
            const dash = cell.entry.dash.length ? ` stroke-dasharray="${cell.entry.dash.join(' ')}"` : '';
            out.push(`<line x1="${n(lx)}" y1="${n(y - fs * 0.35)}" x2="${n(lx + fs * 2)}" y2="${n(y - fs * 0.35)}" stroke="${cell.entry.color}" stroke-width="1.2"${dash}/>`);
          }
          text(lx + fs * 2.4, y, fieldLabel(cell.entry.item.field), 'start');
        });
      });
    }

    const ticksX = timeTicks(winStart, winEnd, Math.floor((x1 - x0) / (fs * 7)));
    const yTicks = (group) => {
      const values = [];
      const first = Math.ceil(group.min / group.step - 1e-9) * group.step;
      for (let v = first; v <= group.max + group.step * 1e-6 && values.length < 60; v += group.step) values.push(v);
      return values;
    };
    const ys = (group, v) => y1 - ((v - group.min) / (group.max - group.min)) * (y1 - y0);

    if (pub.grid) {
      out.push('<g stroke="#d9d9d9" stroke-width="0.5">');
      ticksX.values.forEach((t) => out.push(`<line x1="${n(xs(t))}" y1="${n(y0)}" x2="${n(xs(t))}" y2="${n(y1)}"/>`));
      if (groups.length) {
        yTicks(groups[0]).forEach((v) => out.push(`<line x1="${n(x0)}" y1="${n(ys(groups[0], v))}" x2="${n(x1)}" y2="${n(ys(groups[0], v))}"/>`));
      }
      out.push('</g>');
    }

    out.push(`<clipPath id="plotArea"><rect x="${n(x0)}" y="${n(y0)}" width="${n(x1 - x0)}" height="${n(y1 - y0)}"/></clipPath>`);
    out.push('<g clip-path="url(#plotArea)">');
    const barWidth = Math.max(0.4, ((x1 - x0) / Math.max(times.length, 1)) * 0.8);
    // Bars first, so lines stay readable on top of them.
    const drawOrder = [...series].sort((a, b) => Number(b.item.type === 'bar') - Number(a.item.type === 'bar'));
    drawOrder.forEach((entry) => {
      const values = seriesValues[entry.item.field] || [];
      if (entry.item.type === 'bar') {
        const base = ys(entry.group, Math.min(Math.max(0, entry.group.min), entry.group.max));
        const rects = [];
        times.forEach((t, i) => {
          const v = values[i];
          if (v === null || v === undefined || !Number.isFinite(t)) return;
          const y = ys(entry.group, v);
          rects.push(`<rect x="${n(xs(t) - barWidth / 2)}" y="${n(Math.min(y, base))}" width="${n(barWidth)}" height="${n(Math.abs(base - y))}"/>`);
        });
        // Group opacity: overlapping bars of a dense series do not add up.
        out.push(`<g fill="${entry.color}" opacity="${pub.mono ? 0.35 : 0.6}">${rects.join('')}</g>`);
      } else {
        const points = [];
        times.forEach((t, i) => {
          const v = values[i];
          if (v === null || v === undefined || !Number.isFinite(t)) return;
          points.push(`${points.length ? 'L' : 'M'}${n(xs(t))} ${n(ys(entry.group, v))}`);
        });
        const dash = entry.dash.length ? ` stroke-dasharray="${entry.dash.join(' ')}"` : '';
        if (points.length) out.push(`<path d="${points.join('')}" fill="none" stroke="${entry.color}" stroke-width="1" stroke-linejoin="round"${dash}/>`);
      }
    });
    out.push('</g>');

    out.push(`<rect x="${n(x0)}" y="${n(y0)}" width="${n(x1 - x0)}" height="${n(y1 - y0)}" fill="none" stroke="#000000" stroke-width="${stroke}"/>`);

    let previous = null;
    ticksX.values.forEach((t) => {
      const x = xs(t);
      out.push(`<line x1="${n(x)}" y1="${n(y1)}" x2="${n(x)}" y2="${n(y1 + fs * 0.4)}" stroke="#000000" stroke-width="${stroke}"/>`);
      timeLabel(t, ticksX.step, previous).forEach((line, lineIndex) => text(x, y1 + fs * (1.4 + lineIndex * 1.15), line));
      previous = t;
    });
    text((x0 + x1) / 2, H - fs * 0.6, 'Time (UTC)');

    const drawAxis = (group, x, direction) => {
      const decimals = decimalsFor(group.step);
      out.push(`<line x1="${n(x)}" y1="${n(y0)}" x2="${n(x)}" y2="${n(y1)}" stroke="#000000" stroke-width="${stroke}"/>`);
      yTicks(group).forEach((v) => {
        const y = ys(group, v);
        out.push(`<line x1="${n(x)}" y1="${n(y)}" x2="${n(x + direction * fs * 0.4)}" y2="${n(y)}" stroke="#000000" stroke-width="${stroke}"/>`);
        text(x + direction * fs * 0.6, y + fs * 0.35, v.toFixed(decimals), direction < 0 ? 'end' : 'start');
      });
      const titleX = x + direction * (axisWidth(group) - fs * 0.9);
      const titleY = (y0 + y1) / 2;
      text(titleX, titleY, group.label, 'middle', ` transform="rotate(${direction < 0 ? -90 : 90} ${n(titleX)} ${n(titleY)})"`);
    };
    let offset = 0;
    leftGroups.forEach((group) => { drawAxis(group, x0 - offset, -1); offset += axisWidth(group); });
    offset = 0;
    rightGroups.forEach((group) => { drawAxis(group, x1 + offset, 1); offset += axisWidth(group); });

    return [
      '<?xml version="1.0" encoding="UTF-8"?>',
      `<svg xmlns="http://www.w3.org/2000/svg" width="${pub.widthMm}mm" height="${pub.heightMm}mm" viewBox="0 0 ${n(W)} ${n(H)}" font-family="${esc(pub.fontFamily)}" font-size="${n(fs)}" fill="#000000">`,
      ...out,
      '</svg>'
    ].join('\n');
  }

  if (el('pubPngBtn')) el('pubPngBtn').addEventListener('click', downloadPng);
  if (el('pubSvgBtn')) {
    el('pubSvgBtn').addEventListener('click', () => {
      if (!plottedItems().length) return;
      download(new Blob([buildSvg(publicationSettings())], { type: 'image/svg+xml' }), `${fileStem}.svg`);
    });
  }
  // Exposed for automated checks of the exports.
  window.stationBrowser = { chartConfig, buildSvg, renderPng, publicationSettings, axisGroups };

  applyColumns(false);
  if (chartCanvas) refreshChartUi();
})();
</script>
</body>
</html>
"""


# ----------------------------
# Progressive web app assets
# ----------------------------
COMPRESSIBLE_MIMETYPES = frozenset(
    {"text/html", "application/json", "text/javascript", "application/manifest+json"}
)

PWA_THEME_COLOR = "#0b57d0"
PWA_ICON_SIZES = (180, 192, 512)

# The worker keeps only static assets and the offline page: pages and API responses
# depend on the logged-in user and are always fetched from the network.
PWA_SERVICE_WORKER_JS = """
const CACHE = 'sensor-network-pwa-v1';
const OFFLINE_URL = __OFFLINE_URL__;
const PRECACHE = __PRECACHE__;
const STATIC_HOSTS = ['cdn.jsdelivr.net', 'unpkg.com'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((cache) => Promise.all(PRECACHE.map((url) => cache.add(url).catch(() => null))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);

  if (STATIC_HOSTS.includes(url.hostname) || PRECACHE.includes(url.pathname)) {
    event.respondWith(
      caches.open(CACHE).then(async (cache) => {
        const cached = await cache.match(request);
        const refresh = fetch(request)
          .then((response) => {
            if (response.ok) cache.put(request, response.clone());
            return response;
          })
          .catch(() => cached);
        return cached || refresh;
      })
    );
    return;
  }

  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(() => caches.match(OFFLINE_URL)));
  }
});
"""

PWA_BODY_SNIPPET = """
<div id="pwaOfflineBanner" class="alert alert-warning shadow-sm position-fixed bottom-0 start-50 translate-middle-x mb-3 py-2 px-3 d-none" role="status" style="z-index:2000">You are offline. Data shown may be out of date.</div>
<button id="pwaInstallButton" type="button" class="btn btn-primary shadow position-fixed bottom-0 end-0 m-3 d-none" style="z-index:2000">Install app</button>
<script>
(function () {
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register(__SW_URL__, { scope: __SCOPE__ }).catch(function () {});
    });
  }
  var banner = document.getElementById('pwaOfflineBanner');
  function showConnectivity() { banner.classList.toggle('d-none', navigator.onLine); }
  window.addEventListener('online', showConnectivity);
  window.addEventListener('offline', showConnectivity);
  showConnectivity();
  var button = document.getElementById('pwaInstallButton');
  var installPrompt = null;
  window.addEventListener('beforeinstallprompt', function (event) {
    event.preventDefault();
    installPrompt = event;
    button.classList.remove('d-none');
  });
  button.addEventListener('click', function () {
    if (!installPrompt) return;
    installPrompt.prompt();
    installPrompt = null;
    button.classList.add('d-none');
  });
  window.addEventListener('appinstalled', function () { button.classList.add('d-none'); });
})();
</script>
"""


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


@functools.lru_cache(maxsize=None)
def build_pwa_icon_png(size: int) -> bytes:
    """Application icon: a sensor dot with two signal rings on the theme colour."""
    bg = bytes(int(PWA_THEME_COLOR[i:i + 2], 16) for i in (1, 3, 5))
    fg = b"\xff\xff\xff"
    half = size / 2
    # Everything stays inside the central 80% so the icon also works as a maskable one.
    bands = ((0.0, 0.09), (0.17, 0.23), (0.31, 0.37))
    rows = []
    for y in range(size):
        dy = (y + 0.5 - half) / size
        row = bytearray(b"\x00")
        for x in range(size):
            dist = math.hypot((x + 0.5 - half) / size, dy)
            row += fg if any(lo <= dist < hi for lo, hi in bands) else bg
        rows.append(bytes(row))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + _png_chunk(b"IEND", b"")
    )


def create_web_app(cfg: dict, access_store: AccessStore):
    app = Flask(__name__)
    session_secret = cfg["web_session_secret"]
    if not session_secret or session_secret in PLACEHOLDER_SESSION_SECRETS:
        # Kept in the auth DB so that all Gunicorn workers and restarts share it.
        session_secret = access_store.get_or_create_session_secret()
        logger.warning("webSessionSecret is not set or is a sample value; using a generated secret stored in the auth DB")
    app.secret_key = session_secret
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
    runtime["access_store"] = access_store

    @app.before_request
    def reject_cross_site_writes():
        # CSRF defence for the form endpoints: a browser-supplied Origin/Referer
        # naming another host is refused. Ports and schemes are ignored because
        # reverse proxies commonly rewrite them.
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return None
        source = request.headers.get("Origin") or request.headers.get("Referer") or ""
        try:
            source_host = (urlsplit(source).hostname or "").lower()
        except ValueError:
            source_host = ""
        if not source_host:
            return None
        allowed = set()
        for candidate in (
            request.host,
            request.headers.get("X-Forwarded-Host", "").split(",")[0].strip(),
            urlsplit(cfg.get("base_url") or "").netloc,
        ):
            try:
                host = (urlsplit("//" + candidate).hostname or "").lower() if candidate else ""
            except ValueError:
                host = ""
            if host:
                allowed.add(host)
        if source_host not in allowed:
            logger.warning("Rejected cross-site %s %s from origin host %s", request.method, request.path, source_host)
            abort(403, "Cross-site request rejected")
        return None

    @app.after_request
    def add_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.after_request
    def compress_response(response):
        # after_request hooks run in reverse order: this one sees the final body.
        if (
            response.direct_passthrough
            or response.status_code != 200
            or response.headers.get("Content-Encoding")
            or response.mimetype not in COMPRESSIBLE_MIMETYPES
            or "gzip" not in request.headers.get("Accept-Encoding", "").lower()
        ):
            return response
        data = response.get_data()
        if len(data) < 1024:
            return response
        response.set_data(gzip.compress(data, compresslevel=5))
        response.headers["Content-Encoding"] = "gzip"
        response.vary.add("Accept-Encoding")
        return response

    @app.after_request
    def add_pwa_markup(response):
        # Every page is an inline template, so the PWA tags are added here once.
        if response.mimetype != "text/html" or response.direct_passthrough:
            return response
        html = response.get_data(as_text=True)
        if "</head>" not in html or "</body>" not in html:
            return response
        app_name = escape(cfg["web_app_name"])
        head = (
            f'<link rel="manifest" href="{url_for("pwa_manifest")}">'
            f'<meta name="theme-color" content="{PWA_THEME_COLOR}">'
            f'<meta name="application-name" content="{app_name}">'
            '<meta name="mobile-web-app-capable" content="yes">'
            '<meta name="apple-mobile-web-app-capable" content="yes">'
            f'<meta name="apple-mobile-web-app-title" content="{app_name}">'
            f'<link rel="icon" type="image/png" sizes="192x192" href="{url_for("pwa_icon", size=192)}">'
            f'<link rel="apple-touch-icon" href="{url_for("pwa_icon", size=180)}">'
        )
        body = PWA_BODY_SNIPPET.replace("__SW_URL__", json_for_script(url_for("pwa_service_worker"))).replace(
            "__SCOPE__", json_for_script(url_for("index"))
        )
        html = html.replace("</head>", head + "</head>", 1)
        cut = html.rindex("</body>")
        response.set_data(html[:cut] + body + html[cut:])
        return response

    @app.route("/manifest.webmanifest")
    def pwa_manifest():
        icons = [
            {"src": url_for("pwa_icon", size=size), "sizes": f"{size}x{size}", "type": "image/png", "purpose": purpose}
            for size in (192, 512)
            for purpose in ("any", "maskable")
        ]
        manifest = {
            "name": cfg["web_app_name"],
            "short_name": cfg["web_app_short_name"],
            "description": "Sensor network data portal: station map, live dashboards, data browsing and download.",
            "id": url_for("index"),
            "start_url": url_for("index"),
            "scope": url_for("index"),
            "display": "standalone",
            "background_color": "#ffffff",
            "theme_color": PWA_THEME_COLOR,
            "icons": icons,
        }
        response = app.response_class(json.dumps(manifest), mimetype="application/manifest+json")
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.route("/service-worker.js")
    def pwa_service_worker():
        precache = [url_for("pwa_offline"), url_for("pwa_manifest")]
        precache += [url_for("pwa_icon", size=size) for size in PWA_ICON_SIZES]
        script = PWA_SERVICE_WORKER_JS.replace("__OFFLINE_URL__", json.dumps(url_for("pwa_offline"))).replace(
            "__PRECACHE__", json.dumps(precache)
        )
        response = app.response_class(script, mimetype="text/javascript")
        # Browsers must revalidate the worker so that updates are picked up.
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.route("/pwa/icon-<int:size>.png")
    def pwa_icon(size: int):
        if size not in PWA_ICON_SIZES:
            abort(404)
        response = app.response_class(build_pwa_icon_png(size), mimetype="image/png")
        response.headers["Cache-Control"] = "public, max-age=86400"
        return response

    @app.route("/offline")
    def pwa_offline():
        return render_template_string(
            """
            <!doctype html>
            <html lang="en">
            <head>
              <meta charset="utf-8">
              <title>Offline - {{ app_name }}</title>
              <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
              <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            </head>
            <body class="container py-5">
              <div class="card shadow-sm mx-auto" style="max-width: 32rem;">
                <div class="card-body text-center">
                  <h1 class="h4">You are offline</h1>
                  <p class="text-muted">{{ app_name }} needs a network connection to load station data.</p>
                  <button class="btn btn-primary" type="button" onclick="window.location.reload()">Try again</button>
                </div>
              </div>
            </body>
            </html>
            """,
            app_name=cfg["web_app_name"],
        )

    def send_image_asset(path: Path):
        mime, _ = mimetypes.guess_type(str(path))
        response = send_file(path, mimetype=mime or "application/octet-stream")
        # Uploaded SVG files may carry scripts; never let them run in this origin.
        response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
        return response

    def current_request_target() -> str:
        target = request.full_path or request.path or url_for("index")
        return target[:-1] if target.endswith("?") else target

    def redirect_to_login(message: str):
        return redirect(url_for("login", next=current_request_target(), err=message))

    def current_user():
        username = session.get("username")
        if not username:
            return None
        user = access_store.get_user(username)
        return user if user and int(user.get("active") or 0) == 1 else None

    @app.before_request
    def enforce_password_change():
        if request.endpoint in (
            "change_password", "logout", "asset_file", "static",
            "pwa_manifest", "pwa_service_worker", "pwa_icon", "pwa_offline",
        ):
            return None
        user = current_user()
        if user and int(user.get("force_password_change") or 0) == 1:
            if request.method not in ("GET", "HEAD") or request.path.startswith("/api/"):
                abort(403, "Password change required")
            return redirect(url_for("change_password"))
        return None

    def require_admin():
        user = current_user()
        if not user:
            return redirect_to_login("Please log in to access this page.")
        if user.get("role") != "admin":
            abort(403)
        return user

    def require_login():
        user = current_user()
        if not user:
            return redirect_to_login("Please log in to continue.")
        return user

    def storage_root_or_404():
        storage_root = cfg.get("storage_root")
        if not storage_root:
            abort(404, "Storage is not enabled/configured")
        return storage_root

    def available_instruments(storage_root: str):
        return collect_instruments(storage_root)

    def station_is_accessible(user, instrument_uuid: str):
        return access_store.can_download(user, instrument_uuid)

    def station_is_public(user, instrument_uuid: str):
        # Restricted stations are shown only to the users assigned to them.
        return access_store.get_policy(instrument_uuid) != "restricted" or access_store.can_download(
            user, instrument_uuid
        )

    def station_is_controllable(user, instrument_uuid: str):
        return access_store.can_control_station(user, instrument_uuid)

    def logo_url(path: str):
        if not path:
            return None
        p = Path(path)
        if not p.exists() or not p.is_file():
            return None
        return url_for("asset_file", kind="app", name=p.name)

    app_logo_path = str(cfg.get("web_app_logo") or "").strip()

    def station_logo_dir():
        root = storage_root_or_404()
        out = Path(root) / "_logos"
        out.mkdir(parents=True, exist_ok=True)
        return out

    @app.route("/assets/<kind>/<path:name>")
    def asset_file(kind: str, name: str):
        if kind == "app":
            p = Path(app_logo_path) if app_logo_path else None
            if p is None or not p.is_file() or p.name != name:
                abort(404)
            return send_image_asset(p)

        if kind == "station":
            p = station_logo_dir() / safe_filename(name)
            if not p.is_file():
                abort(404)
            return send_image_asset(p)
        abort(404)

    @app.route("/")
    def index():
        user = current_user()
        storage_root = cfg.get("storage_root")
        instruments = available_instruments(storage_root) if storage_root else []
        policies = access_store.list_policies(instruments)

        stations = []
        for inst in instruments:
            can_access = station_is_accessible(user, inst)
            if policies.get(inst) == "restricted" and not can_access:
                continue
            preview = get_station_preview(storage_root, inst)
            stations.append(
                {
                    "uuid": inst,
                    "name": preview["name"],
                    "policy": policies.get(inst, "account"),
                    "can_access": can_access,
                    "latitude": preview["latitude"],
                    "longitude": preview["longitude"],
                    "last_timestamp": preview["last_timestamp"],
                    "rows": preview["rows"],
                    "browse_url": url_for("browse_station", instrument_uuid=inst),
                    "public_url": url_for("public_station", instrument_uuid=inst),
                }
            )

        # Map center based on first station with known coordinates.
        center = {"lat": 40.0, "lon": 14.0, "zoom": 6}
        for st in stations:
            if st["latitude"] is not None and st["longitude"] is not None:
                center = {"lat": st["latitude"], "lon": st["longitude"], "zoom": 9}
                break

        clickable = [s for s in stations if s["can_access"]]

        return render_template_string(
            """
            <!doctype html>
            <html lang="en">
            <head>
              <meta charset="utf-8">
              <title>Sensor Network Collector - Data Portal</title>
              <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
              <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
              <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin=""/>
              <style>
                #map { height: 420px; border: 1px solid #ccc; margin-bottom: 16px; }
                .panel { border: 1px solid #ddd; padding: 12px; margin-bottom: 12px; border-radius: 8px; }
              </style>
            </head>
            <body class="container py-4">
              {% if app_logo_url %}
                {% if web_app_link %}<a href="{{ web_app_link }}" target="_blank" rel="noopener noreferrer">{% endif %}
                <img src="{{ app_logo_url }}" alt="App logo" style="max-height:56px; margin-bottom:8px;">
                {% if web_app_link %}</a>{% endif %}
              {% endif %}
              <h1>Sensor Network Collector - Data Portal</h1>
              {% if user %}
                <p>Logged in as <b>{{ user.username }}</b> ({{ user.role }}) - <a href="{{ url_for('logout') }}">Logout</a></p>
                <p><a href="{{ url_for('anomalies_log') }}">Anomalies log</a></p>
                {% if user.role == 'admin' %}<p><a href="{{ url_for('admin') }}">Admin panel</a></p>{% endif %}
              {% else %}
                <p>{% if web_info_link %}<a href="{{ web_info_link }}" target="_blank" rel="noopener noreferrer">Info</a> | {% endif %}<a href="{{ url_for('login') }}">Login</a> | <a href="{{ url_for('request_account') }}">Request account</a></p>
              {% endif %}

              <div class="panel">
                <h2>Stations map</h2>
                {% if not storage_root %}
                  <p>Storage is not enabled/configured.</p>
                {% elif not stations %}
                  <p>No stations found in the configured storage.</p>
                {% else %}
                  <div id="map"></div>
                  <p>Click a marker for the station details and its dashboard. Green markers: data browsing and download allowed. Gray markers: dashboard only with the current permissions.</p>
                {% endif %}
              </div>

              <div class="panel">
                <h2>Browse & download</h2>
                {% if not clickable %}
                  <p>No stations available with your current permissions.</p>
                {% else %}
                  <form method="post" action="{{ url_for('download') }}">
                    {% for st in clickable %}
                      <label>
                        <input type="checkbox" name="instrument" value="{{ st.uuid }}"> {{ st.name }} ({{ st.uuid }})
                        (policy={{ st.policy }})
                        <a href="{{ url_for('browse_station', instrument_uuid=st.uuid) }}">browse</a>
                        | <a href="{{ url_for('public_station', instrument_uuid=st.uuid) }}">public view</a>
                      </label><br/>
                    {% endfor %}
                    <p>Date filter (optional):</p>
                    <label>From <input class="form-control d-inline-block w-auto" type="date" name="from_date"></label>
                    <label>To <input class="form-control d-inline-block w-auto" type="date" name="to_date"></label>
                    <p><button class="btn btn-primary mt-2" type="submit">Download ZIP</button></p>
                  </form>
                {% endif %}
              </div>

              <div class="panel">
                <h2>Public station dashboards</h2>
                {% if not stations %}
                  <p>No stations found in the configured storage.</p>
                {% else %}
                  <ul>
                    {% for st in stations %}
                      <li><a href="{{ url_for('public_station', instrument_uuid=st.uuid) }}">{{ st.name }} ({{ st.uuid }})</a></li>
                    {% endfor %}
                  </ul>
                {% endif %}
              </div>

              <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
              <script>
                const stations = {{ stations | tojson }};
                const mapEl = document.getElementById('map');
                if (stations.length > 0 && mapEl && window.L) {
                  const map = L.map(mapEl).setView([{{ center.lat }}, {{ center.lon }}], {{ center.zoom }});
                  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                    maxZoom: 19,
                    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
                  }).addTo(map);
                  L.control.scale({ imperial: false }).addTo(map);

                  const positions = [];
                  stations.forEach((s) => {
                    const lat = Number(s.latitude);
                    const lon = Number(s.longitude);
                    if (s.latitude == null || s.longitude == null || !Number.isFinite(lat) || !Number.isFinite(lon)
                        || Math.abs(lat) > 90 || Math.abs(lon) > 180) {
                      return;
                    }
                    positions.push([lat, lon]);
                    const color = s.can_access ? '#1f9d55' : '#666';
                    const marker = L.circleMarker([lat, lon], {
                      radius: 9,
                      color: '#fff',
                      weight: 2,
                      fillColor: color,
                      fillOpacity: 0.9
                    }).addTo(map);

                    // Station names and timestamps come from sensor data: add them as text, never as HTML.
                    const popup = document.createElement('div');
                    const title = document.createElement('div');
                    title.className = 'fw-bold';
                    title.textContent = s.name || s.uuid;
                    popup.appendChild(title);
                    const addLine = (text) => {
                      const line = document.createElement('div');
                      line.className = 'small text-muted';
                      line.textContent = text;
                      popup.appendChild(line);
                    };
                    addLine(s.uuid);
                    addLine(`policy: ${s.policy}`);
                    if (s.last_timestamp) {
                      addLine(`last data: ${s.last_timestamp}`);
                    }
                    const actions = document.createElement('div');
                    actions.className = 'd-grid gap-1 mt-2';
                    const addButton = (href, text, style) => {
                      const a = document.createElement('a');
                      a.href = href;
                      a.className = `btn btn-sm ${style}`;
                      a.style.color = style === 'btn-primary' ? '#fff' : '';
                      a.textContent = text;
                      actions.appendChild(a);
                    };
                    addButton(s.public_url, 'Open dashboard', 'btn-primary');
                    if (s.can_access) {
                      addButton(s.browse_url, 'Browse & download data', 'btn-outline-secondary');
                    } else {
                      addLine('Log in to browse and download data.');
                    }
                    popup.appendChild(actions);
                    marker.bindPopup(popup, { minWidth: 180 });
                    marker.bindTooltip(document.createTextNode(s.name || s.uuid), { direction: 'top', offset: [0, -8] });
                  });

                  // Show every station, whatever the first one is.
                  if (positions.length > 1) {
                    map.fitBounds(positions, { padding: [30, 30], maxZoom: 12 });
                  } else if (positions.length === 1) {
                    map.setView(positions[0], 11);
                  }
                  // The container can change size after the first layout (fonts, installed-app window).
                  window.addEventListener('resize', () => map.invalidateSize());
                  setTimeout(() => map.invalidateSize(), 0);
                }
              </script>
            </body>
            </html>
            """,
            user=user,
            storage_root=storage_root,
            stations=stations,
            clickable=clickable,
            center=center,
            app_logo_url=logo_url(app_logo_path),
            web_app_link=cfg.get("web_app_link"),
            web_info_link=cfg.get("web_info_link"),
        )

    def load_station_window(instrument_uuid: str):
        """Access check and rows of the time range selected by the request arguments.

        Used by the station page and by its CSV export, so both show the same rows.
        """
        user = current_user()
        storage_root = storage_root_or_404()
        if instrument_uuid not in set(available_instruments(storage_root)):
            abort(404, "Station not found")
        if not station_is_accessible(user, instrument_uuid):
            if user is None:
                return redirect_to_login("Please log in to browse this station.")
            abort(403)

        preview = get_station_preview(storage_root, instrument_uuid)
        latest_dt = parse_iso_ts(preview.get("last_timestamp") or "") or utc_now()
        from_date = parse_date_ymd(request.args.get("from_date", ""))
        to_date = parse_date_ymd(request.args.get("to_date", ""))
        if from_date and to_date and to_date < from_date:
            abort(400, "to_date must be >= from_date")

        interval = request_preference_cookie(request, "interval", "station_trend_window", normalize_interval, "hour")
        if from_date or to_date:
            interval = "custom"
        if interval not in STATION_BROWSER_INTERVALS:
            interval = "hour"

        try:
            if interval == "custom":
                win_end = _as_utc_bound(to_date, end_of_day=True) if to_date else latest_dt
                win_start = _as_utc_bound(from_date, end_of_day=False) if from_date else win_end - timedelta(days=1)
                if win_end < win_start:
                    win_end = win_start + timedelta(days=1)
                if win_end - win_start > timedelta(days=STATION_BROWSER_MAX_CUSTOM_DAYS + 1):
                    abort(
                        400,
                        f"A custom range can cover at most {STATION_BROWSER_MAX_CUSTOM_DAYS} days; "
                        "use the ZIP download for longer periods",
                    )
                span = win_end - win_start
                prev_anchor, next_anchor = None, None
                prev_range = (win_start - span, win_start)
                next_range = (win_end, win_end + span)
            else:
                win_end = parse_iso_ts(request.args.get("anchor", "")) or latest_dt
                win_start = interval_start(win_end, interval)
                prev_anchor = shift_anchor(win_end, interval, -1)
                next_anchor = shift_anchor(win_end, interval, 1)
                prev_range = next_range = None
        except OverflowError:
            abort(400, "The requested time range is out of bounds")

        loaded = []
        for row in load_station_rows(storage_root, instrument_uuid, from_date=win_start, to_date=win_end, limit=None):
            ts = parse_iso_ts(row.get("timestamp", ""))
            if ts is not None:
                loaded.append((ts, row))
        if interval != "custom" and not request.args.get("anchor") and loaded:
            # The latest window ends at the newest row, which need not be the last line of the file.
            newest = max(ts for ts, _ in loaded)
            if newest > win_end:
                win_end = latest_dt = newest
                win_start = interval_start(win_end, interval)
                prev_anchor = shift_anchor(win_end, interval, -1)
                next_anchor = shift_anchor(win_end, interval, 1)
        rows_ts = sorted(((ts, row) for ts, row in loaded if win_start <= ts <= win_end), key=lambda item: item[0])

        return {
            "user": user,
            "storage_root": storage_root,
            "preview": preview,
            "latest_dt": latest_dt,
            "interval": interval,
            "win_start": win_start,
            "win_end": win_end,
            "prev_anchor": prev_anchor,
            "next_anchor": next_anchor,
            "prev_range": prev_range,
            "next_range": next_range,
            "from_date": from_date,
            "to_date": to_date,
            "rows": [row for _, row in rows_ts],
        }

    @app.route("/station/<path:instrument_uuid>/export.csv")
    def export_station_csv(instrument_uuid: str):
        window = load_station_window(instrument_uuid)
        if not isinstance(window, dict):
            return window
        rows = window["rows"]
        all_columns = list(dict.fromkeys(key for row in rows for key in row))
        wanted = [c for c in request.args.getlist("col") if c in all_columns]
        columns = wanted or all_columns
        if not rows:
            abort(404, "No data rows in the selected time range")

        def generate():
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(columns)
            for index, row in enumerate(rows, start=1):
                writer.writerow(["" if row.get(c) is None else row.get(c) for c in columns])
                if index % 500 == 0:
                    yield buffer.getvalue()
                    buffer.seek(0)
                    buffer.truncate(0)
            yield buffer.getvalue()

        stamp = "%Y%m%dT%H%M%SZ"
        filename = safe_filename(
            f"{instrument_uuid}_{window['win_start'].strftime(stamp)}_{window['win_end'].strftime(stamp)}.csv"
        )
        response = app.response_class(generate(), mimetype="text/csv")
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    @app.route("/station/<path:instrument_uuid>")
    def browse_station(instrument_uuid: str):
        window = load_station_window(instrument_uuid)
        if not isinstance(window, dict):
            return window
        user = window["user"]
        rows = window["rows"]
        interval = window["interval"]
        station_name = window["preview"].get("name") or instrument_uuid
        can_control = station_is_controllable(user, instrument_uuid)
        station_logo_row = access_store.get_station_logo(instrument_uuid)
        station_logo_url = None
        if station_logo_row:
            station_logo_url = url_for("asset_file", kind="station", name=Path(station_logo_row["logo_path"]).name)

        excluded = {"timestamp", "topic", "uuid", "position", "latitude", "longitude", "lat", "lon", "lng"}
        numeric_cols = extract_numeric_series(rows, excluded=excluded)
        units_map = get_field_units(cfg)

        # Long windows hold tens of thousands of samples: the chart gets an evenly
        # thinned series, while the table, the statistics and the CSV keep every row.
        chart_step = max(1, math.ceil(len(rows) / STATION_BROWSER_MAX_CHART_POINTS))
        chart_rows = rows[::chart_step]
        if chart_step > 1 and chart_rows[-1] is not rows[-1]:
            chart_rows.append(rows[-1])
        chart_labels = [str(row.get("timestamp") or "") for row in chart_rows]
        numeric_series_aligned = {field: [_to_float(row.get(field)) for row in chart_rows] for field in numeric_cols}

        # Hourly files can have different headers, so use every column seen in the window.
        all_table_columns = list(dict.fromkeys(key for row in rows for key in row))
        page_size = int(
            request_preference_cookie(
                request, "page_size", "station_page_size",
                lambda v: v if v in STATION_BROWSER_PAGE_SIZES else "50", "50",
            )
        )
        order = request_preference_cookie(
            request, "order", "station_row_order", lambda v: "desc" if v == "desc" else "asc", "asc"
        )
        try:
            page = max(1, int(request.args.get("page", "1") or "1"))
        except ValueError:
            page = 1
        total_rows = len(rows)
        page_count = max(1, (total_rows + page_size - 1) // page_size)
        page = min(page, page_count)
        start_idx = (page - 1) * page_size
        ordered_rows = rows[::-1] if order == "desc" else rows
        table_rows = ordered_rows[start_idx:start_idx + page_size]
        table_column_stats = build_table_column_stats(rows, numeric_cols)

        def iso(dt):
            return dt.isoformat().replace("+00:00", "Z")

        # Arguments that identify the current time range, reused by every link of the page.
        if interval == "custom":
            range_args = {
                "from_date": window["win_start"].date().isoformat(),
                "to_date": window["win_end"].date().isoformat(),
            }
            prev_args = {
                "from_date": window["prev_range"][0].date().isoformat(),
                "to_date": (window["prev_range"][1] - timedelta(seconds=1)).date().isoformat(),
            }
            next_args = {
                "from_date": (window["next_range"][0] + timedelta(seconds=1)).date().isoformat(),
                "to_date": window["next_range"][1].date().isoformat(),
            }
        else:
            range_args = {"interval": interval}
            if request.args.get("anchor"):
                range_args["anchor"] = iso(window["win_end"])
            prev_args = {"interval": interval, "anchor": iso(window["prev_anchor"])}
            next_args = {"interval": interval, "anchor": iso(window["next_anchor"])}
        is_latest = window["win_end"] >= window["latest_dt"]

        return render_template_string(
            STATION_BROWSER_TEMPLATE,
            instrument_uuid=instrument_uuid,
            station_name=station_name,
            user=user,
            can_control=can_control,
            app_logo_url=logo_url(app_logo_path),
            station_logo_url=station_logo_url,
            interval=interval,
            interval_options=TREND_INTERVALS,
            chart_step=chart_step,
            win_start=iso(window["win_start"]),
            win_end=iso(window["win_end"]),
            range_args=range_args,
            prev_args=prev_args,
            next_args=next_args,
            is_latest=is_latest,
            numeric_cols=numeric_cols,
            default_chart_colors=DEFAULT_CHART_COLORS,
            table_rows=table_rows,
            all_table_columns=all_table_columns,
            table_column_stats=table_column_stats,
            units_map=units_map,
            page=page,
            page_count=page_count,
            page_size=page_size,
            page_sizes=STATION_BROWSER_PAGE_SIZES,
            order=order,
            total_rows=total_rows,
            start_idx=start_idx,
            end_idx=min(start_idx + page_size, total_rows),
            max_custom_days=STATION_BROWSER_MAX_CUSTOM_DAYS,
            station_browse_state_json=json_for_script(
                {
                    "chart_labels": chart_labels,
                    "numeric_cols": numeric_cols,
                    "all_table_columns": all_table_columns,
                    "units_map": units_map,
                    "numeric_series_aligned": numeric_series_aligned,
                }
            ),
        )

    @app.route("/station/<path:instrument_uuid>/logo", methods=["POST"])
    def upload_station_logo(instrument_uuid: str):
        user = require_login()
        if not isinstance(user, dict):
            return user
        storage_root = storage_root_or_404()
        instruments = set(available_instruments(storage_root))
        if instrument_uuid not in instruments:
            abort(404, "Station not found")
        if not station_is_accessible(user, instrument_uuid):
            abort(403)

        f = request.files.get("logo")
        if f is None or not f.filename:
            abort(400, "Missing logo file")
        original = safe_filename(f.filename)
        ext = Path(original).suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
            abort(400, "Unsupported logo format")

        content = f.read()
        if not content:
            abort(400, "Empty logo file")
        digest = hashlib.sha256(content).hexdigest()[:12]
        out_name = safe_filename(f"{instrument_uuid}_{digest}{ext}")
        out_path = station_logo_dir() / out_name
        out_path.write_bytes(content)
        access_store.set_station_logo(instrument_uuid, str(out_path), user["username"])
        return redirect(url_for("browse_station", instrument_uuid=instrument_uuid))


    @app.route("/public/station/<path:instrument_uuid>")
    def public_station(instrument_uuid: str):
        storage_root = storage_root_or_404()
        instruments = set(available_instruments(storage_root))
        if instrument_uuid not in instruments:
            abort(404, "Station not found")

        user = current_user()
        if not station_is_public(user, instrument_uuid):
            if user is None:
                return redirect_to_login("Please log in to view this station.")
            abort(404, "Station not found")
        can_browse_download = bool(user) and station_is_accessible(user, instrument_uuid)
        can_control = bool(user) and station_is_controllable(user, instrument_uuid)
        selected_window = request_preference_cookie(request, "window", "public_trend_window", normalize_public_window, "hour")
        selected_focus = str(request.args.get("focus", "") or "").strip()
        snapshot = build_public_station_snapshot(
            storage_root,
            instrument_uuid,
            window=selected_window,
            cfg=cfg,
            access_store=access_store,
        )
        station_logo_row = access_store.get_station_logo(instrument_uuid)
        station_logo_url = None
        if station_logo_row:
            station_logo_url = url_for("asset_file", kind="station", name=Path(station_logo_row["logo_path"]).name)
        return render_template_string(
            """
            <!doctype html>
            <html lang="en">
            <head>
              <meta charset="utf-8">
              <title>Public Station View - {{ snapshot.station_name }}</title>
              <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
              <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
              <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
              <style>
                html, body { min-height: 100%; overflow-y: auto; }
                .dashboard-root { min-height: 100vh; display: flex; flex-direction: column; }
                #cards .card-body { padding: .3rem .45rem; }
                .metric-label { font-size: .72rem; line-height: 1.1; margin-bottom: .1rem; }
                .metric-value { font-size: .95rem; line-height: 1.0; font-weight: 600; }
                .metric-unit { font-size: .72rem; margin-left: .2rem; }
                #chartsWrap { flex: 1 1 auto; min-height: 0; overflow: visible; }
                .chart-card { height: 170px; }
                .chart-card .card-body { height: 100%; display: flex; flex-direction: column; }
                .chart-card canvas { height: 112px !important; }
                .chart-card.fullscreen {
                  position: fixed;
                  inset: 10px;
                  z-index: 2000;
                  height: auto;
                  border: 2px solid rgba(13,110,253,.18);
                  box-shadow: 0 1rem 3rem rgba(0,0,0,.25);
                }
                .chart-card.fullscreen canvas { height: calc(100vh - 330px) !important; }
                .chart-card .fullscreen-branding { display: none; }
                .chart-card.fullscreen .fullscreen-branding {
                  display: grid;
                  grid-template-columns: minmax(90px, 140px) 1fr minmax(90px, 140px);
                  align-items: center;
                  gap: .75rem;
                  margin-bottom: .5rem;
                }
                .fullscreen-branding-logos { display: flex; align-items: center; gap: .5rem; }
                .fullscreen-branding-logo-left { justify-content: flex-start; }
                .fullscreen-branding-logo-right { justify-content: flex-end; }
                .fullscreen-branding img { max-height: 38px; max-width: 100%; object-fit: contain; }
                .fullscreen-station-name { font-weight: 700; font-size: 1.35rem; text-align: center; line-height: 1.15; }
                .chart-card .fullscreen-stats { display: none; }
                .chart-card.fullscreen .fullscreen-stats {
                  display: grid;
                  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
                  gap: .5rem;
                  margin-top: auto;
                  padding-top: .5rem;
                  align-items: stretch;
                }
                .chart-card.fullscreen .fullscreen-hint { display: none; }
                .chart-card .fullscreen-hint { font-size: .72rem; color: #6c757d; }
                .stat-chip {
                  border: 1px solid #dee2e6;
                  border-radius: .5rem;
                  padding: .5rem .65rem;
                  background: #fff;
                }
                .stat-chip-grid {
                  display: grid;
                  grid-template-columns: repeat(3, minmax(0, 1fr));
                  gap: .6rem;
                }
                .stat-chip-section {
                  min-width: 0;
                  display: flex;
                  flex-direction: column;
                  gap: .15rem;
                }
                .stat-chip-label { font-size: .72rem; color: #6c757d; }
                .stat-chip-value { font-weight: 700; line-height: 1.1; }
                .stat-chip-time {
                  font-size: .72rem;
                  color: #6c757d;
                  line-height: 1.2;
                  word-break: break-word;
                }
                .aqi-card { min-height: 94px; }
                .aqi-stack { display: flex; gap: .45rem; align-items: center; }
                .aqi-lights { display: flex; flex-direction: column-reverse; gap: .35rem; }
                .aqi-light { width: 16px; height: 16px; border-radius: 50%; background: #d9d9d9; border: 1px solid rgba(0,0,0,.15); opacity: .35; }
                .aqi-light.active { opacity: 1; box-shadow: 0 0 0 2px rgba(0,0,0,.05); }
                .aqi-light.good.active { background: #198754; }
                .aqi-light.warning.active { background: #ffc107; }
                .aqi-light.bad.active { background: #dc3545; }
                .aqi-status-value { font-size: 1.4rem; line-height: 1; font-weight: 700; }
                body.chart-focus-active #cards,
                body.chart-focus-active #aqiStatus,
                body.chart-focus-active #charts > div:not(.focus-host) { display: none !important; }
                body.chart-focus-active #charts > div.focus-host { display: block !important; width: 100%; }
              </style>
            </head>
            <body class="bg-light">
              <div class="container-fluid py-3 dashboard-root">
              <div class="d-flex flex-wrap justify-content-between align-items-center mb-3">
                <div>
                  <div class="mb-2">
                    {% if app_logo_url %}<img src="{{ app_logo_url }}" alt="App logo" style="max-height:44px; margin-right:8px;">{% endif %}
                    {% if station_logo_url %}<img src="{{ station_logo_url }}" alt="Station logo" style="max-height:44px;">{% endif %}
                  </div>
                  <h1 class="h3 mb-1">Public Station Dashboard</h1>
                  <div class="text-muted" id="stationTitle">{{ snapshot.station_name }} ({{ snapshot.instrument_uuid }})</div>
                </div>
                <div class="text-end">
                  <div class="mb-2">
                    <label class="small text-muted">Trend window</label>
                    <select id="windowSelect" class="form-select form-select-sm">
                      {% for code, label in window_options %}
                        <option value="{{ code }}" {% if code == selected_window %}selected{% endif %}>{{ label }}</option>
                      {% endfor %}
                    </select>
                  </div>
                  <div class="small text-muted">Last update</div>
                  <div class="fw-semibold" id="lastUpdate">{{ snapshot.last_timestamp or "-" }}</div>
                  {% if can_control %}
                    <a class="btn btn-sm btn-outline-secondary mt-2" href="{{ url_for('station_chart_settings', instrument_uuid=snapshot.instrument_uuid) }}">Trend chart axis settings</a>
                  {% endif %}
                  {% if can_browse_download %}
                    <a class="btn btn-sm btn-primary mt-2" href="{{ url_for('browse_station', instrument_uuid=snapshot.instrument_uuid) }}">Browse & Download Data</a>
                  {% endif %}
                </div>
              </div>

              <div id="cards" class="row g-1 mb-1"></div>
              <div id="aqiStatus" class="row g-1 mb-1"></div>
              <div id="chartsWrap">
                <div id="charts" class="row g-1"></div>
              </div>

              <script>
                const snapshotUrl = {{ url_for('public_station_snapshot', instrument_uuid=snapshot.instrument_uuid) | tojson }};
                let lastTimestamp = {{ snapshot.last_timestamp | tojson }};
                let currentWindow = {{ selected_window | tojson }};
                const chartInstances = {};
                const chartCards = {};
                const windowSelect = document.getElementById('windowSelect');
                let focusedChartKey = {{ selected_focus | tojson }};
                let renderedWindow = currentWindow;
                const appLogoUrl = {{ app_logo_url | tojson }};
                const stationLogoUrl = {{ station_logo_url | tojson }};
                const colors = ['#0d6efd', '#20c997', '#fd7e14', '#6f42c1', '#dc3545', '#198754', '#6c757d'];
                const SECOND_MS = 1000;
                const MINUTE_MS = 60 * SECOND_MS;
                const HOUR_MS = 60 * MINUTE_MS;

                function normalizeChartPoints(points) {
                  return (points || [])
                    .map((p) => {
                      const x = Date.parse(p.x);
                      const y = Number(p.y);
                      if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
                      return { x, y };
                    })
                    .filter(Boolean);
                }

                function ceilToStep(ts, stepMs) {
                  if (!Number.isFinite(ts) || !stepMs) return ts;
                  return Math.ceil(ts / stepMs) * stepMs;
                }

                function appendSteppedTicks(ticks, startMs, endMs, stepMs) {
                  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || !stepMs || endMs < startMs) return;
                  for (let tick = ceilToStep(startMs, stepMs); tick <= endMs; tick += stepMs) {
                    ticks.push(tick);
                  }
                }

                function buildWindowTicks(windowCode, xMin, xMax) {
                  if (!Number.isFinite(xMin) || !Number.isFinite(xMax) || xMax <= xMin) return [];
                  const ticks = [xMin];
                  if (windowCode === '1m') {
                    appendSteppedTicks(ticks, xMin, xMax, 10 * SECOND_MS);
                  } else if (windowCode === '10m') {
                    appendSteppedTicks(ticks, xMin, xMax, 1 * MINUTE_MS);
                  } else if (windowCode === 'hour') {
                    appendSteppedTicks(ticks, xMin, xMax, 10 * MINUTE_MS);
                  } else if (windowCode === '3h') {
                    appendSteppedTicks(ticks, xMin, xMax, 15 * MINUTE_MS);
                  } else if (windowCode === '6h') {
                    appendSteppedTicks(ticks, xMin, xMax, 30 * MINUTE_MS);
                  } else if (windowCode === '12h') {
                    appendSteppedTicks(ticks, xMin, xMax, 1 * HOUR_MS);
                  } else if (windowCode === '24h') {
                    appendSteppedTicks(ticks, xMin, xMax - HOUR_MS, 3 * HOUR_MS);
                    appendSteppedTicks(ticks, Math.max(xMin, xMax - HOUR_MS), xMax, 15 * MINUTE_MS);
                  } else if (windowCode === '72h') {
                    appendSteppedTicks(ticks, xMin, xMax - 3 * HOUR_MS, 6 * HOUR_MS);
                    appendSteppedTicks(ticks, Math.max(xMin, xMax - 3 * HOUR_MS), xMax - HOUR_MS, 1 * HOUR_MS);
                    appendSteppedTicks(ticks, Math.max(xMin, xMax - HOUR_MS), xMax, 15 * MINUTE_MS);
                  } else {
                    appendSteppedTicks(ticks, xMin, xMax, 1 * HOUR_MS);
                  }
                  ticks.push(xMax);
                  return [...new Set(ticks.map((v) => Math.round(v)))].sort((a, b) => a - b);
                }

                function formatWindowTick(value, windowCode) {
                  const d = new Date(value);
                  if (Number.isNaN(d.getTime())) return '';
                  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
                }

                function windowLabel(windowCode) {
                  const options = new Map({{ window_options | tojson }});
                  return options.get(windowCode) || windowCode || '';
                }

                function formatWindowTickWithDayChange(value, windowCode, index, ticks) {
                  const d = new Date(value);
                  if (Number.isNaN(d.getTime())) return '';
                  if (windowCode === '1m') {
                    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
                  }
                  const prevTick = (Array.isArray(ticks) && index > 0) ? ticks[index - 1] : null;
                  const prevDate = prevTick && Number.isFinite(prevTick.value) ? new Date(prevTick.value) : null;
                  const dayChanged = !prevDate || prevDate.toDateString() !== d.toDateString();
                  const timeLabel = formatWindowTick(value, windowCode);
                  if (dayChanged) {
                    // Two lines: the date under the time keeps the label as narrow as the others.
                    return [timeLabel, d.toLocaleDateString([], { year: 'numeric', month: '2-digit', day: '2-digit' })];
                  }
                  return timeLabel;
                }

                function formatStatTimestamp(value) {
                  const d = new Date(value);
                  if (Number.isNaN(d.getTime())) return '';
                  return d.toLocaleString([], {
                    year: 'numeric',
                    month: '2-digit',
                    day: '2-digit',
                    hour: '2-digit',
                    minute: '2-digit'
                  });
                }

                function cardHtml(card) {
                  const value = (card.value === null || card.value === undefined) ? '--' : card.value;
                  const unit = card.unit || '';
                  return `
                    <div class="col-6 col-md-4 col-xl-2">
                      <div class="card h-100 shadow-sm">
                        <div class="card-body">
                          <div class="text-muted metric-label">${card.label}</div>
                          <div class="metric-value">${value}<span class="metric-unit">${unit}</span></div>
                        </div>
                      </div>
                    </div>
                  `;
                }

                function renderAqiStatus(snapshot) {
                  const el = document.getElementById('aqiStatus');
                  const status = snapshot.aqi_status;
                  if (!el) return;
                  if (!status) {
                    el.innerHTML = '';
                    return;
                  }
                  const valueCard = (snapshot.cards || []).find((c) => c.key === 'aqi_current');
                  const value = valueCard && valueCard.value !== null && valueCard.value !== undefined ? valueCard.value : '--';
                  el.innerHTML = `
                    <div class="col-12 col-md-6 col-xl-3">
                      <div class="card shadow-sm aqi-card">
                        <div class="card-body">
                          <div class="text-muted metric-label">Air Quality Status</div>
                          <div class="aqi-stack">
                            <div class="aqi-lights">
                              <span class="aqi-light good ${status.level === 'good' ? 'active' : ''}"></span>
                              <span class="aqi-light warning ${status.level === 'warning' ? 'active' : ''}"></span>
                              <span class="aqi-light bad ${status.level === 'bad' ? 'active' : ''}"></span>
                            </div>
                            <div>
                              <div class="aqi-status-value">${value}</div>
                              <div class="fw-semibold">${status.label}</div>
                            </div>
                          </div>
                        </div>
                      </div>
                    </div>
                  `;
                }

                function renderCards(snapshot) {
                  const cardsEl = document.getElementById('cards');
                  cardsEl.innerHTML = (snapshot.cards || []).filter(c => c.value !== null && c.value !== undefined).map(cardHtml).join('');
                  renderAqiStatus(snapshot);
                }

                function renderSeries(snapshot) {
                  const chartsEl = document.getElementById('charts');
                  const series = snapshot.series || [];
                  const knownKeys = new Set(series.map((item) => item.key));
                  if (focusedChartKey && !knownKeys.has(focusedChartKey)) {
                    focusedChartKey = '';
                  }
                  const xMin = snapshot.window_start ? Date.parse(snapshot.window_start) : undefined;
                  const xMax = snapshot.window_end ? Date.parse(snapshot.window_end) : undefined;
                  const customTicks = buildWindowTicks(snapshot.window || currentWindow, xMin, xMax);
                  // Shared with the double-click handlers, which are bound when a chart is first drawn.
                  renderedWindow = snapshot.window || currentWindow;
                  const renderStats = (stats, unit) => {
                    const items = Array.isArray(stats) ? stats.filter(Boolean) : (stats ? [stats] : []);
                    if (!items.length) {
                      return '<div class="text-muted small">No numeric data available for the selected window.</div>';
                    }
                    return items.map((item) => `
                      <div class="stat-chip">
                        <div class="fw-semibold small">${item.label || ''}</div>
                        <div class="stat-chip-grid">
                          <div class="stat-chip-section">
                            <div class="stat-chip-label">current</div>
                            <div class="stat-chip-value">${item.current}${unit ? ` ${unit}` : ''}</div>
                          </div>
                          <div class="stat-chip-section">
                            <div class="stat-chip-label">min</div>
                            <div class="stat-chip-value">${item.min}${unit ? ` ${unit}` : ''}</div>
                            <div class="stat-chip-time">${item.min_at ? formatStatTimestamp(item.min_at) : ''}</div>
                          </div>
                          <div class="stat-chip-section">
                            <div class="stat-chip-label">max</div>
                            <div class="stat-chip-value">${item.max}${unit ? ` ${unit}` : ''}</div>
                            <div class="stat-chip-time">${item.max_at ? formatStatTimestamp(item.max_at) : ''}</div>
                          </div>
                        </div>
                      </div>
                    `).join('');
                  };
                  const syncFocusUrl = () => {
                    const url = new URL(window.location.href);
                    if (focusedChartKey) {
                      url.searchParams.set('focus', focusedChartKey);
                    } else {
                      url.searchParams.delete('focus');
                    }
                    window.history.replaceState({}, '', url.toString());
                  };
                  const applyChartFocus = () => {
                    document.body.classList.toggle('chart-focus-active', Boolean(focusedChartKey));
                    Array.from(chartsEl.children).forEach((col) => {
                      const isFocused = focusedChartKey && col.dataset.chartKey === focusedChartKey;
                      col.classList.toggle('focus-host', Boolean(isFocused));
                      const card = col.querySelector('.chart-card');
                      if (card) {
                        card.classList.toggle('fullscreen', Boolean(isFocused));
                      }
                      const titleEl = col.querySelector('.chart-title');
                      if (titleEl) {
                        const baseLabel = col.dataset.chartLabel || '';
                        titleEl.textContent = isFocused ? `${baseLabel} - ${windowLabel(renderedWindow)}` : baseLabel;
                      }
                    });
                    syncFocusUrl();
                  };

                  Object.keys(chartCards).forEach((key) => {
                    if (knownKeys.has(key)) return;
                    if (chartInstances[key]) {
                      chartInstances[key].destroy();
                      delete chartInstances[key];
                    }
                    if (chartCards[key]) {
                      chartCards[key].remove();
                      delete chartCards[key];
                    }
                  });

                  const styleDataset = (dataset, color) => {
                    const pointCount = Array.isArray(dataset.data) ? dataset.data.length : 0;
                    const sparse = pointCount <= 2;
                    const datasetType = dataset.type === 'bar' ? 'bar' : 'line';
                    return {
                      ...dataset,
                      type: datasetType,
                      borderColor: color,
                      backgroundColor: datasetType === 'bar' ? `${color}88` : color,
                      borderWidth: 2,
                      tension: sparse ? 0 : 0.25,
                      spanGaps: true,
                      showLine: datasetType === 'bar' ? false : pointCount > 1,
                      pointRadius: sparse ? 3 : 0,
                      pointHoverRadius: sparse ? 4 : 0
                    };
                  };

                  series.forEach((s, idx) => {
                    const id = `chart_${s.key}`;
                    const yMin = (s.y_min !== null && s.y_min !== undefined) ? s.y_min : undefined;
                    const yMax = (s.y_max !== null && s.y_max !== undefined) ? s.y_max : undefined;
                    const yStep = (s.y_step !== null && s.y_step !== undefined) ? s.y_step : undefined;
                    const datasets = (s.datasets && Array.isArray(s.datasets))
                      ? s.datasets.map((d, j) => styleDataset({
                          label: d.label,
                          data: normalizeChartPoints(d.points),
                          type: d.type,
                          yAxisID: d.yAxisID
                        }, colors[(idx + j) % colors.length]))
                      : [styleDataset({
                          label: s.label,
                          data: normalizeChartPoints(s.points)
                        }, colors[idx % colors.length])];
                    const scales = {
                      x: {
                        type: 'linear',
                        min: xMin,
                        max: xMax,
                        afterBuildTicks: (axis) => {
                          // Keep the ticks whose level labels fit side by side, starting
                          // from the newest one: the height is left to the plot.
                          const span = axis.max - axis.min;
                          const minGapPx = 72;
                          let lastPx = Infinity;
                          const kept = [];
                          for (let i = customTicks.length - 1; i >= 0; i -= 1) {
                            const px = span > 0 ? ((customTicks[i] - axis.min) / span) * axis.width : 0;
                            if (lastPx - px < minGapPx) continue;
                            lastPx = px;
                            kept.unshift({ value: customTicks[i] });
                          }
                          axis.ticks = kept;
                        },
                        ticks: {
                          maxRotation: 0,
                          autoSkip: false,
                          callback: (value, index, ticks) => {
                            return formatWindowTickWithDayChange(value, snapshot.window || currentWindow, index, ticks);
                          }
                        }
                      }
                    };
                    if (s.axes && typeof s.axes === 'object') {
                      Object.entries(s.axes).forEach(([axisId, axis]) => {
                        const axisStep = (axis.y_step !== null && axis.y_step !== undefined) ? axis.y_step : undefined;
                        scales[axisId] = {
                          type: 'linear',
                          display: true,
                          position: axis.position === 'right' ? 'right' : 'left',
                          min: axis.y_min !== null && axis.y_min !== undefined ? axis.y_min : undefined,
                          max: axis.y_max !== null && axis.y_max !== undefined ? axis.y_max : undefined,
                          ticks: axisStep ? { stepSize: axisStep } : {},
                          title: { display: Boolean(axis.unit), text: axis.unit || '' },
                          grid: { drawOnChartArea: axis.position !== 'right' }
                        };
                      });
                    } else {
                      scales.y = {
                        beginAtZero: false,
                        min: yMin,
                        max: yMax,
                        ticks: yStep ? { stepSize: yStep } : {},
                        title: { display: Boolean(s.unit), text: s.unit || '' }
                      };
                    }
                    let col = chartCards[s.key];
                    if (!col) {
                      col = document.createElement('div');
                      col.className = 'col-12 col-md-6 col-xl-4';
                      col.dataset.chartKey = s.key;
                      col.innerHTML = `
                        <div class="card chart-card shadow-sm">
                          <div class="card-body">
                            <div class="fullscreen-branding">
                              <div class="fullscreen-branding-logos fullscreen-branding-logo-left">
                                ${appLogoUrl ? `<img src="${appLogoUrl}" alt="App logo">` : ''}
                              </div>
                              <div class="fullscreen-station-name"></div>
                              <div class="fullscreen-branding-logos fullscreen-branding-logo-right">
                                ${stationLogoUrl ? `<img src="${stationLogoUrl}" alt="Station logo">` : ''}
                              </div>
                            </div>
                            <div class="d-flex justify-content-between">
                              <h2 class="h6 mb-1 chart-title"></h2>
                              <span class="text-muted small chart-unit"></span>
                            </div>
                            <div class="fullscreen-hint mb-1">Double-click to focus this chart.</div>
                            <canvas id="${id}" height="110"></canvas>
                            <div class="fullscreen-stats"></div>
                          </div>
                        </div>
                      `;
                      chartsEl.appendChild(col);
                      chartCards[s.key] = col;

                      const card = col.querySelector('.chart-card');
                      const canvas = col.querySelector('canvas');
                      const toggleFocus = () => {
                        focusedChartKey = (focusedChartKey === s.key) ? '' : s.key;
                        applyChartFocus();
                      };
                      if (card) {
                        card.addEventListener('dblclick', toggleFocus);
                      }
                      if (canvas) {
                        canvas.addEventListener('dblclick', (event) => {
                          event.preventDefault();
                          event.stopPropagation();
                          toggleFocus();
                        });
                      }
                    } else if (!chartsEl.contains(col)) {
                      chartsEl.appendChild(col);
                    }

                    col.dataset.chartLabel = s.label;
                    const titleEl = col.querySelector('.chart-title');
                    titleEl.textContent = s.label;
                    col.querySelector('.chart-unit').textContent = s.unit || '';
                    col.querySelector('.fullscreen-station-name').textContent = snapshot.station_name || snapshot.instrument_uuid;
                    col.querySelector('.fullscreen-stats').innerHTML = renderStats(s.stats, s.unit || '');
                    const canvas = col.querySelector('canvas');
                    if (!canvas) return;

                    if (!chartInstances[s.key]) {
                      chartInstances[s.key] = new Chart(canvas, {
                        type: datasets.some((d) => d.type === 'bar') ? 'bar' : 'line',
                        data: {
                          labels: s.labels,
                          datasets
                        },
                        options: {
                          responsive: true,
                          maintainAspectRatio: false,
                          plugins: {
                            legend: { display: datasets.length > 1 },
                            tooltip: { enabled: false }
                          },
                          scales
                        }
                      });
                    } else {
                      const chart = chartInstances[s.key];
                      chart.data.labels = s.labels;
                      chart.data.datasets = datasets;
                      chart.config.type = datasets.some((d) => d.type === 'bar') ? 'bar' : 'line';
                      chart.options.plugins.legend.display = datasets.length > 1;
                      chart.options.scales = scales;
                      chart.update('none');
                    }
                  });
                  applyChartFocus();
                }

                function applySnapshot(snapshot) {
                  currentWindow = snapshot.window || currentWindow;
                  if (windowSelect && windowSelect.value !== currentWindow) {
                    windowSelect.value = currentWindow;
                  }
                  document.getElementById('stationTitle').textContent = `${snapshot.station_name} (${snapshot.instrument_uuid})`;
                  document.getElementById('lastUpdate').textContent = snapshot.last_timestamp || '-';
                  renderCards(snapshot);
                  renderSeries(snapshot);
                }

                async function pollSnapshot(forceRefresh = false) {
                  try {
                    const qs = new URLSearchParams({
                      since: lastTimestamp || '',
                      window: currentWindow,
                      force: forceRefresh ? '1' : '0'
                    });
                    const resp = await fetch(`${snapshotUrl}?${qs.toString()}`, { cache: 'no-store' });
                    if (!resp.ok) return;
                    const data = await resp.json();
                    if (!data.changed) return;
                    lastTimestamp = data.snapshot.last_timestamp;
                    applySnapshot(data.snapshot);
                  } catch (_) {
                    // keep page live even if polling occasionally fails
                  }
                }

                applySnapshot({{ snapshot | tojson }});
                if (windowSelect) {
                  windowSelect.addEventListener('change', () => {
                    currentWindow = windowSelect.value;
                    document.cookie = `public_trend_window=${encodeURIComponent(currentWindow)}; path=/; max-age=31536000; samesite=lax`;
                    const url = new URL(window.location.href);
                    url.searchParams.set('window', currentWindow);
                    window.history.replaceState({}, '', url.toString());
                    pollSnapshot(true);
                  });
                }
                window.addEventListener('keydown', (event) => {
                  if (event.key === 'Escape' && focusedChartKey) {
                    focusedChartKey = '';
                    const chartsEl = document.getElementById('charts');
                    document.body.classList.remove('chart-focus-active');
                    Array.from(chartsEl.children).forEach((col) => {
                      col.classList.remove('focus-host');
                      const card = col.querySelector('.chart-card');
                      if (card) card.classList.remove('fullscreen');
                    });
                    const url = new URL(window.location.href);
                    url.searchParams.delete('focus');
                    window.history.replaceState({}, '', url.toString());
                  }
                });
                setInterval(pollSnapshot, 5000);
              </script>
              </div>
            </body>
            </html>
            """,
            snapshot=snapshot,
            can_browse_download=can_browse_download,
            can_control=can_control,
            app_logo_url=logo_url(app_logo_path),
            station_logo_url=station_logo_url,
            selected_window=selected_window,
            selected_focus=selected_focus,
            window_options=PUBLIC_TREND_WINDOWS,
        )

    @app.route("/api/public/station/<path:instrument_uuid>/snapshot")
    def public_station_snapshot(instrument_uuid: str):
        storage_root = storage_root_or_404()
        instruments = set(available_instruments(storage_root))
        if instrument_uuid not in instruments or not station_is_public(current_user(), instrument_uuid):
            abort(404, "Station not found")

        window = normalize_public_window(request.args.get("window", "hour"))
        since = request.args.get("since", "")
        force = parse_boolish(request.args.get("force", "0"), False)
        since_dt = parse_iso_ts(since)
        if since_dt is not None and not force:
            # Most polls arrive before the station has sent new data: answer those
            # from the latest stored row instead of rebuilding the whole snapshot.
            latest = get_station_preview(storage_root, instrument_uuid).get("last_timestamp")
            if parse_iso_ts(latest or "") == since_dt:
                return jsonify({"changed": False})
        snapshot = build_public_station_snapshot(
            storage_root,
            instrument_uuid,
            window=window,
            cfg=cfg,
            access_store=access_store,
        )
        changed = bool(snapshot.get("last_timestamp")) and snapshot.get("last_timestamp") != since
        if force:
            changed = True
        if not since:
            changed = True
        return jsonify({"changed": changed, "snapshot": snapshot})

    @app.route("/login", methods=["GET", "POST"])
    def login():
        err = str(request.args.get("err", "") or "").strip()
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = access_store.authenticate(username, password)
            if user:
                session.clear()
                session["username"] = user["username"]
                if int(user.get("force_password_change", 0)) == 1:
                    return redirect(url_for("change_password"))
                nxt = request.args.get("next") or url_for("index")
                if (
                    not nxt.startswith("/")
                    or nxt.startswith("//")
                    or "\\" in nxt
                    or any(ord(ch) < 32 or ord(ch) == 127 for ch in nxt)
                ):
                    nxt = url_for("index")
                return redirect(nxt)
            err = "Invalid credentials or inactive user"

        return render_template_string(
            """
            <!doctype html>
            <html lang="en">
            <head>
              <meta charset="utf-8">
              <title>Login</title>
              <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
              <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            </head>
            <body class="container py-5">
              <div class="row justify-content-center">
                <div class="col-12 col-md-6 col-lg-4">
                  <h1 class="h3 mb-3">Login</h1>
                  {% if err %}<div class="alert alert-danger">{{ err }}</div>{% endif %}
                  <form method="post">
                    <div class="mb-3">
                      <label class="form-label">Username</label>
                      <input class="form-control" name="username">
                    </div>
                    <div class="mb-3">
                      <label class="form-label">Password</label>
                      <input class="form-control" name="password" type="password">
                    </div>
                    <button class="btn btn-primary w-100" type="submit">Login</button>
                  </form>
                  <p class="mt-3 mb-1"><a href="{{ url_for('forgot_password') }}">Forgot password?</a></p>
                  <p class="mb-1"><a href="{{ url_for('request_account') }}">Request account</a></p>
                  <p><a href="{{ url_for('index') }}">Home</a></p>
                </div>
              </div>
            </body>
            </html>
            """,
            err=err,
        )

    @app.route("/fast-login")
    def fast_login():
        token = request.args.get("token", "")
        user = access_store.consume_login_token(token)
        if not user:
            abort(403, "Invalid or expired login token")
        session["username"] = user["username"]
        if int(user.get("force_password_change", 0)) == 1:
            return redirect(url_for("change_password"))
        return redirect(url_for("index"))

    @app.route("/logout")
    def logout():
        session.pop("username", None)
        return redirect(url_for("index"))

    @app.route("/change-password", methods=["GET", "POST"])
    def change_password():
        user = require_login()
        if not isinstance(user, dict):
            return user
        msg = ""
        err = ""
        if request.method == "POST":
            p1 = request.form.get("password", "")
            p2 = request.form.get("password2", "")
            if p1 != p2:
                err = "Passwords do not match"
            else:
                ok, txt = access_store.change_password(user["username"], p1)
                if ok:
                    msg = txt
                else:
                    err = txt
        return render_template_string(
            """
            <!doctype html>
            <html lang="en">
            <head>
              <meta charset="utf-8">
              <title>Change password</title>
              <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
              <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            </head>
            <body class="container py-5">
              <h1 class="h4">Change password</h1>
              {% if msg %}<div class="alert alert-success">{{ msg }}</div>{% endif %}
              {% if err %}<div class="alert alert-danger">{{ err }}</div>{% endif %}
              <form method="post">
                <div class="mb-3"><label class="form-label">New password</label><input class="form-control" type="password" name="password"><div class="form-text">Use at least 12 characters, including uppercase, lowercase, digit, and special character.</div></div>
                <div class="mb-3"><label class="form-label">Confirm password</label><input class="form-control" type="password" name="password2"></div>
                <button class="btn btn-primary" type="submit">Update password</button>
              </form>
              <p class="mt-3"><a href="{{ url_for('index') }}">Home</a></p>
            </body>
            </html>
            """,
            msg=msg,
            err=err,
        )

    @app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
        msg = ""
        err = ""
        if request.method == "POST":
            identity = request.form.get("identity", "").strip()
            user = access_store.find_active_user_by_identity(identity)
            if user and user.get("email"):
                token = access_store.create_password_reset_token(user["username"], ttl_minutes=30)
                link = compose_external_url(cfg["base_url"], url_for("reset_password"), {"token": token})
                send_email(
                    cfg,
                    [user["email"]],
                    "Sensor Network Collector: password reset",
                    (
                        f"Hello {user['username']},\n\n"
                        f"Use the following link to reset your password. It expires in 30 minutes:\n{link}\n"
                    ),
                )
            msg = "If the account exists and has an email address, a reset link has been sent."

        return render_template_string(
            """
            <!doctype html>
            <html lang="en">
            <head>
              <meta charset="utf-8">
              <title>Forgot password</title>
              <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
              <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            </head>
            <body class="container py-5">
              <div class="row justify-content-center">
                <div class="col-12 col-md-6 col-lg-4">
                  <h1 class="h3 mb-3">Forgot password</h1>
                  {% if msg %}<div class="alert alert-success">{{ msg }}</div>{% endif %}
                  {% if err %}<div class="alert alert-danger">{{ err }}</div>{% endif %}
                  <form method="post">
                    <div class="mb-3">
                      <label class="form-label">Username or email</label>
                      <input class="form-control" name="identity">
                    </div>
                    <button class="btn btn-primary w-100" type="submit">Send reset link</button>
                  </form>
                  <p class="mt-3"><a href="{{ url_for('login') }}">Back to login</a></p>
                </div>
              </div>
            </body>
            </html>
            """,
            msg=msg,
            err=err,
        )

    @app.route("/reset-password", methods=["GET", "POST"])
    def reset_password():
        token = request.args.get("token", "").strip()
        user = access_store.get_password_reset_user(token)
        if user is None:
            abort(403, "Invalid or expired password reset token")

        msg = ""
        err = ""
        if request.method == "POST":
            p1 = request.form.get("password", "")
            p2 = request.form.get("password2", "")
            strong, strength_msg = validate_password_strength(p1)
            if p1 != p2:
                err = "Passwords do not match"
            elif not strong:
                # Checked before consuming the single-use token.
                err = strength_msg
            else:
                consumed_user = access_store.consume_password_reset_token(token)
                if consumed_user is None:
                    err = "Invalid or expired password reset token"
                else:
                    ok, txt = access_store.change_password(consumed_user["username"], p1)
                    if ok:
                        msg = txt
                    else:
                        err = txt

        return render_template_string(
            """
            <!doctype html>
            <html lang="en">
            <head>
              <meta charset="utf-8">
              <title>Reset password</title>
              <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
              <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            </head>
            <body class="container py-5">
              <div class="row justify-content-center">
                <div class="col-12 col-md-6 col-lg-4">
                  <h1 class="h3 mb-3">Reset password</h1>
                  <p class="text-muted">Account: {{ user.username }}</p>
                  {% if msg %}<div class="alert alert-success">{{ msg }}</div>{% endif %}
                  {% if err %}<div class="alert alert-danger">{{ err }}</div>{% endif %}
                  {% if not msg %}
                  <form method="post">
                    <div class="mb-3">
                      <label class="form-label">New password</label>
                      <input class="form-control" type="password" name="password">
                      <div class="form-text">Use at least 12 characters, including uppercase, lowercase, digit, and special character.</div>
                    </div>
                    <div class="mb-3">
                      <label class="form-label">Confirm password</label>
                      <input class="form-control" type="password" name="password2">
                    </div>
                    <button class="btn btn-primary w-100" type="submit">Reset password</button>
                  </form>
                  {% endif %}
                  <p class="mt-3"><a href="{{ url_for('login') }}">Back to login</a></p>
                </div>
              </div>
            </body>
            </html>
            """,
            user=user,
            msg=msg,
            err=err,
        )

    @app.route("/request-account", methods=["GET", "POST"])
    def request_account():
        msg = ""
        err = ""
        if request.method == "POST":
            email = request.form.get("email", "").strip()
            reason = request.form.get("reason", "")
            ok, text = access_store.create_account_request(email, reason)
            if ok:
                msg = text
                send_email(
                    cfg,
                    [email],
                    "Sensor Network Collector: registration request received",
                    "Hello,\n\nYour registration request has been received and is pending admin approval.",
                )
                admin_emails = access_store.list_admin_emails()
                if admin_emails:
                    send_email(
                        cfg,
                        admin_emails,
                        "Sensor Network Collector: new account request",
                        (
                            "A new account request has been submitted.\n\n"
                            f"Email: {email}\n"
                            f"Reason: {reason.strip() or '-'}\n\n"
                            f"Review it in the admin panel:\n{compose_external_url(cfg['base_url'], url_for('admin'))}\n"
                        ),
                    )
            else:
                err = text

        return render_template_string(
            """
            <!doctype html>
            <html lang="en">
            <head>
              <meta charset="utf-8">
              <title>Request account</title>
              <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
              <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            </head>
            <body class="container py-5">
              <div class="row justify-content-center">
                <div class="col-12 col-md-8 col-lg-6">
                  <h1 class="h3 mb-3">Request account</h1>
                  {% if msg %}<div class="alert alert-success">{{ msg }}</div>{% endif %}
                  {% if err %}<div class="alert alert-danger">{{ err }}</div>{% endif %}
                  <form method="post">
                    <div class="mb-3">
                      <label class="form-label">Email</label>
                      <input class="form-control" name="email" type="email" required>
                    </div>
                    <div class="mb-3">
                      <label class="form-label">Reason</label>
                      <textarea class="form-control" name="reason" rows="4" required></textarea>
                    </div>
                    <button class="btn btn-primary" type="submit">Submit request</button>
                  </form>
                  <p class="mt-3"><a href="{{ url_for('index') }}">Home</a></p>
                </div>
              </div>
            </body>
            </html>
            """,
            msg=msg,
            err=err,
        )

    @app.route("/api/check-username")
    def check_username():
        # Only the onboarding form needs this; without a valid link it would list usernames.
        if access_store.get_account_request_for_token(request.args.get("token", "").strip()) is None:
            abort(403, "Invalid or expired onboarding link")
        username = request.args.get("username", "").strip()
        if not username:
            return jsonify({"available": False, "message": "Username is required"})
        available = not access_store.username_exists(username)
        return jsonify({"available": available, "message": "" if available else "Username already exists"})

    @app.route("/request-account/complete", methods=["GET", "POST"])
    def complete_account_request():
        token = request.args.get("token", "").strip()
        request_row = access_store.get_account_request_for_token(token)
        if request_row is None:
            return render_template_string(
                """
                <!doctype html>
                <html lang="en">
                <head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"/></head>
                <body class="container py-5">
                  <h1 class="h4">Onboarding link invalid</h1>
                  <p>This onboarding link is invalid, expired, or already used.</p>
                  <p><a href="{{ url_for('index') }}">Home</a></p>
                </body></html>
                """
            )
        msg = ""
        err = ""
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")
            if password != confirm_password:
                err = "Passwords do not match"
            else:
                ok, text = access_store.complete_account_request(token, username, password)
                if ok:
                    msg = text
                    session["username"] = username
                    return redirect(url_for("index"))
                err = text
        return render_template_string(
            """
            <!doctype html>
            <html lang="en">
            <head>
              <meta charset="utf-8">
              <title>Complete onboarding</title>
              <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
              <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            </head>
            <body class="container py-5">
              <div class="row justify-content-center">
                <div class="col-12 col-md-8 col-lg-6">
                  <h1 class="h3 mb-3">Complete onboarding</h1>
                  <p class="text-muted">Request for {{ request_row.email }}</p>
                  {% if msg %}<div class="alert alert-success">{{ msg }}</div>{% endif %}
                  {% if err %}<div class="alert alert-danger">{{ err }}</div>{% endif %}
                  <form method="post" id="onboardingForm" novalidate>
                    <div class="mb-3">
                      <label class="form-label">Username</label>
                      <input class="form-control" name="username" id="onboardingUsername" required>
                      <div id="usernameAvailability" class="form-text"></div>
                    </div>
                    <div class="mb-3">
                      <label class="form-label">Password</label>
                      <input class="form-control" name="password" id="onboardingPassword" type="password" required>
                      <div class="form-text">Use at least 12 characters, including uppercase, lowercase, digit, and special character.</div>
                    </div>
                    <div class="mb-3">
                      <label class="form-label">Confirm password</label>
                      <input class="form-control" name="confirm_password" type="password" required>
                    </div>
                    <button class="btn btn-primary" type="submit">Create account</button>
                  </form>
                  <p class="mt-3"><a href="{{ url_for('index') }}">Home</a></p>
                </div>
              </div>
              <script>
                const usernameInput = document.getElementById('onboardingUsername');
                const availability = document.getElementById('usernameAvailability');
                let availabilityTimer = null;
                let lastAvailability = null;
                async function checkUsername(showAlert = false) {
                  const username = (usernameInput.value || '').trim();
                  if (!username) {
                    availability.textContent = '';
                    lastAvailability = null;
                    return;
                  }
                  try {
                    const qs = new URLSearchParams({ username: username, token: {{ onboarding_token | tojson }} });
                    const resp = await fetch(`{{ url_for('check_username') }}?${qs.toString()}`, { cache: 'no-store' });
                    if (!resp.ok) return;
                    const payload = await resp.json();
                    availability.textContent = payload.message || 'Username available';
                    availability.className = payload.available ? 'form-text text-success' : 'form-text text-danger';
                    if (!payload.available && showAlert && lastAvailability !== false) {
                      window.alert(payload.message || 'Username already exists');
                    }
                    lastAvailability = payload.available;
                  } catch (_) {}
                }
                usernameInput.addEventListener('input', () => {
                  if (availabilityTimer) clearTimeout(availabilityTimer);
                  availabilityTimer = setTimeout(() => checkUsername(false), 300);
                });
                usernameInput.addEventListener('blur', () => checkUsername(true));
              </script>
            </body>
            </html>
            """,
            request_row=request_row,
            onboarding_token=token,
            msg=msg,
            err=err,
        )

    @app.route("/download", methods=["POST"])
    def download():
        user = current_user()
        storage_root = storage_root_or_404()
        all_instruments = set(available_instruments(storage_root))

        requested = []
        for inst in request.form.getlist("instrument"):
            inst = str(inst).strip()
            if inst and inst not in requested:
                requested.append(inst)

        if not requested:
            abort(400, "Select at least one instrument")

        unknown = [inst for inst in requested if inst not in all_instruments]
        if unknown:
            abort(404, f"Unknown station(s): {', '.join(unknown)}")

        unauthorized = [inst for inst in requested if not station_is_accessible(user, inst)]
        if unauthorized:
            abort(403, f"Access denied for station(s): {', '.join(unauthorized)}")

        from_date = parse_date_ymd(request.form.get("from_date", ""))
        to_date = parse_date_ymd(request.form.get("to_date", ""))
        if from_date and to_date and to_date < from_date:
            abort(400, "to_date must be >= from_date")

        zip_path, count = make_zip_for_download(storage_root, requested, from_date=from_date, to_date=to_date)
        if count == 0:
            zip_path.unlink(missing_ok=True)
            abort(404, "No data files found for selected filters")

        response = send_file(
            zip_path,
            as_attachment=True,
            download_name=f"collector_data_{datetime.now().strftime('%Y%m%dT%H%M%S')}.zip",
            mimetype="application/zip",
        )
        # send_file() already holds the archive open, so on POSIX it can be removed
        # now and the space is released once the response has been streamed.
        try:
            zip_path.unlink(missing_ok=True)
        except OSError:
            pass
        return response

    @app.route("/admin")
    def admin():
        admin_user = require_admin()
        if not isinstance(admin_user, dict):
            return admin_user

        storage_root = cfg.get("storage_root")
        instruments = collect_instruments(storage_root) if storage_root else []
        policies = access_store.list_policies(instruments)
        users = access_store.list_users()
        pending_requests = access_store.list_account_requests(status="pending")
        all_access = access_store.list_all_user_instruments()
        all_controls = access_store.list_all_user_station_controls()

        for u in users:
            u["access"] = set(all_access.get(u["username"], []))
            u["controls"] = set(all_controls.get(u["username"], []))
        regular_users = [u for u in users if u["role"] != "admin"]

        stations = []
        for inst in instruments:
            preview = get_station_preview(storage_root, inst)
            stations.append(
                {
                    "uuid": inst,
                    "name": preview["name"],
                    "last_timestamp": preview["last_timestamp"],
                    "policy": policies.get(inst, "account"),
                    "access_count": sum(1 for u in regular_users if inst in u["access"]),
                    "control_count": sum(1 for u in regular_users if inst in u["controls"]),
                }
            )

        return render_template_string(
            """
            <!doctype html>
            <html lang="en">
            <head>
              <meta charset="utf-8">
              <title>Administration</title>
              <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
              <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
              <style>
                .uuid { font-family: var(--bs-font-monospace); font-size: .8rem; }
                .manage-row > td { background: var(--bs-light); }
                .rights-table td, .rights-table th { vertical-align: middle; }
                .flash-text { overflow-wrap: anywhere; }
              </style>
            </head>
            <body class="bg-light">
            <div class="container py-4">
              <div class="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-3">
                <div>
                  <h1 class="h3 mb-0">Administration</h1>
                  <div class="text-muted small">Signed in as <b>{{ admin_user.username }}</b></div>
                </div>
                <div class="d-flex flex-wrap gap-2">
                  <a class="btn btn-outline-secondary btn-sm" href="{{ url_for('index') }}">Home</a>
                  <a class="btn btn-outline-secondary btn-sm" href="{{ url_for('anomalies_log') }}">Anomalies log</a>
                  <a class="btn btn-outline-primary btn-sm" href="{{ url_for('admin_dashboard') }}">Network dashboard</a>
                </div>
              </div>

              {% for category, message in get_flashed_messages(with_categories=true) %}
                <div class="alert alert-{{ category }} flash-text" role="alert">{{ message }}</div>
              {% endfor %}

              <div class="row g-2 mb-3">
                <div class="col-4"><div class="card shadow-sm"><div class="card-body py-2">
                  <div class="text-muted small">Users</div>
                  <div class="h4 mb-0">{{ users|length }}</div>
                </div></div></div>
                <div class="col-4"><div class="card shadow-sm"><div class="card-body py-2">
                  <div class="text-muted small">Stations</div>
                  <div class="h4 mb-0">{{ stations|length }}</div>
                </div></div></div>
                <div class="col-4"><div class="card shadow-sm {% if pending_requests %}border-warning{% endif %}"><div class="card-body py-2">
                  <div class="text-muted small">Pending requests</div>
                  <div class="h4 mb-0">{{ pending_requests|length }}</div>
                </div></div></div>
              </div>

              <ul class="nav nav-tabs" id="adminTabs" role="tablist">
                <li class="nav-item" role="presentation">
                  <button class="nav-link active" data-bs-toggle="tab" data-bs-target="#users" type="button" role="tab">Users</button>
                </li>
                <li class="nav-item" role="presentation">
                  <button class="nav-link" data-bs-toggle="tab" data-bs-target="#stations" type="button" role="tab">Stations</button>
                </li>
                <li class="nav-item" role="presentation">
                  <button class="nav-link" data-bs-toggle="tab" data-bs-target="#requests" type="button" role="tab">
                    Account requests
                    {% if pending_requests %}<span class="badge text-bg-warning">{{ pending_requests|length }}</span>{% endif %}
                  </button>
                </li>
              </ul>

              <div class="tab-content bg-white border border-top-0 rounded-bottom p-3 shadow-sm">

                {# ------------------------------ Users ------------------------------ #}
                <div class="tab-pane fade show active" id="users" role="tabpanel">
                  <div class="d-flex flex-wrap gap-2 mb-3">
                    <input id="userSearch" class="form-control" style="max-width: 20rem;" type="search" placeholder="Search by name or email" aria-label="Search users">
                    <button class="btn btn-primary ms-auto" type="button" data-bs-toggle="collapse" data-bs-target="#newUser">New user</button>
                  </div>

                  <div class="collapse mb-3" id="newUser">
                    <form method="post" action="{{ url_for('admin_create_user') }}" class="card card-body">
                      <h2 class="h6">New user</h2>
                      <div class="row g-3">
                        <div class="col-md-6 col-lg-3">
                          <label class="form-label" for="newUsername">Username</label>
                          <input class="form-control" id="newUsername" name="username" required autocomplete="off">
                        </div>
                        <div class="col-md-6 col-lg-3">
                          <label class="form-label" for="newEmail">Email <span class="text-muted">(optional)</span></label>
                          <input class="form-control" id="newEmail" name="email" type="email" autocomplete="off">
                          <div class="form-text">Used for the welcome link, alarms and password reset.</div>
                        </div>
                        <div class="col-md-6 col-lg-3">
                          <label class="form-label" for="newPassword">Password</label>
                          <input class="form-control" id="newPassword" name="password" type="password" required minlength="12" autocomplete="new-password">
                          <div class="form-text">At least 12 characters with upper and lower case, a digit and a symbol.</div>
                        </div>
                        <div class="col-md-6 col-lg-3">
                          <label class="form-label" for="newRole">Role</label>
                          <select class="form-select" id="newRole" name="role">
                            <option value="user">User</option>
                            <option value="admin">Administrator</option>
                          </select>
                        </div>
                      </div>
                      <div class="form-check mt-3">
                        <input class="form-check-input" type="checkbox" id="newForce" name="force_password_change" value="1" checked>
                        <label class="form-check-label" for="newForce">Ask for a new password at the first login</label>
                      </div>
                      <div class="mt-3"><button class="btn btn-primary" type="submit">Create user</button></div>
                    </form>
                  </div>

                  <div class="table-responsive">
                  <table class="table align-middle mb-0" id="userTable">
                    <thead><tr><th>User</th><th>Role</th><th>Status</th><th>Station rights</th><th></th></tr></thead>
                    <tbody>
                    {% for u in users %}
                      <tr class="user-row" data-search="{{ (u.username ~ ' ' ~ (u.email or ''))|lower }}">
                        <td>
                          <div class="fw-semibold">{{ u.username }}{% if u.username == admin_user.username %} <span class="text-muted fw-normal">(you)</span>{% endif %}</div>
                          <div class="small text-muted">{{ u.email or 'no email' }}</div>
                        </td>
                        <td>
                          {% if u.role == 'admin' %}<span class="badge text-bg-primary">Administrator</span>
                          {% else %}<span class="badge text-bg-secondary">User</span>{% endif %}
                        </td>
                        <td>
                          {% if u.active %}<span class="badge text-bg-success">Active</span>
                          {% else %}<span class="badge text-bg-danger">Disabled</span>{% endif %}
                          {% if u.force_password_change %}<span class="badge text-bg-warning">Must change password</span>{% endif %}
                        </td>
                        <td class="small">
                          {% if u.role == 'admin' %}All stations
                          {% else %}
                            Data on {{ u.access|length }} restricted · Charts on {{ u.controls|length }}
                          {% endif %}
                        </td>
                        <td class="text-end">
                          <button class="btn btn-outline-primary btn-sm" type="button" data-bs-toggle="collapse" data-bs-target="#user-{{ loop.index }}">Manage</button>
                        </td>
                      </tr>
                      <tr class="manage-row user-row" data-search="{{ (u.username ~ ' ' ~ (u.email or ''))|lower }}">
                        <td colspan="5" class="p-0 border-0">
                          <div class="collapse" id="user-{{ loop.index }}">
                            <div class="row g-3 p-3">
                              <div class="col-lg-7">
                                <h3 class="h6">Station rights</h3>
                                {% if u.role == 'admin' %}
                                  <p class="text-muted mb-0">Administrators can read, download and configure every station.</p>
                                {% elif not stations %}
                                  <p class="text-muted mb-0">No stations found in storage.</p>
                                {% else %}
                                  <form method="post" action="{{ url_for('admin_set_user_permissions') }}">
                                    <input type="hidden" name="username" value="{{ u.username }}">
                                    <table class="table table-sm rights-table bg-white border">
                                      <thead><tr>
                                        <th>Station</th>
                                        <th class="text-center" title="Browse and download the station data">Data access</th>
                                        <th class="text-center" title="Edit the trend-chart axis settings of the public dashboard">Chart settings</th>
                                      </tr></thead>
                                      <tbody>
                                      {% for st in stations %}
                                        <tr>
                                          <td>
                                            <input type="hidden" name="station" value="{{ st.uuid }}">
                                            {{ st.name }}
                                            {% if st.name != st.uuid %}<div class="uuid text-muted">{{ st.uuid }}</div>{% endif %}
                                          </td>
                                          <td class="text-center">
                                            {% if st.policy == 'restricted' %}
                                              <input class="form-check-input" type="checkbox" name="access" value="{{ st.uuid }}" aria-label="Data access to {{ st.name }}" {% if st.uuid in u.access %}checked{% endif %}>
                                            {% else %}
                                              {% if st.uuid in u.access %}<input type="hidden" name="access" value="{{ st.uuid }}">{% endif %}
                                              <span class="badge text-bg-light border" title="The station policy already lets this user in">Yes, by policy</span>
                                            {% endif %}
                                          </td>
                                          <td class="text-center">
                                            <input class="form-check-input" type="checkbox" name="control" value="{{ st.uuid }}" aria-label="Chart settings of {{ st.name }}" {% if st.uuid in u.controls %}checked{% endif %}>
                                          </td>
                                        </tr>
                                      {% endfor %}
                                      </tbody>
                                    </table>
                                    <button class="btn btn-primary btn-sm" type="submit">Save station rights</button>
                                    <span class="form-text ms-2">Data access is chosen here only for stations with the Restricted policy.</span>
                                  </form>
                                {% endif %}
                              </div>
                              <div class="col-lg-5">
                                <h3 class="h6">Account</h3>
                                <form method="post" action="{{ url_for('admin_update_user') }}" class="input-group input-group-sm mb-2">
                                  <input type="hidden" name="username" value="{{ u.username }}">
                                  <input type="hidden" name="action" value="email">
                                  <span class="input-group-text">Email</span>
                                  <input class="form-control" type="email" name="email" value="{{ u.email or '' }}" aria-label="Email of {{ u.username }}">
                                  <button class="btn btn-outline-primary" type="submit">Save</button>
                                </form>
                                <div class="d-flex flex-wrap gap-2">
                                  <form method="post" action="{{ url_for('admin_force_password') }}">
                                    <input type="hidden" name="username" value="{{ u.username }}">
                                    {% if u.force_password_change %}
                                      <input type="hidden" name="force" value="0">
                                      <button class="btn btn-outline-secondary btn-sm" type="submit">Stop asking for a new password</button>
                                    {% else %}
                                      <button class="btn btn-outline-warning btn-sm" type="submit">Ask for a new password</button>
                                    {% endif %}
                                  </form>
                                  {% if u.username != admin_user.username %}
                                    <form method="post" action="{{ url_for('admin_update_user') }}">
                                      <input type="hidden" name="username" value="{{ u.username }}">
                                      {% if u.role == 'admin' %}
                                        <input type="hidden" name="action" value="make_user">
                                        <button class="btn btn-outline-secondary btn-sm" type="submit">Make regular user</button>
                                      {% else %}
                                        <input type="hidden" name="action" value="make_admin">
                                        <button class="btn btn-outline-secondary btn-sm" type="submit" data-confirm="Give {{ u.username }} full administrator rights?">Make administrator</button>
                                      {% endif %}
                                    </form>
                                    <form method="post" action="{{ url_for('admin_update_user') }}">
                                      <input type="hidden" name="username" value="{{ u.username }}">
                                      {% if u.active %}
                                        <input type="hidden" name="action" value="deactivate">
                                        <button class="btn btn-outline-danger btn-sm" type="submit" data-confirm="Disable {{ u.username }}? They will not be able to log in.">Disable account</button>
                                      {% else %}
                                        <input type="hidden" name="action" value="activate">
                                        <button class="btn btn-outline-success btn-sm" type="submit">Enable account</button>
                                      {% endif %}
                                    </form>
                                  {% endif %}
                                </div>
                                <div class="form-text mt-2">Created {{ u.created_at }}</div>
                              </div>
                            </div>
                          </div>
                        </td>
                      </tr>
                    {% endfor %}
                    </tbody>
                  </table>
                  </div>
                  <p id="userSearchEmpty" class="text-muted mt-3 mb-0 d-none">No user matches the search.</p>
                </div>

                {# ------------------------------ Stations ------------------------------ #}
                <div class="tab-pane fade" id="stations" role="tabpanel">
                  <div class="alert alert-light border small">
                    <b>Who can browse and download a station's data:</b>
                    <span class="badge text-bg-success">Open</span> everyone, without login ·
                    <span class="badge text-bg-primary">Account</span> every logged-in user ·
                    <span class="badge text-bg-dark">Restricted</span> only the users you select; the station is also hidden from everyone else.
                  </div>
                  {% if not stations %}
                    <p class="text-muted mb-0">No stations found in storage.</p>
                  {% else %}
                  <div class="table-responsive">
                  <table class="table align-middle mb-0">
                    <thead><tr><th>Station</th><th>Last data</th><th>Policy</th><th>Users</th><th></th></tr></thead>
                    <tbody>
                    {% for st in stations %}
                      <tr>
                        <td>
                          <div class="fw-semibold">{{ st.name }}</div>
                          {% if st.name != st.uuid %}<div class="uuid text-muted">{{ st.uuid }}</div>{% endif %}
                        </td>
                        <td class="small">{{ st.last_timestamp or '-' }}</td>
                        <td>
                          <form method="post" action="{{ url_for('admin_set_policy') }}" class="d-flex gap-1">
                            <input type="hidden" name="instrument_uuid" value="{{ st.uuid }}">
                            <select class="form-select form-select-sm policy-select" name="policy" style="min-width: 8.5rem;" aria-label="Policy of {{ st.name }}">
                              <option value="open" {% if st.policy=='open' %}selected{% endif %}>Open</option>
                              <option value="account" {% if st.policy=='account' %}selected{% endif %}>Account</option>
                              <option value="restricted" {% if st.policy=='restricted' %}selected{% endif %}>Restricted</option>
                            </select>
                            <button class="btn btn-outline-primary btn-sm policy-save" type="submit">Save</button>
                          </form>
                        </td>
                        <td class="small">
                          {% if st.policy == 'restricted' %}{{ st.access_count }} with data access{% else %}All by policy{% endif %}
                          · {{ st.control_count }} chart editor{{ '' if st.control_count == 1 else 's' }}
                        </td>
                        <td class="text-end text-nowrap">
                          <a class="btn btn-outline-secondary btn-sm" href="{{ url_for('public_station', instrument_uuid=st.uuid) }}">Dashboard</a>
                          <a class="btn btn-outline-secondary btn-sm" href="{{ url_for('browse_station', instrument_uuid=st.uuid) }}">Data</a>
                          <a class="btn btn-outline-secondary btn-sm" href="{{ url_for('station_chart_settings', instrument_uuid=st.uuid) }}">Chart settings</a>
                          <button class="btn btn-outline-primary btn-sm" type="button" data-bs-toggle="collapse" data-bs-target="#station-{{ loop.index }}">Users</button>
                        </td>
                      </tr>
                      <tr class="manage-row">
                        <td colspan="5" class="p-0 border-0">
                          <div class="collapse" id="station-{{ loop.index }}">
                            <div class="p-3">
                              <h3 class="h6">Users of {{ st.name }}</h3>
                              {% if not regular_users %}
                                <p class="text-muted mb-0">There are no regular users yet. Administrators already have every right.</p>
                              {% else %}
                                <form method="post" action="{{ url_for('admin_set_station_permissions') }}" style="max-width: 44rem;">
                                  <input type="hidden" name="station_uuid" value="{{ st.uuid }}">
                                  <table class="table table-sm rights-table bg-white border">
                                    <thead><tr>
                                      <th>User</th>
                                      <th class="text-center">Data access</th>
                                      <th class="text-center">Chart settings</th>
                                    </tr></thead>
                                    <tbody>
                                    {% for u in regular_users %}
                                      <tr>
                                        <td>
                                          <input type="hidden" name="user" value="{{ u.username }}">
                                          {{ u.username }}
                                          {% if not u.active %}<span class="badge text-bg-danger">Disabled</span>{% endif %}
                                          <div class="small text-muted">{{ u.email or 'no email' }}</div>
                                        </td>
                                        <td class="text-center">
                                          {% if st.policy == 'restricted' %}
                                            <input class="form-check-input" type="checkbox" name="access" value="{{ u.username }}" aria-label="Data access for {{ u.username }}" {% if st.uuid in u.access %}checked{% endif %}>
                                          {% else %}
                                            {% if st.uuid in u.access %}<input type="hidden" name="access" value="{{ u.username }}">{% endif %}
                                            <span class="badge text-bg-light border">Yes, by policy</span>
                                          {% endif %}
                                        </td>
                                        <td class="text-center">
                                          <input class="form-check-input" type="checkbox" name="control" value="{{ u.username }}" aria-label="Chart settings for {{ u.username }}" {% if st.uuid in u.controls %}checked{% endif %}>
                                        </td>
                                      </tr>
                                    {% endfor %}
                                    </tbody>
                                  </table>
                                  <button class="btn btn-primary btn-sm" type="submit">Save users</button>
                                  {% if st.policy != 'restricted' %}
                                    <span class="form-text ms-2">Set the policy to Restricted to choose who has data access.</span>
                                  {% endif %}
                                </form>
                              {% endif %}
                            </div>
                          </div>
                        </td>
                      </tr>
                    {% endfor %}
                    </tbody>
                  </table>
                  </div>
                  {% endif %}
                </div>

                {# ------------------------------ Requests ------------------------------ #}
                <div class="tab-pane fade" id="requests" role="tabpanel">
                  {% if not smtp_ready %}
                    <div class="alert alert-warning small">Email is not configured: after approving a request, the onboarding link is shown here for you to pass on.</div>
                  {% endif %}
                  {% if pending_requests %}
                    {% for r in pending_requests %}
                      <div class="card mb-2"><div class="card-body d-flex flex-wrap justify-content-between align-items-center gap-2">
                        <div>
                          <div class="fw-semibold">{{ r.email }}</div>
                          <div class="small">{{ r.message or 'No reason given.' }}</div>
                          <div class="small text-muted">Request #{{ r.id }} · {{ r.created_at }}</div>
                        </div>
                        <div class="d-flex gap-2">
                          <form method="post" action="{{ url_for('admin_approve_request', request_id=r.id) }}">
                            <button class="btn btn-success btn-sm" type="submit">Approve</button>
                          </form>
                          <form method="post" action="{{ url_for('admin_reject_request', request_id=r.id) }}">
                            <button class="btn btn-outline-danger btn-sm" type="submit" data-confirm="Reject the request from {{ r.email }}?">Reject</button>
                          </form>
                        </div>
                      </div></div>
                    {% endfor %}
                  {% else %}
                    <p class="text-muted mb-0">No pending requests.</p>
                  {% endif %}
                </div>
              </div>
            </div>

            <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
            <script>
              (function () {
                // Open the tab named in the URL (actions come back to the tab they started from).
                const showTab = (hash) => {
                  const trigger = document.querySelector(`#adminTabs [data-bs-target="${hash}"]`);
                  if (trigger && window.bootstrap) bootstrap.Tab.getOrCreateInstance(trigger).show();
                };
                const showTabFromUrl = () => {
                  if (['#users', '#stations', '#requests'].includes(window.location.hash)) showTab(window.location.hash);
                };
                showTabFromUrl();
                window.addEventListener('hashchange', showTabFromUrl);
                document.querySelectorAll('#adminTabs [data-bs-toggle="tab"]').forEach((trigger) => {
                  trigger.addEventListener('shown.bs.tab', () => {
                    window.history.replaceState({}, '', trigger.dataset.bsTarget);
                  });
                });

                document.querySelectorAll('[data-confirm]').forEach((button) => {
                  button.addEventListener('click', (event) => {
                    if (!window.confirm(button.dataset.confirm)) event.preventDefault();
                  });
                });

                // A changed policy is saved at once; the Save button remains for browsers without scripts.
                document.querySelectorAll('.policy-select').forEach((select) => {
                  select.form.querySelector('.policy-save').classList.add('d-none');
                  select.addEventListener('change', () => select.form.requestSubmit());
                });

                const search = document.getElementById('userSearch');
                const empty = document.getElementById('userSearchEmpty');
                search.addEventListener('input', () => {
                  const term = search.value.trim().toLowerCase();
                  let shown = 0;
                  document.querySelectorAll('#userTable .user-row').forEach((row) => {
                    const match = !term || row.dataset.search.includes(term);
                    row.classList.toggle('d-none', !match);
                    if (match) shown += 1;
                  });
                  empty.classList.toggle('d-none', shown > 0);
                });
              })();
            </script>
            </body></html>
            """,
            admin_user=admin_user,
            stations=stations,
            users=users,
            regular_users=regular_users,
            pending_requests=pending_requests,
            smtp_ready=bool(cfg.get("smtp_enabled") and cfg.get("smtp_host")),
        )

    @app.route("/admin/dashboard")
    def admin_dashboard():
        admin_user = require_admin()
        if not isinstance(admin_user, dict):
            return admin_user

        return render_template_string(
            """
            <!doctype html>
            <html lang="en">
            <head>
              <meta charset="utf-8">
              <title>Sensor Network Dashboard</title>
              <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
              <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
              <style>
                .table-wrap { max-height: 78vh; overflow: auto; border: 1px solid #ddd; }
                table { border-collapse: collapse; width: 100%; font-size: 12px; }
                th, td { border: 1px solid #ddd; padding: 4px; vertical-align: top; }
                th { position: sticky; top: 0; background: #f8f8f8; z-index: 2; }
                .status-ok { color: #157347; font-weight: 700; }
                .status-alarm { color: #bb2d3b; font-weight: 700; }
                .missing-cell { background: #ffe7e7; color: #8b0000; font-weight: 600; }
                .group-cell { font-weight: 700; background: #f9fafb; }
              </style>
            </head>
            <body class="container-fluid py-3">
              <p><a href="{{ url_for('admin') }}">Admin panel</a> | <a href="{{ url_for('index') }}">Home</a></p>
              <h1>Sensor Network Dashboard</h1>
              <p class="text-muted mb-1">Admin only page. Auto-refresh every 10 seconds.</p>
              <p id="updatedAt">Updated at: -</p>
              <div class="table-wrap">
                <table>
                  <thead><tr id="headRow"></tr></thead>
                  <tbody id="bodyRows"></tbody>
                </table>
              </div>
              <script>
                const endpoint = {{ url_for('admin_dashboard_api') | tojson }};
                const headRow = document.getElementById('headRow');
                const bodyRows = document.getElementById('bodyRows');
                const updatedAt = document.getElementById('updatedAt');

                const GROUP_ORDER = ['Atmosphere', 'Wind', 'Rain', 'Air Quality', 'Position', 'System', 'Other'];

                function classifyField(field) {
                  const f = String(field || '').toLowerCase();
                  if (f.includes('aqi') || f.startsWith('pm') || f.includes('partic') || f.includes('airlink')) return 'Air Quality';
                  if (f.includes('wind')) return 'Wind';
                  if (f.includes('rain') || f.includes('storm') || f.includes('et')) return 'Rain';
                  if (f.includes('lat') || f.includes('lon') || f.includes('lng') || f.includes('position')) return 'Position';
                  if (
                    f.includes('temp') || f.includes('hum') || f.includes('bar') || f.includes('press') ||
                    f.includes('dew') || f.includes('wet_bulb') || f.includes('heat')
                  ) return 'Atmosphere';
                  if (f.includes('battery') || f.includes('volt') || f.includes('status') || f.includes('signal')) return 'System';
                  return 'Other';
                }

                function buildGroupedEntries(st) {
                  const values = st.values || {};
                  const missing = new Set(st.missingFields || []);
                  const keys = new Set(Object.keys(values));
                  missing.forEach((k) => keys.add(k));

                  const entries = Array.from(keys).map((k) => {
                    const isMissing = missing.has(k);
                    return {
                      group: classifyField(k),
                      field: k,
                      value: isMissing ? 'MISSING' : (values[k] == null ? '' : String(values[k])),
                      missing: isMissing
                    };
                  });

                  entries.sort((a, b) => {
                    const ga = GROUP_ORDER.indexOf(a.group);
                    const gb = GROUP_ORDER.indexOf(b.group);
                    const ia = ga === -1 ? GROUP_ORDER.length : ga;
                    const ib = gb === -1 ? GROUP_ORDER.length : gb;
                    if (ia !== ib) return ia - ib;
                    return a.field.localeCompare(b.field);
                  });

                  const grouped = new Map();
                  entries.forEach((entry) => {
                    if (!grouped.has(entry.group)) {
                      grouped.set(entry.group, []);
                    }
                    grouped.get(entry.group).push(entry);
                  });

                  const out = Array.from(grouped.entries()).map(([group, items]) => {
                    items.sort((a, b) => a.field.localeCompare(b.field));
                    return { group, items };
                  });

                  out.sort((a, b) => {
                    const ga = GROUP_ORDER.indexOf(a.group);
                    const gb = GROUP_ORDER.indexOf(b.group);
                    const ia = ga === -1 ? GROUP_ORDER.length : ga;
                    const ib = gb === -1 ? GROUP_ORDER.length : gb;
                    return ia - ib;
                  });

                  return out;
                }

                function render(payload) {
                  headRow.innerHTML = '';
                  ['Station', 'UUID', 'Timestamp', 'Age(s)', 'Usual update(s)', 'Fail threshold(s)', 'Status', 'Alarms', 'Missing values', 'Battery', 'Group', 'Data'].forEach((h) => {
                    const th = document.createElement('th');
                    th.textContent = h;
                    headRow.appendChild(th);
                  });

                  bodyRows.innerHTML = '';
                  (payload.stations || []).forEach((st) => {
                    const entries = buildGroupedEntries(st);
                    if (entries.length === 0) {
                      entries.push({ group: 'Other', items: [{ field: '-', value: '-', missing: false }] });
                    }

                    entries.forEach((entry, idx) => {
                      const tr = document.createElement('tr');

                      if (idx === 0) {
                        const fixed = [
                          st.name || st.uuid,
                          st.uuid || '',
                          st.lastTimestamp || '',
                          st.ageSeconds == null ? '' : st.ageSeconds,
                          st.usualUpdateSeconds == null ? '' : st.usualUpdateSeconds,
                          st.failureThresholdSeconds == null ? '' : st.failureThresholdSeconds,
                          st.status || '',
                          (st.alarms || []).join(', '),
                          (st.missingFields && st.missingFields.length) ? st.missingFields.join(', ') : '',
                          st.batteryInfo || ''
                        ];
                        fixed.forEach((value, colIdx) => {
                          const td = document.createElement('td');
                          td.textContent = String(value);
                          td.rowSpan = entries.length;
                          if (colIdx === 6) {
                            td.className = (String(value) === 'OK') ? 'status-ok' : 'status-alarm';
                          }
                          tr.appendChild(td);
                        });
                      }

                      const g = document.createElement('td');
                      g.textContent = entry.group;
                      g.className = 'group-cell';
                      tr.appendChild(g);

                      const v = document.createElement('td');
                      entry.items.forEach((item, itemIdx) => {
                        if (itemIdx > 0) v.appendChild(document.createTextNode(' | '));
                        if (item.missing) {
                          const span = document.createElement('span');
                          span.className = 'missing-cell px-1';
                          span.textContent = `${item.field}=MISSING`;
                          v.appendChild(span);
                        } else {
                          v.appendChild(document.createTextNode(`${item.field}=${item.value}`));
                        }
                      });
                      tr.appendChild(v);

                      bodyRows.appendChild(tr);
                    });
                  });

                  updatedAt.textContent = 'Updated at: ' + (payload.updatedAt || '-');
                }

                async function refresh() {
                  try {
                    const resp = await fetch(endpoint, { cache: 'no-store' });
                    if (!resp.ok) return;
                    const payload = await resp.json();
                    render(payload);
                  } catch (_) {
                    // keep polling on transient errors
                  }
                }

                refresh();
                setInterval(refresh, 10000);
              </script>
            </body>
            </html>
            """
        )

    @app.route("/api/admin/dashboard")
    def admin_dashboard_api():
        admin_user = require_admin()
        if not isinstance(admin_user, dict):
            return admin_user
        storage_root = storage_root_or_404()
        return jsonify(build_admin_network_dashboard(storage_root))

    @app.route("/anomalies")
    def anomalies_log():
        user = require_login()
        if not isinstance(user, dict):
            return user
        items = access_store.list_anomalies_for_user(user)
        return render_template_string(
            """
            <!doctype html>
            <html lang="en">
            <head>
              <meta charset="utf-8">
              <title>Anomalies log</title>
              <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
              <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            </head>
            <body class="container py-4">
              <p><a href="{{ url_for('index') }}">Home</a></p>
              <h1 class="h4">Anomalies log</h1>
              <table class="table table-sm table-bordered table-striped">
                <thead>
                  <tr><th>Station</th><th>Type</th><th>Status</th><th>Message</th><th>Updated</th><th>Action</th></tr>
                </thead>
                <tbody>
                {% for a in items %}
                  <tr>
                    <td>{{ a.station_uuid }}</td>
                    <td>{{ a.anomaly_type }}</td>
                    <td>{{ a.status }}</td>
                    <td>{{ a.message }}</td>
                    <td>{{ a.updated_at }}</td>
                    <td>
                      {% if a.status == 'open' %}
                      <form method="post" action="{{ url_for('silence_anomaly') }}" class="d-flex gap-1">
                        <input type="hidden" name="station_uuid" value="{{ a.station_uuid }}">
                        <input type="hidden" name="anomaly_type" value="{{ a.anomaly_type }}">
                        <input class="form-control form-control-sm" style="max-width:110px" type="number" min="1" max="24" name="hours" value="24">
                        <button class="btn btn-warning btn-sm" type="submit">Silence</button>
                      </form>
                      {% endif %}
                    </td>
                  </tr>
                {% endfor %}
                </tbody>
              </table>
            </body>
            </html>
            """,
            items=items,
        )

    @app.route("/anomalies/silence", methods=["POST"])
    def silence_anomaly():
        user = require_login()
        if not isinstance(user, dict):
            return user
        station_uuid = request.form.get("station_uuid", "").strip()
        anomaly_type = request.form.get("anomaly_type", "").strip()
        try:
            hours = int(request.form.get("hours", "24") or 24)
        except ValueError:
            abort(400, "hours must be an integer")
        if not station_uuid or not anomaly_type:
            abort(400, "Missing station_uuid or anomaly_type")
        if user.get("role") != "admin" and not station_is_accessible(user, station_uuid):
            abort(403)
        access_store.set_anomaly_silence(station_uuid, anomaly_type, user["username"], hours)
        return redirect(url_for("anomalies_log"))

    @app.route("/station/<path:instrument_uuid>/chart-settings", methods=["GET", "POST"])
    def station_chart_settings(instrument_uuid: str):
        user = require_login()
        if not isinstance(user, dict):
            return user

        storage_root = storage_root_or_404()
        instruments = set(available_instruments(storage_root))
        if instrument_uuid not in instruments:
            abort(404, "Station not found")
        if not station_is_controllable(user, instrument_uuid):
            abort(403)

        msg = ""
        err = ""
        if request.method == "POST":
            raw_settings = {}
            for spec in get_chart_setting_catalog():
                raw_settings[spec["key"]] = {
                    "y_min": request.form.get(f"y_min__{spec['key']}", ""),
                    "y_max": request.form.get(f"y_max__{spec['key']}", ""),
                    "y_step": request.form.get(f"y_step__{spec['key']}", ""),
                }
            try:
                normalized = normalize_station_chart_settings_map(raw_settings)
                access_store.replace_station_chart_settings(instrument_uuid, normalized, user["username"])
                msg = "Trend chart settings saved"
            except ValueError as e:
                err = str(e)

        preview = get_station_preview(storage_root, instrument_uuid)
        snapshot = build_public_station_snapshot(
            storage_root,
            instrument_uuid,
            window="hour",
            max_points=120,
            cfg=cfg,
            access_store=access_store,
        )
        effective_series = {item["key"]: item for item in snapshot.get("series", [])}
        chart_specs = resolve_station_chart_specs(access_store, instrument_uuid)
        return render_template_string(
            """
            <!doctype html>
            <html lang="en">
            <head>
              <meta charset="utf-8">
              <title>Trend chart axis settings - {{ station_name }}</title>
              <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
              <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
            </head>
            <body class="container py-4">
              <p><a href="{{ url_for('index') }}">Home</a> | <a href="{{ url_for('public_station', instrument_uuid=instrument_uuid) }}">Public dashboard</a> | <a href="{{ url_for('browse_station', instrument_uuid=instrument_uuid) }}">Browse station</a></p>
              <h1 class="h4">Trend chart axis settings</h1>
              <p class="text-muted">{{ station_name }} ({{ instrument_uuid }})</p>
              {% if msg %}<div class="alert alert-success">{{ msg }}</div>{% endif %}
              {% if err %}<div class="alert alert-danger">{{ err }}</div>{% endif %}

              <div class="card mb-3"><div class="card-body">
                <div class="d-flex flex-wrap gap-2 align-items-center">
                  <a class="btn btn-outline-primary btn-sm" href="{{ url_for('station_chart_settings_export', instrument_uuid=instrument_uuid) }}">Export JSON</a>
                  <form id="chartSettingsImportForm" method="post" action="{{ url_for('station_chart_settings_import', instrument_uuid=instrument_uuid) }}" enctype="multipart/form-data" class="d-flex gap-2 align-items-center">
                    <input id="chartSettingsImportFile" class="form-control form-control-sm" type="file" name="settings_file" accept="application/json,.json" required>
                    <input type="hidden" name="confirm_foreign_station" id="confirmForeignStation" value="0">
                    <button class="btn btn-outline-secondary btn-sm" type="submit">Import JSON</button>
                  </form>
                </div>
                <p class="text-muted small mt-2 mb-0">Leave a field empty to keep automatic axis sizing for that chart. Saved values are stored in the auth database and immediately applied to the public station dashboard. If the imported JSON belongs to another station, the UI asks for confirmation before applying it here.</p>
              </div></div>

              <form method="post">
                <div class="table-responsive">
                  <table class="table table-sm table-bordered align-middle">
                    <thead>
                      <tr>
                        <th>Chart</th>
                        <th>Unit</th>
                        <th>Saved y_min</th>
                        <th>Saved y_max</th>
                        <th>Saved step</th>
                        <th>Effective range</th>
                        <th>Updated</th>
                      </tr>
                    </thead>
                    <tbody>
                      {% for spec in chart_specs %}
                        {% set effective = effective_series.get(spec.key, {}) %}
                        <tr>
                          <td><b>{{ spec.label }}</b><br><span class="text-muted small">{{ spec.key }}</span></td>
                          <td>{{ spec.unit or '-' }}</td>
                          <td><input class="form-control form-control-sm" type="number" step="any" name="y_min__{{ spec.key }}" value="{{ '' if spec.saved.y_min is none else spec.saved.y_min }}"></td>
                          <td><input class="form-control form-control-sm" type="number" step="any" name="y_max__{{ spec.key }}" value="{{ '' if spec.saved.y_max is none else spec.saved.y_max }}"></td>
                          <td><input class="form-control form-control-sm" type="number" step="any" min="0.000001" name="y_step__{{ spec.key }}" value="{{ '' if spec.saved.y_step is none else spec.saved.y_step }}"></td>
                          <td>
                            {% if effective %}
                              {{ effective.y_min }} .. {{ effective.y_max }}
                              {% if effective.y_step is not none %}<br><span class="text-muted small">step={{ effective.y_step }}</span>{% endif %}
                            {% else %}
                              <span class="text-muted">No recent data</span>
                            {% endif %}
                          </td>
                          <td>
                            {% if spec.saved.updated_at %}
                              {{ spec.saved.updated_at }}<br><span class="text-muted small">{{ spec.saved.updated_by }}</span>
                            {% else %}
                              <span class="text-muted">default</span>
                            {% endif %}
                          </td>
                        </tr>
                      {% endfor %}
                    </tbody>
                  </table>
                </div>
                <button class="btn btn-primary" type="submit">Save settings</button>
              </form>
              <script>
                const importForm = document.getElementById('chartSettingsImportForm');
                const importFile = document.getElementById('chartSettingsImportFile');
                const confirmForeignStation = document.getElementById('confirmForeignStation');
                const stationUuid = {{ instrument_uuid | tojson }};
                if (importForm && importFile && confirmForeignStation) {
                  importForm.dataset.sourceStationUuid = '';
                  importForm.dataset.fileChecked = '0';

                  importFile.addEventListener('change', async () => {
                    confirmForeignStation.value = '0';
                    const file = importFile.files && importFile.files[0];
                    importForm.dataset.sourceStationUuid = '';
                    importForm.dataset.fileChecked = file ? '0' : '1';
                    if (!file) return;
                    try {
                      const text = await file.text();
                      const payload = JSON.parse(text);
                      const sourceStationUuid = String((payload && payload.station_uuid) || '').trim();
                      importForm.dataset.sourceStationUuid = sourceStationUuid;
                    } catch (_) {
                      importForm.dataset.sourceStationUuid = '';
                    } finally {
                      importForm.dataset.fileChecked = '1';
                    }
                  });

                  importForm.addEventListener('submit', (event) => {
                    const file = importFile.files && importFile.files[0];
                    if (!file) return;
                    if (importForm.dataset.fileChecked !== '1') {
                      event.preventDefault();
                      window.alert('Please wait for the JSON file to be checked, then submit again.');
                      return;
                    }
                    const sourceStationUuid = String(importForm.dataset.sourceStationUuid || '').trim();
                    if (sourceStationUuid && sourceStationUuid !== stationUuid) {
                      const ok = window.confirm(`This configuration was exported from station "${sourceStationUuid}" and will be imported into "${stationUuid}". Continue?`);
                      if (!ok) {
                        event.preventDefault();
                        return;
                      }
                      confirmForeignStation.value = '1';
                    }
                  });
                }
              </script>
            </body>
            </html>
            """,
            instrument_uuid=instrument_uuid,
            station_name=preview.get("name") or instrument_uuid,
            chart_specs=chart_specs,
            effective_series=effective_series,
            msg=msg,
            err=err,
        )

    @app.route("/station/<path:instrument_uuid>/chart-settings/export")
    def station_chart_settings_export(instrument_uuid: str):
        user = require_login()
        if not isinstance(user, dict):
            return user
        storage_root = storage_root_or_404()
        instruments = set(available_instruments(storage_root))
        if instrument_uuid not in instruments:
            abort(404, "Station not found")
        if not station_is_controllable(user, instrument_uuid):
            abort(403)

        payload = export_station_chart_settings_payload(access_store, instrument_uuid)
        response = app.response_class(
            response=json.dumps(payload, indent=2, sort_keys=True),
            status=200,
            mimetype="application/json",
        )
        response.headers["Content-Disposition"] = f"attachment; filename={safe_filename(instrument_uuid)}_chart_settings.json"
        return response

    @app.route("/station/<path:instrument_uuid>/chart-settings/import", methods=["POST"])
    def station_chart_settings_import(instrument_uuid: str):
        user = require_login()
        if not isinstance(user, dict):
            return user
        storage_root = storage_root_or_404()
        instruments = set(available_instruments(storage_root))
        if instrument_uuid not in instruments:
            abort(404, "Station not found")
        if not station_is_controllable(user, instrument_uuid):
            abort(403)

        upload = request.files.get("settings_file")
        if upload is None or not upload.filename:
            abort(400, "Missing JSON file")
        try:
            payload = json.load(upload.stream)
            if not isinstance(payload, dict):
                raise ValueError("Invalid JSON payload")
            payload_station_uuid = str(payload.get("station_uuid") or "").strip()
            if payload_station_uuid and payload_station_uuid != instrument_uuid:
                if not parse_boolish(request.form.get("confirm_foreign_station", "0"), False):
                    abort(
                        400,
                        (
                            f"JSON file belongs to station {payload_station_uuid}. "
                            "Confirm import from another station in the web form and retry."
                        ),
                    )
            normalized = parse_station_chart_settings_payload(payload)
            access_store.replace_station_chart_settings(instrument_uuid, normalized, user["username"])
        except ValueError as e:
            abort(400, str(e))
        except json.JSONDecodeError:
            abort(400, "Invalid JSON file")
        return redirect(url_for("station_chart_settings", instrument_uuid=instrument_uuid))

    def admin_done(tab: str, ok: bool, message: str):
        """Report the outcome of an admin action on the tab it was started from."""
        flash(message, "success" if ok else "danger")
        return redirect(url_for("admin") + f"#{tab}")

    @app.route("/admin/create-user", methods=["POST"])
    def admin_create_user():
        admin_user = require_admin()
        if not isinstance(admin_user, dict):
            return admin_user

        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "user")

        if email and not is_valid_email(email):
            return admin_done("users", False, "Email address is not valid")
        ok, msg = access_store.create_user(username, password, email, role=role)
        if not ok:
            return admin_done("users", False, msg)
        if parse_boolish(request.form.get("force_password_change"), False):
            access_store.set_force_password_change(username, True)
        msg = f"User {username} created"
        if email:
            token = access_store.create_login_token(username, ttl_minutes=60)
            link = compose_external_url(cfg["base_url"], url_for("fast_login"), {"token": token})
            sent = send_email(
                cfg,
                [email],
                "Welcome to Sensor Network Collector",
                f"Hello {username},\n\nYour account has been created.\nFast login link (expires in 60 minutes):\n{link}\n",
            )
            msg += "; a welcome email with a login link was sent" if sent else "; no welcome email was sent"
        return admin_done("users", True, msg)

    @app.route("/admin/requests/<int:request_id>/approve", methods=["POST"])
    def admin_approve_request(request_id: int):
        admin_user = require_admin()
        if not isinstance(admin_user, dict):
            return admin_user
        ok, msg = access_store.approve_request(request_id, admin_user["username"])
        if not ok:
            return admin_done("requests", False, msg)
        req = access_store.get_account_request(request_id)
        if req and req.get("email"):
            token = access_store.create_account_request_token(request_id, ttl_hours=48)
            link = compose_external_url(cfg["base_url"], url_for("complete_account_request"), {"token": token})
            sent = send_email(
                cfg,
                [req["email"]],
                "Sensor Network Collector: account approved",
                (
                    "Hello,\n\n"
                    "Your account request has been approved.\n"
                    "Use the link below to complete onboarding and choose your username and password.\n"
                    "This link expires in 48 hours.\n\n"
                    f"{link}\n"
                ),
            )
            if sent:
                msg = f"Request approved; the onboarding link was emailed to {req['email']}"
            else:
                # Without email the link would be lost: hand it to the admin instead.
                msg = (
                    f"Request approved, but no email could be sent. Give this onboarding link "
                    f"(valid 48 hours) to {req['email']}: {link}"
                )
        return admin_done("requests", True, msg)

    @app.route("/admin/requests/<int:request_id>/reject", methods=["POST"])
    def admin_reject_request(request_id: int):
        admin_user = require_admin()
        if not isinstance(admin_user, dict):
            return admin_user
        ok, msg = access_store.reject_request(request_id, admin_user["username"])
        return admin_done("requests", ok, msg)

    @app.route("/admin/policy", methods=["POST"])
    def admin_set_policy():
        admin_user = require_admin()
        if not isinstance(admin_user, dict):
            return admin_user

        instrument_uuid = request.form.get("instrument_uuid", "")
        policy = request.form.get("policy", "")
        ok, msg = access_store.set_policy(instrument_uuid, policy, admin_user["username"])
        if ok:
            msg = f"Policy of {instrument_uuid.strip()} set to {policy}"
        return admin_done("stations", ok, msg)

    @app.route("/admin/user-access", methods=["POST"])
    def admin_set_user_access():
        admin_user = require_admin()
        if not isinstance(admin_user, dict):
            return admin_user

        username = request.form.get("username", "")
        instrument_uuid = request.form.get("instrument_uuid", "")
        allow = parse_boolish(request.form.get("allow", "1"), True)

        ok, msg = access_store.set_user_instrument_access(username, instrument_uuid, allow)
        return admin_done("users", ok, msg)

    @app.route("/admin/user-control", methods=["POST"])
    def admin_set_user_control():
        admin_user = require_admin()
        if not isinstance(admin_user, dict):
            return admin_user

        username = request.form.get("username", "")
        station_uuid = request.form.get("station_uuid", "")
        allow = parse_boolish(request.form.get("allow", "1"), True)

        ok, msg = access_store.set_user_station_control(username, station_uuid, allow)
        return admin_done("users", ok, msg)

    @app.route("/admin/user-permissions", methods=["POST"])
    def admin_set_user_permissions():
        admin_user = require_admin()
        if not isinstance(admin_user, dict):
            return admin_user
        ok, msg = access_store.replace_user_permissions(
            request.form.get("username", ""),
            request.form.getlist("station"),
            request.form.getlist("access"),
            request.form.getlist("control"),
        )
        return admin_done("users", ok, msg)

    @app.route("/admin/station-permissions", methods=["POST"])
    def admin_set_station_permissions():
        admin_user = require_admin()
        if not isinstance(admin_user, dict):
            return admin_user
        ok, msg = access_store.replace_station_permissions(
            request.form.get("station_uuid", ""),
            request.form.getlist("user"),
            request.form.getlist("access"),
            request.form.getlist("control"),
        )
        return admin_done("stations", ok, msg)

    @app.route("/admin/user-update", methods=["POST"])
    def admin_update_user():
        admin_user = require_admin()
        if not isinstance(admin_user, dict):
            return admin_user
        username = request.form.get("username", "").strip()
        action = request.form.get("action", "")
        changes = {
            "activate": {"active": True},
            "deactivate": {"active": False},
            "make_admin": {"role": "admin"},
            "make_user": {"role": "user"},
            "email": {"email": request.form.get("email", "")},
        }.get(action)
        if changes is None:
            return admin_done("users", False, "Unknown action")
        if username == admin_user["username"] and action in ("deactivate", "make_user"):
            return admin_done("users", False, "You cannot disable or demote your own account")
        ok, msg = access_store.update_user(username, **changes)
        return admin_done("users", ok, msg)

    @app.route("/admin/force-password", methods=["POST"])
    def admin_force_password():
        admin_user = require_admin()
        if not isinstance(admin_user, dict):
            return admin_user
        username = request.form.get("username", "").strip()
        if not username:
            return admin_done("users", False, "Username is required")
        force = parse_boolish(request.form.get("force", "1"), True)
        ok, msg = access_store.set_force_password_change(username, force)
        if ok:
            msg = (
                f"{username} must choose a new password at the next login"
                if force
                else f"{username} is no longer asked to change password"
            )
        return admin_done("users", ok, msg)

    return app


# ----------------------------
# Runtime state
# ----------------------------
runtime = {
    "config": {},
    "influx_client": None,
    "access_store": None,
    "watchdog_stop_event": None,
}


def init_influx_runtime(cfg: dict):
    runtime["config"] = cfg
    if not cfg.get("enable_influx"):
        return None

    influx_client = runtime.get("influx_client")
    if influx_client is not None:
        return influx_client

    influx_client = InfluxDBClient(
        url=cfg["influxdb_url"],
        token=cfg["influxdb_token"],
        org=cfg["influxdb_org"],
    )
    runtime["influx_client"] = influx_client
    return influx_client


def open_access_store(cfg: dict) -> AccessStore:
    access_store = AccessStore(cfg["auth_db_path"])
    access_store.ensure_admin(cfg["admin_user"], cfg["admin_password"])
    return access_store


# ----------------------------
# Graceful shutdown
# ----------------------------
def shutdown(signum, frame):
    logger.info("Shutting down (signal=%s)...", signum)

    stop_event = runtime.get("watchdog_stop_event")
    if stop_event is not None:
        stop_event.set()

    influx_client = runtime.get("influx_client")
    if influx_client is not None:
        try:
            influx_client.close()
        except Exception:
            pass

    sys.exit(0)


def main():
    args = parse_args()
    cfg = load_config(args.config)

    apply_log_level(cfg)
    init_influx_runtime(cfg)

    try:
        access_store = open_access_store(cfg)
    except (RuntimeError, OSError, sqlite3.Error) as e:
        logger.error("Cannot open the auth database: %s", e)
        sys.exit(1)
    runtime["access_store"] = access_store

    stop_event = threading.Event()
    runtime["watchdog_stop_event"] = stop_event
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    if args.watchdog_only:
        run_watchdog_loop(cfg, access_store, stop_event)
        return

    app = create_web_app(cfg, access_store)
    threading.Thread(
        target=run_watchdog_loop,
        args=(cfg, access_store, stop_event),
        name="pwa-watchdog",
        daemon=True,
    ).start()

    logger.info("Web application listening on http://%s:%s", cfg["http_host"], cfg["http_port"])
    app.run(host=cfg["http_host"], port=cfg["http_port"], debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
