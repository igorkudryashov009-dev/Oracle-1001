#!/usr/bin/env python3
"""Install repo githooks (Prompt 11). Sets core.hooksPath=githooks."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    hook = ROOT / "githooks" / "pre-commit"
    if not hook.is_file():
        print(f"FAIL: missing {hook}", file=sys.stderr)
        return 1
    # Ensure executable bit for Unix checkouts (best-effort on Windows).
    try:
        hook.chmod(hook.stat().st_mode | 0o111)
    except OSError:
        pass
    subprocess.check_call(
        ["git", "config", "core.hooksPath", "githooks"],
        cwd=str(ROOT),
    )
    print("OK: git config core.hooksPath=githooks")
    print("Contract drift guard active on commits touching ingest/gate/UI/docs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
