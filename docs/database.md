# Application database

The portal uses SQLAlchemy for accounts, permissions, tokens, workflows, notification state,
and audit data. SQLite is convenient for development and small single-host installations.
Use PostgreSQL (or another SQLAlchemy-supported client/server database after testing its
dialect) in production, especially with several Gunicorn workers or application hosts.

## Configuration

Set `databaseUrl` in `config.json`, or preferably provide `DATABASE_URL` through the process
environment so credentials are not committed:

```text
postgresql+psycopg://sensor_network:password@database:5432/sensor_network
```

`databaseUrl` takes precedence over `authDbPath`. If it is absent, `authDbPath` is converted
to a SQLite SQLAlchemy URL, preserving existing deployments. PostgreSQL is the supported and
documented production server. MariaDB/MySQL and other SQLAlchemy dialects may work, but are
not covered by CI and should be validated with the complete test suite before deployment.

## Migrate SQLite to PostgreSQL

1. Back up and verify SQLite with `python -m sensor_network_pwa.maintenance backup backup.sqlite`.
2. Stop all web and watchdog processes so the source cannot change during copying.
3. Create an empty PostgreSQL database and account. Do not point the command at a database
   containing application data; it deliberately refuses a non-empty destination.
4. Run:

   ```bash
   python -m sensor_network_pwa.db_migrate \
     --sqlite /data/collector_auth.sqlite \
     --database-url 'postgresql+psycopg://sensor_network:password@localhost/sensor_network'
   ```

5. Set `DATABASE_URL` to the same URL, start one web process, and check `/health` and an admin
   login. Then start the remaining web workers and the single watchdog process.
6. Keep the SQLite file and its backup read-only until retention policy permits removal.

The migration creates the current schema, copies all shared tables in batches, updates
PostgreSQL identity sequences, and compares source and destination row counts in one target
transaction. A failure rolls back copied rows. To retry after a failure outside that
transaction (for example, schema creation), recreate an empty target database first.

## Backups

The `sensor_network_pwa.maintenance` command is SQLite-specific. For PostgreSQL, use managed
snapshots plus regular `pg_dump` backups and test `pg_restore` into an isolated database.
Never place database passwords in `config.json` committed to source control.
