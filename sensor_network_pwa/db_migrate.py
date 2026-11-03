"""Copy an existing SQLite application database to a SQLAlchemy database URL."""

import argparse
from pathlib import Path

from sqlalchemy import MetaData, create_engine, func, select, text

from sensor_network_pwa.access_store import AccessStore
from sensor_network_pwa.access_store.base import database_url


def migrate_sqlite(source: Path, target_url: str, batch_size: int = 500) -> dict[str, int]:
    """Create the target schema, copy every table, and verify row counts.

    The destination must be empty. This makes the operation safe to rerun only
    after explicitly recreating the target database and prevents accidental
    merging of credentials or workflow state.
    """
    if not source.is_file():
        raise RuntimeError(f"SQLite source does not exist: {source}")
    target = AccessStore(target_url)
    source_engine = create_engine(database_url(str(source)))
    source_meta = MetaData()
    target_meta = MetaData()
    source_meta.reflect(bind=source_engine)
    target_meta.reflect(bind=target.engine)
    common = [name for name in source_meta.tables if name in target_meta.tables and name != "sqlite_sequence"]

    counts: dict[str, int] = {}
    with target.engine.begin() as destination:
        occupied = {
            name: destination.scalar(select(func.count()).select_from(target_meta.tables[name]))
            for name in common
            if name != "write_probe"
        }
        occupied = {name: count for name, count in occupied.items() if count}
        if occupied:
            names = ", ".join(sorted(occupied))
            raise RuntimeError(f"Target database is not empty (data found in: {names})")
        with source_engine.connect() as origin:
            for name in common:
                source_table = source_meta.tables[name]
                target_table = target_meta.tables[name]
                copied = 0
                result = origin.execute(select(source_table))
                while rows := result.mappings().fetchmany(max(1, batch_size)):
                    destination.execute(target_table.insert(), [dict(row) for row in rows])
                    copied += len(rows)
                counts[name] = copied

        if target.engine.dialect.name == "postgresql":
            for name in common:
                table = target_meta.tables[name]
                if "id" not in table.c:
                    continue
                destination.execute(
                    text(
                        "SELECT setval(pg_get_serial_sequence(:table, 'id'), "
                        "COALESCE((SELECT MAX(id) FROM " + name + "), 1), "
                        "(SELECT COUNT(*) > 0 FROM " + name + "))"
                    ),
                    {"table": name},
                )

        for name, expected in counts.items():
            actual = destination.scalar(select(func.count()).select_from(target_meta.tables[name]))
            if actual != expected:
                raise RuntimeError(f"Verification failed for {name}: copied {actual}, expected {expected}")
    source_engine.dispose()
    target.engine.dispose()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", required=True, type=Path, help="Existing SQLite database file")
    parser.add_argument("--database-url", required=True, help="Empty destination SQLAlchemy URL")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()
    counts = migrate_sqlite(args.sqlite, args.database_url, args.batch_size)
    print(f"Migration verified: {sum(counts.values())} rows across {len(counts)} tables")


if __name__ == "__main__":
    main()
