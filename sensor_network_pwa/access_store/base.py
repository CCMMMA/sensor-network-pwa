"""SQLAlchemy connection handling and compatibility facade used by store mixins."""

import re
import secrets
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import OperationalError

from sensor_network_pwa.access_store.schema import CAN_WORKFLOWS_MIGRATION, MIGRATIONS, SCHEMA_SQL
from sensor_network_pwa.timeutil import now_utc_iso, parse_iso_ts, utc_iso, utc_now

_ID_TABLES = {
    "account_requests",
    "access_events",
    "anomalies",
    "api_tokens",
    "email_queue",
    "impersonation_events",
    "notifications",
    "workflows",
    "write_probe",
}
_INSERT_RE = re.compile(r"^\s*INSERT\s+INTO\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE)


def database_url(value: str) -> str:
    """Accept a SQLAlchemy URL or the historical SQLite filename."""
    if "://" in value:
        return value
    return f"sqlite:///{Path(value).resolve()}"


class CompatRow(Mapping):
    """Keep sqlite3.Row's string and integer lookup behavior for existing mixins."""

    def __init__(self, row):
        self._mapping = dict(row._mapping)
        self._values = tuple(row)

    def __getitem__(self, key):
        return self._values[key] if isinstance(key, int) else self._mapping[key]

    def __iter__(self) -> Iterator:
        return iter(self._mapping)

    def __len__(self) -> int:
        return len(self._mapping)


class CompatResult:
    def __init__(self, result, lastrowid=None):
        self._result = result
        self.lastrowid = lastrowid
        self.rowcount = result.rowcount

    def fetchone(self):
        row = self._result.fetchone()
        return CompatRow(row) if row is not None else None

    def fetchall(self):
        return [CompatRow(row) for row in self._result.fetchall()]

    def __iter__(self):
        return (CompatRow(row) for row in self._result)


class CompatConnection:
    """Translate the store's qmark SQL to SQLAlchemy portable named parameters."""

    def __init__(self, connection: Connection):
        self.connection = connection

    def execute(self, statement: str, parameters=()):
        if statement.strip().upper() == "BEGIN IMMEDIATE":
            # SQLAlchemy already opened the transaction. PostgreSQL obtains row
            # locks through the following conditional updates.
            return CompatResult(self.connection.execute(text("SELECT 1")))
        ignore = bool(re.match(r"^\s*INSERT\s+OR\s+IGNORE", statement, re.IGNORECASE))
        statement = re.sub(r"^\s*INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", statement, flags=re.IGNORECASE)
        params = tuple(parameters or ())
        named = {}
        index = 0

        def replace(_match):
            nonlocal index
            key = f"p{index}"
            named[key] = params[index]
            index += 1
            return f":{key}"

        sql = re.sub(r"\?", replace, statement)
        if index != len(params):
            raise ValueError(f"SQL parameter mismatch: expected {index}, received {len(params)}")
        if ignore and "ON CONFLICT" not in sql.upper():
            sql = f"{sql.rstrip().rstrip(';')} ON CONFLICT DO NOTHING"
        if (
            self.connection.dialect.name == "postgresql"
            and "FROM email_queue" in sql
            and "status='pending'" in sql
            and "FOR UPDATE" not in sql.upper()
        ):
            # Multiple watchdogs may briefly overlap during a rolling restart.
            sql = f"{sql.rstrip().rstrip(';')} FOR UPDATE SKIP LOCKED"

        match = _INSERT_RE.match(sql)
        table = match.group(1).lower() if match else None
        wants_id = table in _ID_TABLES and "RETURNING" not in sql.upper() and "ON CONFLICT" not in sql.upper()
        if wants_id:
            sql = f"{sql.rstrip().rstrip(';')} RETURNING id"
        result = self.connection.execute(text(sql), named)
        # Reading CursorResult.lastrowid on psycopg closes a SELECT cursor.
        # Every generated id used by the store is handled by RETURNING above.
        lastrowid = result.scalar_one_or_none() if wants_id else None
        return CompatResult(result, lastrowid)

    def executemany(self, statement: str, parameter_sets):
        result = None
        for parameters in parameter_sets:
            result = self.execute(statement, parameters)
        return result


class StoreBase:
    """Application database shared by the watchdog and every web worker."""

    def __init__(self, db_url: str):
        self.database_url = database_url(db_url)
        self.db_path = (
            self.database_url.removeprefix("sqlite:///") if self.database_url.startswith("sqlite:///") else db_url
        )
        self._lock = threading.Lock()
        if self.database_url.startswith("sqlite:///"):
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        connect_args = {"timeout": 30} if self.database_url.startswith("sqlite:") else {}
        self.engine: Engine = create_engine(self.database_url, pool_pre_ping=True, connect_args=connect_args)
        self._init_schema()
        self._verify_writable()

    @contextmanager
    def _connect(self):
        with self.engine.begin() as connection:
            yield CompatConnection(connection)

    @contextmanager
    def _readonly_as_runtime_error(self, message: str):
        try:
            yield
        except OperationalError as error:
            if self._is_readonly_error(error):
                raise RuntimeError(message) from error
            raise

    def _readonly_message(self) -> str:
        return f"Application database is read-only: {self.database_url}. Configure a writable databaseUrl."

    def _init_schema(self):
        schema = SCHEMA_SQL
        if self.engine.dialect.name != "sqlite":
            schema = schema.replace(
                "INTEGER PRIMARY KEY AUTOINCREMENT", "INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
            )
        with self._lock, self._readonly_as_runtime_error(self._readonly_message()), self.engine.begin() as connection:
            for statement in schema.split(";"):
                if statement.strip():
                    connection.exec_driver_sql(statement)
            inspector = inspect(connection)
            for statement in MIGRATIONS:
                match = re.match(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", statement, re.IGNORECASE)
                if not match:
                    continue
                table, column = match.groups()
                if column in {item["name"] for item in inspector.get_columns(table)}:
                    continue
                connection.exec_driver_sql(statement)
                if statement == CAN_WORKFLOWS_MIGRATION:
                    connection.exec_driver_sql("UPDATE users SET can_workflows=can_api")

    @staticmethod
    def _is_readonly_error(err: Exception) -> bool:
        msg = str(err).lower()
        return "readonly" in msg or "read-only" in msg or "read only" in msg

    def _verify_writable(self):
        with self._lock, self._readonly_as_runtime_error(self._readonly_message()), self._connect() as con:
            cur = con.execute("INSERT INTO write_probe(created_at) VALUES(?)", (now_utc_iso(),))
            if cur.lastrowid is not None:
                con.execute("DELETE FROM write_probe WHERE id = ?", (cur.lastrowid,))

    def get_or_create_session_secret(self) -> str:
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT INTO app_settings(key,value) VALUES('session_secret',?) ON CONFLICT(key) DO NOTHING",
                (secrets.token_hex(32),),
            )
            row = con.execute("SELECT value FROM app_settings WHERE key = 'session_secret'").fetchone()
        return row["value"]

    def get_app_settings(self, keys: tuple[str, ...]) -> dict[str, str]:
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        with self._connect() as con:
            rows = con.execute(f"SELECT key,value FROM app_settings WHERE key IN ({placeholders})", keys).fetchall()
        return {row["key"]: row["value"] for row in rows}

    def set_app_settings(self, values: dict[str, str]) -> None:
        with self._lock, self._connect() as con:
            for key, value in values.items():
                con.execute(
                    "INSERT INTO app_settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )

    def healthcheck(self) -> bool:
        with self._connect() as con:
            return con.execute("SELECT 1").fetchone()[0] == 1

    def rate_limit_exceeded(self, key_hash: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        now = utc_now()
        with self._lock, self._connect() as con:
            stale_before = now - timedelta(seconds=max(window_seconds, 86400))
            con.execute("DELETE FROM rate_limits WHERE window_started_at < ?", (utc_iso(stale_before),))
            row = con.execute(
                "SELECT window_started_at,attempts FROM rate_limits WHERE key_hash=?", (key_hash,)
            ).fetchone()
            started = parse_iso_ts(row["window_started_at"]) if row else None
            if started is None or now - started >= timedelta(seconds=window_seconds):
                con.execute(
                    "INSERT INTO rate_limits(key_hash,window_started_at,attempts) VALUES(?,?,1) "
                    "ON CONFLICT(key_hash) DO UPDATE SET window_started_at=excluded.window_started_at,attempts=1",
                    (key_hash, now_utc_iso()),
                )
                return False, window_seconds
            attempts = int(row["attempts"]) + 1
            con.execute("UPDATE rate_limits SET attempts=? WHERE key_hash=?", (attempts, key_hash))
            retry_after = max(1, window_seconds - int((now - started).total_seconds()))
            return attempts > limit, retry_after
