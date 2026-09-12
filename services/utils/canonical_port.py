"""Canonical dashboard port lock (AGENTS.md): 8765 only; 8478 = CRITICAL drift."""

from __future__ import annotations

import os
import sys

CANONICAL_DASHBOARD_PORT = 8765
FORBIDDEN_DRIFT_PORTS = frozenset({8478})


def resolve_dashboard_port(*, default: int = CANONICAL_DASHBOARD_PORT) -> int:
    raw = (os.environ.get("DASHBOARD_PORT") or os.environ.get("PORT") or str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return int(default)


def assert_canonical_dashboard_port(port: int | None = None, *, context: str = "") -> int:
    """Return port if allowed; exit/raise on 8478 (or other forbidden) drift.

    Used by run_server / serve_dashboard / run_release before bind.
    """
    p = int(port if port is not None else resolve_dashboard_port())
    ctx = f" ({context})" if context else ""
    if p in FORBIDDEN_DRIFT_PORTS:
        msg = (
            f"CRITICAL deployment drift{ctx}: port {p} is forbidden. "
            f"Canonical DASHBOARD_PORT is {CANONICAL_DASHBOARD_PORT} only (AGENTS.md)."
        )
        print(msg, file=sys.stderr)
        raise SystemExit(1)
    if p <= 0 or p > 65535:
        print(f"FAIL{ctx}: invalid dashboard port {p}", file=sys.stderr)
        raise SystemExit(1)
    return p
