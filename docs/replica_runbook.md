# Read-only replica runbook

SQLite WAL is the current replica foundation. Timescale replica reading is only after the migration runbook.

## WAL

These opens already set `PRAGMA journal_mode=WAL`:

- `services/storage.py`
- `services/archive_service.py`
- `services/sqlite_wal.py` `ensure_wal()`, called from the 04:00 `sqlite_maintenance` job

WAL allows one writer and concurrent readers. It does not, by itself, copy the database to another host.

## SQLite replica (current topology)

1. Keep the archive database in WAL mode (the maintenance job re-asserts it).
2. On the writer, run `PRAGMA wal_checkpoint(PASSIVE)` before copying, which the ingest path already does.
3. Copy `sentinel_ais.db`, `sentinel_ais.db-wal`, and `sentinel_ais.db-shm` together, or use `sqlite3 .backup` so the replica file is consistent.
4. The second node opens that file read-only (`mode=ro`). It must not run ingest or `PRAGMA optimize`.
5. London already ships a one-way snapshot of the AIS database to Korolev. That copy remains the hot-standby path. This runbook does not add a second writer.

## After Timescale

When `db_backend.active` is `timescale`, a read replica is a Postgres streaming replica. Application reads use the replica DSN. Writes stay on the primary. Until that cutover, `active_backend()` remains `sqlite`.
