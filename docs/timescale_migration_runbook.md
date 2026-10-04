# TimescaleDB migration runbook

Live serving stays on SQLite. This runbook is the cutover, and it is not run by deploy.

## Preconditions

- `DB_BACKEND` in `config.yaml` is `sqlite` (default). `services.db_backend.active_backend()` stays `sqlite` until the cutover marker exists.
- Node A disk free is above the Dual Gate floor (20%).
- A fresh copy of `история1/sentinel_ais.db` is taken with `sqlite3 .backup`.

## Schema mapping

`scripts/migrate_to_timescale.py` maps `vessel_daily_archive` to a hypertable-shaped table:

| SQLite | Timescale | Type |
|---|---|---|
| snapshot_date | snapshot_date | date |
| imo | imo | bigint |
| lat | lat | double precision |
| lon | lon | double precision |
| source | source | text |
| gap_hours | gap_hours | double precision |

Primary key remains `(snapshot_date, imo)`. `source='none'` rows are copied as-is. The script does not invent positions.

## Staging backfill

```bash
python scripts/migrate_to_timescale.py --source path/to/sentinel_ais.db --dest path/to/timescale_stage.db
```

The command prints source and dest row counts and a checksum sample. Run it a second time: `inserted` must be 0 and the counts must still match.

## Cutover

1. Stop `sentinel-core` ingest writes.
2. Load the staged table into Timescale (`CREATE EXTENSION timescaledb`, then `create_hypertable` on the timestamp you choose for the hypertable).
3. Set `DB_BACKEND=timescale` in the environment.
4. Write `data/archive/timescale_cutover.json` with `{"at":"<UTC ISO>"}` only after the counts match.
5. Start the core and confirm `/api/v1/health` (admin key) shows `db_backend.active=timescale`.

## Rollback

1. Delete `data/archive/timescale_cutover.json`.
2. Unset `DB_BACKEND` (config default `sqlite`).
3. Start the core against the SQLite backup.
4. `active_backend()` returns `sqlite` again. Dual Gate thresholds are unchanged.
