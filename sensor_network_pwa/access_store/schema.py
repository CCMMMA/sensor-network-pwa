"""Tables of the auth database and the column additions applied to databases created by older versions."""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    email TEXT,
    role TEXT NOT NULL DEFAULT 'user',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    force_password_change INTEGER NOT NULL DEFAULT 0,
    can_api INTEGER NOT NULL DEFAULT 0,
    can_workflows INTEGER NOT NULL DEFAULT 0
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
    can_browse INTEGER NOT NULL DEFAULT 1,
    can_download INTEGER NOT NULL DEFAULT 1,
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

CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    token_prefix TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_used_at TEXT,
    expires_at TEXT
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

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    anomaly_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    last_sent_at TEXT,
    recovery_sent_at TEXT,
    acknowledged_at TEXT,
    snoozed_until TEXT,
    cleared_at TEXT,
    send_claimed_at TEXT,
    recovery_claimed_at TEXT,
    UNIQUE (username, anomaly_id)
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

CREATE TABLE IF NOT EXISTS workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    graph_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_run_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS workflow_state (
    workflow_id INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 0,
    last_fired_at TEXT,
    PRIMARY KEY (workflow_id, node_id)
);

CREATE TABLE IF NOT EXISTS write_probe (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rate_limits (
    key_hash TEXT PRIMARY KEY,
    window_started_at TEXT NOT NULL,
    attempts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate_limits_window ON rate_limits(window_started_at);

CREATE TABLE IF NOT EXISTS impersonation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_username TEXT NOT NULL,
    target_username TEXT NOT NULL,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS access_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT,
    event_type TEXT NOT NULL,
    station_uuid TEXT,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_access_events_created_at ON access_events(created_at);
CREATE INDEX IF NOT EXISTS idx_access_events_username ON access_events(username);
CREATE INDEX IF NOT EXISTS idx_access_events_station ON access_events(station_uuid);

CREATE TABLE IF NOT EXISTS email_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipients_json TEXT NOT NULL,
    subject TEXT NOT NULL,
    body_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    next_attempt_at TEXT NOT NULL,
    locked_at TEXT,
    sent_at TEXT,
    last_error TEXT
);
"""

# Each statement fails with "duplicate column name" once applied, which is how it is skipped.
CAN_WORKFLOWS_MIGRATION = "ALTER TABLE users ADD COLUMN can_workflows INTEGER NOT NULL DEFAULT 0"

MIGRATIONS = (
    "ALTER TABLE users ADD COLUMN force_password_change INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE account_requests ADD COLUMN reviewed_at TEXT",
    # NULL means DEFAULT_NOTIFICATION_INTERVAL_MIN.
    "ALTER TABLE users ADD COLUMN notify_interval_min INTEGER",
    # Existing combined assignments retain both permissions; new assignments
    # explicitly set each flag.
    "ALTER TABLE user_instruments ADD COLUMN can_browse INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE user_instruments ADD COLUMN can_download INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE users ADD COLUMN can_api INTEGER NOT NULL DEFAULT 0",
    CAN_WORKFLOWS_MIGRATION,
    # Watchdog processes can briefly overlap during a container restart. These
    # leases make deciding who sends an email atomic across processes.
    "ALTER TABLE notifications ADD COLUMN send_claimed_at TEXT",
    "ALTER TABLE notifications ADD COLUMN recovery_claimed_at TEXT",
)
