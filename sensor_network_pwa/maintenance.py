"""Operator tools for consistent SQLite backups and non-destructive restore drills."""

import argparse
import sqlite3
import tempfile
from pathlib import Path

from sensor_network_pwa.config import load_config


def backup_database(source: Path, destination: Path) -> None:
    if destination.exists():
        raise RuntimeError(f"Refusing to overwrite existing backup: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
    verify_database(destination)


def verify_database(path: Path) -> None:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as con:
        result = con.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise RuntimeError(f"SQLite integrity check failed for {path}")
        required = {"users", "app_settings", "instrument_policies"}
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = required - tables
        if missing:
            raise RuntimeError(f"Backup is missing required tables: {', '.join(sorted(missing))}")


def restore_drill(source: Path) -> None:
    """Backup and reopen a disposable restore without touching the live database."""
    with tempfile.TemporaryDirectory(prefix="sensor-pwa-restore-drill-") as directory:
        backup = Path(directory) / "backup.sqlite"
        restored = Path(directory) / "restored.sqlite"
        backup_database(source, backup)
        with sqlite3.connect(backup) as src, sqlite3.connect(restored) as dst:
            src.backup(dst)
        verify_database(restored)
        with sqlite3.connect(source) as live, sqlite3.connect(restored) as copy:
            live_users = live.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            copy_users = copy.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            if live_users != copy_users:
                raise RuntimeError("Restore drill user count does not match the live database")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup", help="Create and verify an online SQLite backup")
    backup.add_argument("output", type=Path)
    subparsers.add_parser("drill", help="Perform a non-destructive backup/restore drill")
    args = parser.parse_args()
    source = Path(load_config(args.config)["auth_db_path"])
    if args.command == "backup":
        backup_database(source, args.output)
        print(f"Verified backup written to {args.output}")
    else:
        restore_drill(source)
        print("Backup/restore drill passed")


if __name__ == "__main__":
    main()
