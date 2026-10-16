"""Connection handling, schema creation and the settings shared by every store mixin."""

import secrets
import sqlite3
import threading
from contextlib import contextmanager, suppress
from pathlib import Path

from sensor_network_pwa.access_store.schema import MIGRATIONS, SCHEMA_SQL
from sensor_network_pwa.timeutil import now_utc_iso


class StoreBase:
    """One SQLite file shared by the watchdog and every web worker; writes are serialised by a lock."""

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

    @contextmanager
    def _readonly_as_runtime_error(self, message: str):
        """Turn SQLite's read-only failure into a RuntimeError that tells the operator what to change."""
        try:
            yield
        except sqlite3.OperationalError as e:
            if self._is_readonly_error(e):
                raise RuntimeError(message) from e
            raise

    def _readonly_message(self) -> str:
        return f"Auth DB is read-only: {self.db_path}. Set authDbPath to a writable location (for example under /data)."

    def _init_schema(self):
        with self._lock, self._readonly_as_runtime_error(self._readonly_message()), self._connect() as con:
            con.executescript(SCHEMA_SQL)
            for statement in MIGRATIONS:
                with suppress(sqlite3.OperationalError):
                    con.execute(statement)

    @staticmethod
    def _is_readonly_error(err: Exception) -> bool:
        msg = str(err).lower()
        return "readonly" in msg or "read-only" in msg

    def _verify_writable(self):
        """Fail fast on startup if the auth DB is not writable."""
        with self._lock, self._readonly_as_runtime_error(self._readonly_message()), self._connect() as con:
            cur = con.execute("INSERT INTO write_probe(created_at) VALUES(?)", (now_utc_iso(),))
            probe_id = cur.lastrowid
            if probe_id is not None:
                con.execute("DELETE FROM write_probe WHERE id = ?", (probe_id,))

    def get_or_create_session_secret(self) -> str:
        """Random session secret shared by every process that opens this database."""
        with self._lock, self._connect() as con:
            con.execute(
                "INSERT OR IGNORE INTO app_settings(key,value) VALUES('session_secret',?)",
                (secrets.token_hex(32),),
            )
            row = con.execute("SELECT value FROM app_settings WHERE key = 'session_secret'").fetchone()
        return row["value"]
