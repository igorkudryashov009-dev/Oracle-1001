"""Retention for append-only diagnostic logs and corrupt SQLite backups.

Docker json-file rotation (20m×5) covers container stdout only — not host
jsonl / corrupt_backup / apt-adjacent growth. Run periodically from
sentinel-core rollup or: python -m services.log_retention
"""

from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

# Append-only G3 / SRE logs
DEFAULT_JSONL_MAX_DAYS = 90
DEFAULT_JSONL_MAX_BYTES = 50 * 1024 * 1024  # 50 MiB soft cap → trim oldest lines

# Recovery leftovers from hard_recover / salvage (can be hundreds of MB each)
DEFAULT_CORRUPT_BACKUP_KEEP = 1  # newest timestamped .db.* only
DEFAULT_CORRUPT_BACKUP_MAX_TOTAL = 200 * 1024 * 1024  # 200 MiB

LOG_CANDIDATES = [
    ROOT / "logs" / "health_coverage_daily.jsonl",
    Path("/opt/oracle1001/logs/health_coverage_daily.jsonl"),
    Path("/opt/oracle1001/sentinel/logs/health_coverage_daily.jsonl"),
    Path("/opt/oracle1001/analytical_engine/logs/health_coverage_daily.jsonl"),
    Path("/app/logs/health_coverage_daily.jsonl"),
]

CORRUPT_BACKUP_DIRS = [
    ROOT / "ais_data" / "corrupt_backup",
    Path("/opt/oracle1001/ais_data/corrupt_backup"),
    Path("/app/история1/corrupt_backup"),
]


def _utc_now() -> float:
    return datetime.now(timezone.utc).timestamp()


def trim_jsonl(
    path: Path,
    *,
    max_days: int = DEFAULT_JSONL_MAX_DAYS,
    max_bytes: int = DEFAULT_JSONL_MAX_BYTES,
) -> dict[str, Any]:
    """Drop lines older than max_days; if still over max_bytes, keep tail."""
    report: dict[str, Any] = {"path": str(path), "action": "skip"}
    if not path.is_file():
        report["action"] = "missing"
        return report

    before = path.stat().st_size
    report["bytes_before"] = before
    cutoff = _utc_now() - max(1, int(max_days)) * 86400.0

    try:
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        report["action"] = "error"
        report["error"] = str(exc)
        return report

    kept: list[str] = []
    dropped_age = 0
    for line in raw:
        s = line.strip()
        if not s:
            continue
        # Prefer ISO ts at start of JSON: "ts_utc":"..."
        ts_ok = True
        idx = s.find('"ts_utc"')
        if idx >= 0:
            try:
                frag = s[idx:].split(":", 1)[1].strip().strip(",").strip('"')
                # 2026-09-13T09:00:00Z
                dt = datetime.fromisoformat(frag.replace("Z", "+00:00"))
                if dt.timestamp() < cutoff:
                    ts_ok = False
            except Exception:
                ts_ok = True
        if ts_ok:
            kept.append(line)
        else:
            dropped_age += 1

    # Size cap: keep newest lines (tail)
    dropped_size = 0
    if max_bytes > 0:
        encoded = [ln.encode("utf-8") for ln in kept]
        total = sum(len(b) + 1 for b in encoded)
        if total > max_bytes:
            new_kept: list[str] = []
            acc = 0
            for ln, b in zip(reversed(kept), reversed(encoded)):
                add = len(b) + 1
                if acc + add > max_bytes and new_kept:
                    dropped_size += 1
                    continue
                new_kept.append(ln)
                acc += add
            kept = list(reversed(new_kept))

    if dropped_age == 0 and dropped_size == 0 and before <= max_bytes:
        report["action"] = "noop"
        report["lines"] = len(kept)
        return report

    tmp = path.with_suffix(path.suffix + ".tmp")
    text = "\n".join(kept) + ("\n" if kept else "")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    after = path.stat().st_size
    report.update(
        {
            "action": "trimmed",
            "lines_kept": len(kept),
            "dropped_age": dropped_age,
            "dropped_size": dropped_size,
            "bytes_after": after,
            "bytes_freed": max(0, before - after),
        }
    )
    return report


def prune_corrupt_backups(
    directory: Path,
    *,
    keep: int = DEFAULT_CORRUPT_BACKUP_KEEP,
    max_total_bytes: int = DEFAULT_CORRUPT_BACKUP_MAX_TOTAL,
) -> dict[str, Any]:
    """Keep newest N timestamped DB dumps; delete older / over-budget files."""
    report: dict[str, Any] = {"path": str(directory), "deleted": [], "kept": [], "bytes_freed": 0}
    if not directory.is_dir():
        report["action"] = "missing"
        return report

    dumps = sorted(
        [p for p in directory.iterdir() if p.is_file() and ".db." in p.name],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    # Always prefer keeping small recovered artifacts; sort by mtime already.
    keep_n = max(0, int(keep))
    survivors = dumps[:keep_n]
    victims = dumps[keep_n:]

    # If survivors still exceed budget, drop largest oldest among survivors first
    def _total(paths: list[Path]) -> int:
        return sum(p.stat().st_size for p in paths if p.exists())

    while survivors and _total(survivors) > max_total_bytes and len(survivors) > 0:
        # Drop the oldest among survivors
        oldest = min(survivors, key=lambda p: p.stat().st_mtime)
        survivors.remove(oldest)
        victims.append(oldest)

    for p in victims:
        try:
            sz = p.stat().st_size
            p.unlink()
            report["deleted"].append(str(p))
            report["bytes_freed"] += sz
        except OSError as exc:
            report.setdefault("errors", []).append(f"{p}:{exc}")

    report["kept"] = [str(p) for p in survivors]
    # Side WAL/SHM left from crashed recovery — safe to remove if no live db named sentinel_ais.db in dir
    for side in directory.glob("sentinel_ais.db-*"):
        if side.name.endswith(("-wal", "-shm")):
            try:
                sz = side.stat().st_size
                side.unlink()
                report["deleted"].append(str(side))
                report["bytes_freed"] += sz
            except OSError:
                pass
    report["action"] = "pruned" if report["deleted"] else "noop"
    return report


def run_retention(
    *,
    max_days: int = DEFAULT_JSONL_MAX_DAYS,
    max_bytes: int = DEFAULT_JSONL_MAX_BYTES,
    backup_keep: int = DEFAULT_CORRUPT_BACKUP_KEEP,
) -> dict[str, Any]:
    jsonl_reports = []
    seen: set[str] = set()
    for cand in LOG_CANDIDATES:
        key = str(cand.resolve()) if cand.exists() else str(cand)
        if key in seen:
            continue
        seen.add(key)
        if cand.exists():
            jsonl_reports.append(
                trim_jsonl(cand, max_days=max_days, max_bytes=max_bytes)
            )

    backup_reports = []
    for d in CORRUPT_BACKUP_DIRS:
        if d.is_dir():
            backup_reports.append(
                prune_corrupt_backups(d, keep=backup_keep)
            )

    freed = sum(int(r.get("bytes_freed") or 0) for r in jsonl_reports + backup_reports)
    return {
        "ok": True,
        "ts_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "jsonl": jsonl_reports,
        "corrupt_backups": backup_reports,
        "bytes_freed": freed,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Sentinel log / corrupt-backup retention")
    p.add_argument("--days", type=int, default=DEFAULT_JSONL_MAX_DAYS)
    p.add_argument("--max-bytes", type=int, default=DEFAULT_JSONL_MAX_BYTES)
    p.add_argument("--backup-keep", type=int, default=DEFAULT_CORRUPT_BACKUP_KEEP)
    args = p.parse_args()
    report = run_retention(
        max_days=args.days,
        max_bytes=args.max_bytes,
        backup_keep=args.backup_keep,
    )
    import json

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
