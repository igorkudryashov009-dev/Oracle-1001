"""Project path resolution — no hardcoded C:\\Users\\... absolute paths."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def project_root() -> Path:
    """Oracle-1001/7000 root (directory that contains run_all.py)."""
    return Path(__file__).resolve().parent


def resolve_fleet_source(explicit: Optional[Path] = None) -> Path:
    """Resolve IMO_Filtered_Clean.xlsx with Cyrillic-safe pathlib.

    Priority:
      1) explicit argument
      2) env FLEET_SOURCE_XLSX / ORACLE_SOURCE_XLSX
      3) <project>/data/IMO_Filtered_Clean.xlsx
      4) ~/OneDrive/Desktop/Жемчуг/IMO_Filtered_Clean.xlsx
      5) ~/Desktop/Жемчуг/IMO_Filtered_Clean.xlsx
    """
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit))

    for env_key in ("FLEET_SOURCE_XLSX", "ORACLE_SOURCE_XLSX"):
        raw = os.environ.get(env_key, "").strip().strip('"').strip("'")
        if raw:
            candidates.append(Path(raw))

    root = project_root()
    home = Path.home()
    candidates.extend(
        [
            root / "data" / "IMO_Filtered_Clean.xlsx",
            home / "OneDrive" / "Desktop" / "Жемчуг" / "IMO_Filtered_Clean.xlsx",
            home / "Desktop" / "Жемчуг" / "IMO_Filtered_Clean.xlsx",
        ]
    )

    seen: set[str] = set()
    tried: list[str] = []
    for cand in candidates:
        try:
            resolved = cand.expanduser().resolve()
        except OSError:
            tried.append(str(cand))
            continue
        key = str(resolved).lower()
        if key in seen:
            continue
        seen.add(key)
        tried.append(str(resolved))
        if resolved.is_file():
            return resolved

    lines = "\n".join(f"  - {p}" for p in tried)
    raise FileNotFoundError(
        "Не найден входной файл IMO_Filtered_Clean.xlsx.\n"
        "Проверенные пути (кириллица поддерживается через pathlib):\n"
        f"{lines}\n"
        "Задайте FLEET_SOURCE_XLSX или положите копию в data/IMO_Filtered_Clean.xlsx."
    )
