#!/usr/bin/env bash
# Oracle-1001 / Sentinel — AIS ingest healthcheck (idempotent)
# (a) stop ais-ingest if running
# (b) PRAGMA wal_checkpoint(TRUNCATE)
# (c) PRAGMA integrity_check
# (d) if not ok → backup + recreate empty schema from services.storage DDL
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_HOME="${APP_HOME:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
DB_PATH="${SENTINEL_DB_PATH:-${APP_HOME}/история1/sentinel_ais.db}"
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "${PYTHON_BIN}" ]]; then
  if [[ -x "${APP_HOME}/venv/Scripts/python.exe" ]]; then
    PYTHON_BIN="${APP_HOME}/venv/Scripts/python.exe"
  elif [[ -x "${APP_HOME}/venv/bin/python" ]]; then
    PYTHON_BIN="${APP_HOME}/venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  else
    PYTHON_BIN="$(command -v python)"
  fi
fi

ts() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
log() { echo "[$(ts)] ais_ingest_healthcheck: $*"; }

# ── (a) Stop ingest processes (best-effort; idempotent) ───────────────────────
_stop_ingest() {
  log "stopping ais-ingest processes (if any)"
  # Docker / Linux
  pkill -TERM -f "run_sentinel_core|aisstream_connector" 2>/dev/null || true
  # Windows Git-Bash / MSYS: taskkill via Python (pkill may be absent)
  "${PYTHON_BIN}" - <<'PY' || true
import os, signal, sys, time
try:
    import psutil  # type: ignore
except Exception:
    psutil = None

patterns = ("run_sentinel_core", "aisstream_connector", "services.aisstream_connector")
killed = []
if psutil is not None:
    me = os.getpid()
    for p in psutil.process_iter(["pid", "cmdline", "name"]):
        try:
            if p.info["pid"] == me:
                continue
            cmd = " ".join(p.info.get("cmdline") or [])
            if any(x in cmd for x in patterns):
                p.terminate()
                killed.append(p.info["pid"])
        except Exception:
            continue
    deadline = time.time() + 8
    for pid in killed:
        try:
            proc = psutil.Process(pid)
            while proc.is_running() and time.time() < deadline:
                time.sleep(0.2)
            if proc.is_running():
                proc.kill()
        except Exception:
            pass
else:
    # Fallback: only signal ourselves if we are the ingest PID1 child (compose)
    pass
print(f"stopped_pids={killed}")
PY
  sleep 1 || true
}

_stop_ingest

# ── (b)+(c)+(d) WAL checkpoint + integrity + optional rebuild ─────────────────
export DB_PATH APP_HOME
log "WAL TRUNCATE + integrity_check on ${DB_PATH}"
RESULT="$("${PYTHON_BIN}" - <<'PY'
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

db_path = Path(os.environ["DB_PATH"])
app_home = Path(os.environ["APP_HOME"])
sys.path.insert(0, str(app_home))

def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

def ensure_parent(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)

def recreate_schema(path: Path) -> None:
    """Idempotent empty schema from services.storage (+ TTF DDL if available)."""
    ensure_parent(path)
    if path.exists():
        path.unlink()
    for suf in ("-wal", "-shm"):
        side = Path(str(path) + suf)
        if side.exists():
            side.unlink()
    from services.storage import AISStorage
    store = AISStorage(sqlite_path=path)
    store.open()
    if store._conn is not None:
        store._conn.close()
        store._conn = None

if not db_path.exists():
    print("DB_MISSING — creating empty schema")
    recreate_schema(db_path)
    print("STATUS=OK ACTION=created_empty")
    raise SystemExit(0)

# Soft open with timeout; TRUNCATE checkpoint
conn = sqlite3.connect(str(db_path), timeout=60.0)
try:
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        print(f"WAL_CHECKPOINT={row}")
    except sqlite3.Error as exc:
        print(f"WAL_CHECKPOINT_ERR={exc}")
    integrity = conn.execute("PRAGMA integrity_check").fetchone()
    ok = bool(integrity and str(integrity[0]).lower() == "ok")
    print(f"INTEGRITY={integrity[0] if integrity else None}")
finally:
    conn.close()

if ok:
    print("STATUS=OK ACTION=none")
    raise SystemExit(0)

# Corrupt → backup + recreate
backup = db_path.with_name(f"{db_path.name}.corrupt-{utc_stamp()}")
print(f"INTEGRITY_FAIL — backing up to {backup}")
shutil.copy2(db_path, backup)
for suf in ("-wal", "-shm"):
    side = Path(str(db_path) + suf)
    if side.exists():
        shutil.copy2(side, Path(str(backup) + suf))
        try:
            side.unlink()
        except OSError:
            pass
recreate_schema(db_path)
# Verify new DB
conn = sqlite3.connect(str(db_path), timeout=30.0)
try:
    chk = conn.execute("PRAGMA integrity_check").fetchone()
    print(f"RECREATED_INTEGRITY={chk[0] if chk else None}")
    if not chk or str(chk[0]).lower() != "ok":
        print("STATUS=FAIL ACTION=recreate_failed")
        print(
            "FATAL: empty schema recreate failed. Restore from backup manually:",
            backup,
            file=sys.stderr,
        )
        raise SystemExit(2)
finally:
    conn.close()
print(f"STATUS=RECOVERED ACTION=recreated backup={backup}")
PY
)" || {
  log "Python healthcheck failed"
  echo "${RESULT:-}"
  exit 2
}

echo "${RESULT}"
if echo "${RESULT}" | grep -q 'STATUS=FAIL'; then
  log "FAIL — see message above; restore from backup required"
  exit 2
fi

log "OK — DB healthy (idempotent)"
exit 0
