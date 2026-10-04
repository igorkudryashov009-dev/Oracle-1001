"""Enable SQLite WAL. Idempotent and safe to call on every archive open."""

from __future__ import annotations

import sqlite3


def ensure_wal(conn: sqlite3.Connection) -> str:
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return str(mode[0]).lower() if mode else ""
