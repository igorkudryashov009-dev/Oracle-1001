"""Weekly-safe SQLite maintenance for vessel_daily_archive. Does not delete rows."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any


def _db_path() -> Path:
    from services.storage import DEFAULT_DB

    env = (os.environ.get("SENTINEL_DB_PATH") or "").strip()
    return Path(env) if env else Path(DEFAULT_DB)


def optimize_vessel_archive(db_path: Path | None = None) -> dict[str, Any]:
    path = db_path or _db_path()
    if not path.is_file():
        return {"ok": False, "error": "db_missing", "path_set": True}
    conn = sqlite3.connect(str(path), timeout=60.0)
    try:
        from services.sqlite_wal import ensure_wal

        ensure_wal(conn)
        conn.execute("ANALYZE vessel_daily_archive")
        conn.execute("PRAGMA optimize")
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "pragma": "optimize", "analyze": "vessel_daily_archive"}


def main() -> int:
    import json

    print(json.dumps(optimize_vessel_archive(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
