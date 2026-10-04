"""Scan health payloads and log tails for credential material."""

from __future__ import annotations

import re
from pathlib import Path

PATTERNS = (
    re.compile(r"sk_sent_[A-Za-z0-9]{6,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?i)(api_key|token|userkey|secret)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}"),
)


def find_secret_leaks(text: str) -> list[str]:
    hits: list[str] = []
    for pattern in PATTERNS:
        found = pattern.search(text or "")
        if found:
            hits.append(pattern.pattern)
    return hits


def scan_health_and_logs(health_text: str, log_paths: list[Path], *, tail_lines: int = 1000) -> list[str]:
    leaks = find_secret_leaks(health_text)
    for path in log_paths:
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        leaks.extend(find_secret_leaks("\n".join(lines[-tail_lines:])))
    return leaks
