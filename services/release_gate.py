"""Deploy / Truth Gate helpers for run_release.py (Gate ≠ Publish).

Dual semantics (Prompt dual-gate / G3 terrestrial ceiling):
  - pipeline_health_status  → BLOCKING for --prod-rebuild / --prod-gate
  - fleet_sample_status     → informational only (FULL/LIMITED/INSUFFICIENT)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SENTINEL_HTML = ROOT / "output" / "sentinel_dashboard.html"


def health_paths() -> list[Path]:
    return [
        ROOT / "output" / "api" / "v1" / "health.json",
        ROOT / "output" / "api" / "v1" / "health",
    ]


def load_health_disk() -> dict[str, Any]:
    for p in health_paths():
        if not p.exists():
            continue
        try:
            return json.loads(p.read_text(encoding="utf-8", errors="ignore"))
        except (json.JSONDecodeError, OSError):
            continue
    return {}


def listening_ports() -> set[int]:
    ports: set[int] = set()
    try:
        out = subprocess.check_output(["netstat", "-ano"], text=True, errors="replace")
    except (OSError, subprocess.CalledProcessError):
        try:
            out = subprocess.check_output(["ss", "-lnt"], text=True, errors="replace")
        except (OSError, subprocess.CalledProcessError):
            return ports
    for line in out.splitlines():
        if "LISTEN" not in line.upper():
            continue
        for token in line.replace(":::", ":").split():
            if ":" not in token:
                continue
            try:
                ports.add(int(token.rsplit(":", 1)[-1]))
            except ValueError:
                continue
    return ports


def snapshot_publish_artifacts() -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    snap = ROOT / "output" / ".publish_snapshot" / stamp
    snap.mkdir(parents=True, exist_ok=True)
    targets = [
        SENTINEL_HTML,
        SENTINEL_HTML.with_suffix(SENTINEL_HTML.suffix + ".meta.json"),
        ROOT / "output" / "api" / "v1" / "health.json",
        ROOT / "output" / "api" / "v1" / "health",
    ]
    manifest: list[str] = []
    for src in targets:
        if not src.exists():
            continue
        rel = src.relative_to(ROOT / "output")
        dst = snap / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        manifest.append(str(rel).replace("\\", "/"))
    (snap / "MANIFEST.json").write_text(
        json.dumps({"files": manifest, "created_at": stamp}, indent=2),
        encoding="utf-8",
    )
    return snap


def restore_publish_artifacts(snap: Path) -> None:
    man_path = snap / "MANIFEST.json"
    if not man_path.exists():
        print(f"WARN: snapshot manifest missing at {snap}", file=sys.stderr)
        return
    man = json.loads(man_path.read_text(encoding="utf-8"))
    for rel in man.get("files") or []:
        src = snap / rel
        dst = ROOT / "output" / rel
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        print(f"  restored {rel}")


def assert_deploy_gate(
    *,
    strict: bool,
    label: str = "Deploy Gate",
) -> tuple[int, list[str]]:
    """
    Blocking criterion: pipeline_health_status == NOMINAL.

    fleet_sample_status (FULL/LIMITED/INSUFFICIENT) is reported but NEVER fails the gate.
    ``strict`` kept for API compatibility; both modes use the same blocking set.
    """
    from services.ais_health import compute_ais_freshness, compute_top500_live_coverage
    from services.dual_gate import (
        compute_fleet_sample_status,
        compute_pipeline_health_status,
    )

    _ = strict  # API compat — dual-gate blocking set is identical for gate/rebuild
    print()
    print("=" * 72, file=sys.stderr)
    print(f"STEP: {label} (dual-gate; blocking=pipeline_health only)", file=sys.stderr)
    print("=" * 72, file=sys.stderr)

    failures: list[str] = []
    health = load_health_disk()

    try:
        freshness = compute_ais_freshness()
    except Exception as exc:  # noqa: BLE001
        failures.append(f"ais_freshness_unavailable:{exc}")
        freshness = {}

    ports = listening_ports()
    dash_ports = sorted(p for p in ports if p in (8765, 8478))
    port_drift = 8478 in ports
    port_ok = (8765 in ports) or not ports.intersection({8765, 8478})
    # If neither dashboard port is listening yet (build-only), do not fail port_ok
    # unless 8478 drift is present.
    if ports.intersection({8765, 8478}) and 8765 not in ports:
        port_ok = False

    connector = health.get("connector") or {}
    pipe = compute_pipeline_health_status(
        freshness=freshness,
        connector=connector,
        port_ok=port_ok if ports.intersection({8765, 8478}) else True,
        port_drift_8478=port_drift,
    )
    print(f"  pipeline_health_status = {pipe['pipeline_health_status']!r}  reasons={pipe.get('reasons')}")
    print(f"  ais_lag                = {pipe.get('ais_lag_sec')!r}s")
    print(f"  reconnects / 429       = {pipe.get('reconnects')} / {connector.get('http_429_count', 0)}")
    print(f"  listening_ports        = {dash_ports}")

    if pipe["pipeline_health_status"] != "NOMINAL":
        failures.append(
            f"pipeline_health_status!='NOMINAL' (got {pipe['pipeline_health_status']}; "
            f"reasons={pipe.get('reasons')})"
        )

    cov = health.get("top500_live_coverage")
    if cov is None:
        try:
            cov = compute_top500_live_coverage().get("top500_live_coverage")
        except Exception as exc:  # noqa: BLE001
            print(f"  WARN coverage unavailable: {exc}")
            cov = 0
    sample = compute_fleet_sample_status(cov)
    print(
        f"  fleet_sample_status    = {sample['fleet_sample_status']!r} "
        f"(N={sample['top500_live_coverage']}, LIMITED_MIN={sample['limited_min']}, "
        f"FULL_MIN={sample['full_min']}) — informational, non-blocking"
    )
    if sample.get("sample_size_caveat"):
        print(f"  sample_size_caveat     = {sample['sample_size_caveat']}")

    qp = health.get("quant_pipeline") or {}
    basis = str(qp.get("accuracy_basis") or "")
    print(f"  accuracy_basis         = {basis!r} (informational)")
    print(f"  live_inference_confidence = {qp.get('live_inference_confidence')!r}")

    source_mode = str(health.get("source_mode") or "").lower()
    print(f"  source_mode            = {source_mode!r}")
    forbidden = {"synthetic", "synthetic_seed", "fallback", "mock", "demo", "placeholder"}
    flagged = [tag for tag in forbidden if tag in source_mode]
    if flagged and pipe["pipeline_health_status"] == "NOMINAL":
        # Synthetic source cannot be NOMINAL pipeline for publish
        failures.append(f"source_mode_forbidden_tags:{flagged}")

    db_candidates = [
        ROOT / "история1" / "sentinel_ais.db",
        ROOT / "sentinel_ais.db",
        ROOT / "data" / "sentinel_ais.db",
    ]
    env_db = os.environ.get("SENTINEL_DB_PATH", "")
    if env_db and "raw_positions.db" in env_db.replace("\\", "/").lower():
        failures.append("SENTINEL_DB_PATH_legacy_raw_positions_forbidden")
    if env_db:
        db_candidates.insert(0, Path(env_db))
    db = next((p for p in db_candidates if p.exists()), None)
    if db is None:
        failures.append("sentinel_ais.db_missing")
    elif "raw_positions.db" in str(db).replace("\\", "/").lower():
        failures.append(f"active_db_legacy_forbidden:{db}")
    else:
        db_size = db.stat().st_size
        print(f"  db                     = {db} size={db_size:,}")
        if db_size < 100_000:
            failures.append(f"db_too_small:{db_size}")
        if port_drift:
            failures.append("port_drift_8478_listening")

    print(f"  published_at           = {health.get('published_at')!r}")
    print(f"  build_mode             = {health.get('build_mode')!r}")

    if failures:
        print(f"FAIL [{label}] unmet conditions:", file=sys.stderr)
        for item in failures:
            print(f"  - {item}", file=sys.stderr)
        return 1, failures

    print(f"PASS — {label}: pipeline_health=NOMINAL (fleet_sample={sample['fleet_sample_status']}).")
    return 0, []
