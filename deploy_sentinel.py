#!/usr/bin/env python3
"""Shim: run deploy/sentinel/deploy_sentinel.py from repo root."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent / "deploy" / "sentinel" / "deploy_sentinel.py"
if not TARGET.is_file():
    sys.stderr.write(f"missing {TARGET}\n")
    sys.exit(2)
sys.exit(runpy.run_path(str(TARGET), run_name="__main__"))
