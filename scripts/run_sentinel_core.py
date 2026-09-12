#!/usr/bin/env python3
"""Sentinel-core supervisor: AIS ingest + scheduled TTF analytical rollups.

Designed for docker-compose service ``sentinel-core``.
Paths come from env (entrypoint): SENTINEL_DB_PATH, ASSETS_7000_DIR, OUTPUT_DIR.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TTF_INTERVAL_SEC = int(os.environ.get("TTF_ROLLUP_INTERVAL_SEC", "21600"))  # 6h
STOP = threading.Event()


def _log(msg: str) -> None:
    print(f"[sentinel-core] {msg}", flush=True)


def _run_ttf_rollup() -> int:
    lean = [
        sys.executable,
        str(ROOT / "run_release.py"),
        "--skip-remote",
        "--skip-heavy",
        "--no-open",
    ]
    _log(f"TTF rollup start (interval={TTF_INTERVAL_SEC}s)")
    try:
        proc = subprocess.run(lean, cwd=str(ROOT), check=False)
        _log(f"TTF rollup exit={proc.returncode}")
        # Prompt 11: append-only health/coverage snapshot every Nth rollup (~daily).
        try:
            snap = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "append_health_snapshot.py")],
                cwd=str(ROOT),
                check=False,
                capture_output=True,
                text=True,
                errors="replace",
            )
            if snap.stdout:
                _log(f"health_snapshot {snap.stdout.strip().splitlines()[0][:200]}")
            if snap.returncode != 0:
                _log(f"health_snapshot warn exit={snap.returncode}")
        except Exception as snap_exc:  # noqa: BLE001
            _log(f"health_snapshot skipped: {snap_exc}")
        return int(proc.returncode)
    except Exception as exc:  # noqa: BLE001
        _log(f"TTF rollup failed: {exc}")
        return 1


def _rollup_loop() -> None:
    settle = int(os.environ.get("TTF_ROLLUP_INITIAL_DELAY_SEC", "90"))
    if settle > 0 and not STOP.wait(settle):
        _run_ttf_rollup()
    while not STOP.wait(TTF_INTERVAL_SEC):
        _run_ttf_rollup()


def _wal_checkpoint(db: str) -> None:
    try:
        import sqlite3

        if not Path(db).is_file():
            return
        conn = sqlite3.connect(db, timeout=30.0)
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        conn.close()
        _log("WAL checkpoint PASSIVE ok")
    except Exception as exc:  # noqa: BLE001
        _log(f"WAL checkpoint skipped: {exc}")


def main() -> int:
    def _handle_sig(*_args: object) -> None:
        _log("signal received — shutting down")
        STOP.set()

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    db = os.environ.get("SENTINEL_DB_PATH", str(ROOT / "история1" / "sentinel_ais.db"))
    assets = os.environ.get("ASSETS_7000_DIR", str(ROOT / "assets" / "7000"))
    _log(f"APP_HOME={os.environ.get('APP_HOME', ROOT)}")
    _log(f"SENTINEL_DB_PATH={db}")
    _log(f"ASSETS_7000_DIR={assets}")
    _log(f"DASHBOARD_PORT={os.environ.get('DASHBOARD_PORT') or os.environ.get('PORT', '8765')}")

    rollup = threading.Thread(target=_rollup_loop, name="ttf-rollup", daemon=True)
    rollup.start()

    from services import aisstream_connector

    argv = ["aisstream_connector.py", "--db", db]
    sys.argv = argv
    code = 0
    try:
        code = aisstream_connector.main()
    except KeyboardInterrupt:
        code = 0
    finally:
        STOP.set()
        _wal_checkpoint(db)
        # Give rollup thread a moment to notice STOP
        time.sleep(0.2)

    return int(code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
