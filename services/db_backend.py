"""DB_BACKEND flag. Serving stays on SQLite until the timescale runbook cutover."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _from_config() -> str:
    path = ROOT / "config.yaml"
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("db_backend:"):
            return stripped.split(":", 1)[1].strip().strip("\"'")
    return ""


def requested_backend() -> str:
    raw = (os.getenv("DB_BACKEND") or _from_config() or "sqlite").strip().lower()
    if raw not in {"sqlite", "timescale"}:
        return "sqlite"
    return raw


def active_backend() -> str:
    """Live queries stay on sqlite until a human cutover marker exists."""
    if requested_backend() != "timescale":
        return "sqlite"
    marker = ROOT / "data" / "archive" / "timescale_cutover.json"
    if marker.is_file():
        return "timescale"
    return "sqlite"
