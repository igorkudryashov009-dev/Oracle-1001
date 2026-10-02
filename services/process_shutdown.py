"""SIGTERM path: checkpoint SQLite WAL, then exit. Idempotent."""
from __future__ import annotations

import signal
import sqlite3
import threading
from pathlib import Path

_DONE = False
_LOCK = threading.Lock()

ROOT = Path(__file__).resolve().parents[1]
_DBS = (
    ROOT / "история1" / "sentinel_ais.db",
    Path("/app/история1/sentinel_ais.db"),
)


def checkpoint_sqlite_wal() -> None:
    for path in _DBS:
        if not path.is_file():
            continue
        try:
            conn = sqlite3.connect(str(path), timeout=3.0)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
        except sqlite3.Error:
            continue


def note_shutdown() -> None:
    global _DONE
    with _LOCK:
        if _DONE:
            return
        _DONE = True
    checkpoint_sqlite_wal()
    print("graceful shutdown complete", flush=True)


def install_sigterm(server=None) -> None:
    def _handler(signum, frame):  # noqa: ARG001
        def _run() -> None:
            note_shutdown()
            if server is not None:
                server.shutdown()

        threading.Thread(target=_run, daemon=True).start()

    signal.signal(signal.SIGTERM, _handler)
