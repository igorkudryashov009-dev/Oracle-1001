#!/usr/bin/env python3
"""E2E pre-deploy checks: TOP-10 GLB integrity + dual-gate health + UI HTML.

Hard-fail (exit 1) — blocks release:
  * 10× vessel_{IMO}.glb present, size > 100 KiB, binary magic ``glTF``
    (LE uint32 ``0x46546C67``; note: ``0x46544C67`` in some briefs is a typo — ``l`` is 0x6C)
  * pipeline_health_status == NOMINAL
  * AIS lag (replica.age_sec) < 300s
  * Canonical HTTP port 8765; port 8478 = CRITICAL drift
  * SoT DB = история1/sentinel_ais.db (legacy raw_positions.db forbidden as active path)
  * sentinel_dashboard.html / top100_analytics.html / fleet_meta_analysis.html reachable

Informational only — NEVER fails release (AGENTS.md dual-gate / G3):
  * fleet_sample_status ∈ {FULL, LIMITED, INSUFFICIENT}
  * N>=100 aspirational FULL is reported, not required

Note: ``fleet_sample == "NOMINAL"`` is NOT a valid enum. NOMINAL belongs to
pipeline_health_status only. Requiring FULL/N>=100 would break terrestrial G3 publish.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GLB_DIR = ROOT / "output" / "assets" / "3d_models"
HEALTH_DISK = ROOT / "output" / "api" / "v1" / "health.json"
MIN_GLB_BYTES = 100 * 1024  # Prompt-4: non-degenerate GLB > 100 KiB
GLTF_MAGIC_BYTES = b"glTF"
GLTF_MAGIC = struct.unpack("<I", GLTF_MAGIC_BYTES)[0]  # 0x46546C67
PIPELINE_LAG_MAX_SEC = 300
CANONICAL_PORT = 8765
FORBIDDEN_PORT = 8478
SENTINEL_DB = ROOT / "история1" / "sentinel_ais.db"
LEGACY_DB_FORBIDDEN = "raw_positions.db"

UI_REL_PATHS = (
    "output/sentinel_dashboard.html",
    "output/top100_analytics.html",
    "output/fleet_meta_analysis.html",
)

# Fallback IMO list if manifest import fails (must stay in sync with top10_vessels).
_FALLBACK_IMOS = (
    "9388833",
    "9397303",
    "9397315",
    "9397327",
    "9337755",
    "9372731",
    "9372743",
    "9388819",
    "9388821",
    "9397298",
)


def _ok(msg: str) -> None:
    print(f"  OK  {msg}")


def _fail(msg: str, fails: list[str]) -> None:
    print(f"  FAIL {msg}")
    fails.append(msg)


def _warn(msg: str) -> None:
    print(f"  WARN {msg}")


def _info(msg: str) -> None:
    print(f"  INFO {msg}")


def _port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    import socket

    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def check_sre_bindings(fails: list[str], *, require_http: bool) -> dict[str, Any]:
    """Canonical port 8765 + sentinel_ais.db SoT; reject 8478 / raw_positions.db."""
    print()
    print("-" * 64)
    print("CHECK: SRE bindings (port + DB SoT)")
    print("-" * 64)
    out: dict[str, Any] = {"ok": True}

    try:
        sys.path.insert(0, str(ROOT))
        from services.utils.canonical_port import assert_canonical_dashboard_port, resolve_dashboard_port

        port = assert_canonical_dashboard_port(resolve_dashboard_port(), context="verify_3d_and_health")
        _ok(f"DASHBOARD_PORT={port} (canonical; 8478 rejected)")
        out["dashboard_port"] = port
    except SystemExit:
        _fail("DASHBOARD_PORT rejected (8478 or invalid) — CRITICAL deployment drift", fails)
        out["ok"] = False
        out["dashboard_port"] = None
        port = CANONICAL_PORT

    if _port_open("127.0.0.1", FORBIDDEN_PORT):
        _fail(f"port {FORBIDDEN_PORT} is LISTENING — CRITICAL drift (unbind immediately)", fails)
        out["ok"] = False
        out["port_8478"] = "listening"
    else:
        _ok(f"port {FORBIDDEN_PORT} unbound")
        out["port_8478"] = "unbound"

    if require_http:
        if _port_open("127.0.0.1", CANONICAL_PORT):
            _ok(f"port {CANONICAL_PORT} listening (--require-http)")
            out["port_8765"] = "listening"
        else:
            _fail(f"port {CANONICAL_PORT} not listening (--require-http)", fails)
            out["ok"] = False
            out["port_8765"] = "down"
    else:
        out["port_8765"] = "listening" if _port_open("127.0.0.1", CANONICAL_PORT) else "down"
        _info(f"port {CANONICAL_PORT} status={out['port_8765']} (informational when not --require-http)")

    env_db = (os.environ.get("SENTINEL_DB_PATH") or "").strip()
    if env_db and LEGACY_DB_FORBIDDEN in env_db.replace("\\", "/").lower():
        _fail(
            f"SENTINEL_DB_PATH points to legacy {LEGACY_DB_FORBIDDEN} — forbidden SoT "
            f"(use история1/sentinel_ais.db)",
            fails,
        )
        out["ok"] = False

    db = Path(env_db) if env_db else SENTINEL_DB
    if not env_db:
        candidates = [
            SENTINEL_DB,
            ROOT / "sentinel_ais.db",
            ROOT / "data" / "sentinel_ais.db",
        ]
        db = next((p for p in candidates if p.is_file()), SENTINEL_DB)

    if not db.is_file():
        _fail(f"sentinel_ais.db missing at {db}", fails)
        out["ok"] = False
    elif LEGACY_DB_FORBIDDEN in str(db).replace("\\", "/").lower():
        _fail(f"active DB is legacy {db} — prohibited", fails)
        out["ok"] = False
    else:
        size = db.stat().st_size
        if size < 100_000:
            _fail(f"sentinel_ais.db too small ({size} B)", fails)
            out["ok"] = False
        else:
            _ok(f"DB SoT={db} size={size:,} (legacy raw_positions.db not used)")
        out["db"] = str(db)
        out["db_bytes"] = size

    legacy = ROOT / "история1" / LEGACY_DB_FORBIDDEN
    if legacy.is_file():
        _info(
            f"legacy {legacy.name} present on disk (OK as archive only; "
            "must never be active Sentinel SoT)"
        )

    return out


def check_web_assets_md5(fails: list[str]) -> dict[str, Any]:
    """Blocking: web/js|css must match output/ (MD5/SHA sync)."""
    print()
    print("-" * 64)
    print("CHECK: web -> output asset MD5 sync")
    print("-" * 64)
    try:
        sys.path.insert(0, str(ROOT))
        from services.web_assets_sync import sync_web_assets, verify_sha256_pairs

        sync_web_assets(ROOT, force=False, log=True)
        ok, rows = verify_sha256_pairs(ROOT)
        mismatched = [r for r in rows if not r.get("ok")]
        if not ok:
            for row in mismatched[:12]:
                _fail(
                    f"asset drift {row.get('src')} -> {row.get('dst')} "
                    f"err={row.get('error') or 'sha_mismatch'}",
                    fails,
                )
            if len(mismatched) > 12:
                _fail(f"... and {len(mismatched) - 12} more asset drifts", fails)
            return {"ok": False, "pairs": len(rows), "mismatched": len(mismatched)}
        _ok(f"web->output assets in sync ({len(rows)} pairs)")
        return {"ok": True, "pairs": len(rows), "mismatched": 0}
    except Exception as exc:  # noqa: BLE001
        _fail(f"web assets sync/verify failed: {exc}", fails)
        return {"ok": False, "error": str(exc)}


def resolve_top10_imos() -> list[str]:
    try:
        sys.path.insert(0, str(ROOT))
        from services.top10_vessels import TOP10_VESSELS

        imos = [str(v.get("imo") or "").strip() for v in TOP10_VESSELS]
        imos = [i for i in imos if i]
        if len(imos) == 10:
            return imos
    except Exception as exc:  # noqa: BLE001
        _warn(f"top10_vessels import failed ({exc}); using fallback IMO list")
    return list(_FALLBACK_IMOS)


def check_glb_assets(fails: list[str]) -> dict[str, Any]:
    print()
    print("-" * 64)
    print("CHECK: TOP-10 GLB models (size + glTF magic)")
    print("-" * 64)
    imos = resolve_top10_imos()
    results: list[dict[str, Any]] = []
    if not GLB_DIR.is_dir():
        _fail(f"missing directory {GLB_DIR}", fails)
        return {"ok": False, "models": results}

    for imo in imos:
        path = GLB_DIR / f"vessel_{imo}.glb"
        row: dict[str, Any] = {"imo": imo, "path": str(path)}
        if not path.is_file():
            _fail(f"missing GLB IMO={imo} path={path}", fails)
            row["ok"] = False
            results.append(row)
            continue
        size = path.stat().st_size
        row["bytes"] = size
        if size <= MIN_GLB_BYTES:
            _fail(f"GLB IMO={imo} too small ({size} B ≤ {MIN_GLB_BYTES})", fails)
            row["ok"] = False
            results.append(row)
            continue
        try:
            with path.open("rb") as fh:
                magic_raw = fh.read(4)
                version = struct.unpack("<I", fh.read(4))[0] if size >= 8 else 0
        except OSError as exc:
            _fail(f"GLB IMO={imo} unreadable: {exc}", fails)
            row["ok"] = False
            results.append(row)
            continue
        magic = struct.unpack("<I", magic_raw)[0] if len(magic_raw) == 4 else 0
        row["magic"] = hex(magic)
        row["gltf_version"] = version
        if magic_raw != GLTF_MAGIC_BYTES:
            _fail(
                f"GLB IMO={imo} bad magic {magic_raw!r}/{hex(magic)} "
                f"(expected {GLTF_MAGIC_BYTES!r}={hex(GLTF_MAGIC)})",
                fails,
            )
            row["ok"] = False
            results.append(row)
            continue
        _ok(f"vessel_{imo}.glb bytes={size:,} magic=glTF v{version}")
        row["ok"] = True
        results.append(row)

    ready = sum(1 for r in results if r.get("ok"))
    if ready != 10:
        _fail(f"expected 10 valid GLB, got {ready}", fails)
    return {"ok": ready == 10 and not any(not r.get("ok") for r in results), "models": results}


def _extract_pipeline_status(doc: dict[str, Any]) -> str:
    direct = doc.get("pipeline_health_status")
    if isinstance(direct, str) and direct.strip():
        return direct.strip().upper()
    blob = doc.get("pipeline_health")
    if isinstance(blob, dict):
        nested = blob.get("pipeline_health_status") or blob.get("status")
        if isinstance(nested, str) and nested.strip():
            return nested.strip().upper()
    return ""


def _extract_fleet_sample_status(doc: dict[str, Any]) -> tuple[str, int]:
    status = doc.get("fleet_sample_status")
    n = doc.get("top500_live_coverage")
    blob = doc.get("fleet_sample")
    if isinstance(blob, dict):
        status = status or blob.get("fleet_sample_status")
        if n is None:
            n = blob.get("top500_live_coverage")
    try:
        cov = int(n or 0)
    except (TypeError, ValueError):
        cov = 0
    return (str(status or "").strip().upper(), cov)


def _extract_ais_lag_sec(doc: dict[str, Any]) -> float | None:
    """Prefer replica.age_sec; accept freshness / ais_lag aliases."""
    for key in ("ais_lag", "ais_lag_sec"):
        if doc.get(key) is not None:
            try:
                return float(doc[key])
            except (TypeError, ValueError):
                pass
    replica = doc.get("replica") if isinstance(doc.get("replica"), dict) else {}
    if replica.get("age_sec") is not None:
        try:
            return float(replica["age_sec"])
        except (TypeError, ValueError):
            pass
    fr = doc.get("freshness") if isinstance(doc.get("freshness"), dict) else {}
    if fr.get("age_sec") is not None:
        try:
            return float(fr["age_sec"])
        except (TypeError, ValueError):
            pass
    pipe = doc.get("pipeline_health") if isinstance(doc.get("pipeline_health"), dict) else {}
    if pipe.get("ais_lag_sec") is not None:
        try:
            return float(pipe["ais_lag_sec"])
        except (TypeError, ValueError):
            pass
    return None


def load_health_disk() -> dict[str, Any]:
    if not HEALTH_DISK.is_file():
        return {}
    try:
        return json.loads(HEALTH_DISK.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def fetch_health_http(base_url: str, timeout: float = 8.0) -> dict[str, Any] | None:
    url = base_url.rstrip("/") + "/output/api/v1/health.json"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def check_health(
    fails: list[str],
    *,
    base_url: str,
    require_http: bool,
    disk_only: bool,
) -> dict[str, Any]:
    print()
    print("-" * 64)
    print("CHECK: health metrics (dual-gate)")
    print("-" * 64)

    source = "disk"
    doc: dict[str, Any] = {}
    if not disk_only:
        http_doc = fetch_health_http(base_url)
        if http_doc is not None:
            doc = http_doc
            source = "http"
            _ok(f"fetched {base_url.rstrip('/')}/output/api/v1/health.json")
        elif require_http:
            _fail(f"HTTP health unreachable at {base_url} (--require-http)", fails)
            return {"ok": False, "source": "http_missing"}
        else:
            _warn(f"HTTP health unreachable; falling back to {HEALTH_DISK}")

    if not doc:
        doc = load_health_disk()
        source = "disk"
        if not doc:
            _fail(f"missing/unreadable health document {HEALTH_DISK}", fails)
            return {"ok": False, "source": source}

    pipe = _extract_pipeline_status(doc)
    fleet_status, cov_n = _extract_fleet_sample_status(doc)
    lag = _extract_ais_lag_sec(doc)

    # -- BLOCKING: pipeline ---------------------------------------------------
    if pipe != "NOMINAL":
        _fail(f"pipeline_health_status={pipe!r} (required NOMINAL) source={source}", fails)
    else:
        _ok(f"pipeline_health_status=NOMINAL source={source}")

    if lag is None:
        _fail("ais_lag / replica.age_sec missing from health document", fails)
    elif lag >= PIPELINE_LAG_MAX_SEC:
        _fail(f"ais_lag={lag:.1f}s >= {PIPELINE_LAG_MAX_SEC}s", fails)
    else:
        _ok(f"ais_lag={lag:.1f}s < {PIPELINE_LAG_MAX_SEC}s")

    # -- INFORMATIONAL: fleet sample (G3 — never hard-fail) -------------------
    valid_fleet = {"FULL", "LIMITED", "INSUFFICIENT"}
    if fleet_status not in valid_fleet:
        _warn(
            f"fleet_sample_status={fleet_status!r} unexpected "
            f"(expected one of {sorted(valid_fleet)}; NOMINAL is invalid here)"
        )
    else:
        _info(
            f"fleet_sample_status={fleet_status} top500_live_coverage={cov_n} "
            "(informational — does not block release / G3 terrestrial OK)"
        )
    if fleet_status == "FULL" and cov_n >= 100:
        _ok("fleet sample FULL (N>=100) — fleet-wide metrics eligible")
    else:
        _info(
            "fleet sample below FULL aspirational target — expected on terrestrial AIS; "
            "publish still allowed when pipeline=NOMINAL"
        )

    return {
        "ok": pipe == "NOMINAL" and lag is not None and lag < PIPELINE_LAG_MAX_SEC,
        "source": source,
        "pipeline_health_status": pipe,
        "fleet_sample_status": fleet_status,
        "top500_live_coverage": cov_n,
        "ais_lag_sec": lag,
    }


def http_probe(url: str, timeout: float = 8.0) -> int | None:
    """HEAD then GET fallback; return status code or None on transport error."""
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return int(resp.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except (urllib.error.URLError, TimeoutError, OSError):
            if method == "GET":
                return None
    return None


def check_ui(
    fails: list[str],
    *,
    base_url: str,
    require_http: bool,
    disk_only: bool,
) -> dict[str, Any]:
    print()
    print("-" * 64)
    print("CHECK: UI HTML integrity")
    print("-" * 64)
    rows: list[dict[str, Any]] = []
    for rel in UI_REL_PATHS:
        path = ROOT / rel
        row: dict[str, Any] = {"path": rel}
        if not path.is_file():
            _fail(f"missing UI artifact {rel}", fails)
            row["disk_ok"] = False
            rows.append(row)
            continue
        size = path.stat().st_size
        row["bytes"] = size
        row["disk_ok"] = size > 0
        if size <= 0:
            _fail(f"empty UI artifact {rel}", fails)
        else:
            _ok(f"disk {rel} bytes={size:,}")

        if disk_only:
            rows.append(row)
            continue

        url = base_url.rstrip("/") + "/" + rel.replace("\\", "/")
        code = http_probe(url)
        row["http_status"] = code
        if code == 200:
            _ok(f"HTTP {code} {url}")
        elif code is None:
            msg = f"HTTP unreachable {url}"
            if require_http:
                _fail(msg + " (--require-http)", fails)
            else:
                _warn(msg + " (disk OK; start --serve for live probe)")
        else:
            _fail(f"HTTP {code} {url} (expected 200)", fails)
        rows.append(row)

    # Soft UI wiring markers (dashboard modules for GLB inspector)
    dash = ROOT / "output" / "sentinel_dashboard.html"
    js_need = (
        ROOT / "output" / "js" / "top10_sheet.js",
        ROOT / "output" / "js" / "top10_3d_viewer.js",
        ROOT / "output" / "js" / "vessel_3d_reconstruction.js",
    )
    for jp in js_need:
        if jp.is_file():
            _ok(f"JS present {jp.relative_to(ROOT)}")
        else:
            _fail(f"missing JS {jp.relative_to(ROOT)}", fails)
    if dash.is_file():
        text = dash.read_text(encoding="utf-8", errors="ignore")
        if 'data-sheet="top10"' in text:
            _ok('sentinel_dashboard has data-sheet="top10"')
        else:
            _fail('sentinel_dashboard missing data-sheet="top10"', fails)

    return {"ok": True, "pages": rows}


def run_checks(
    *,
    base_url: str | None = None,
    require_http: bool = False,
    disk_only: bool = False,
) -> int:
    port = (os.environ.get("DASHBOARD_PORT") or os.environ.get("PORT") or "8765").strip()
    base = (base_url or f"http://127.0.0.1:{port}").rstrip("/")
    fails: list[str] = []

    print("=" * 64)
    print("verify_3d_and_health — pre-deploy E2E gate")
    print(f"ROOT={ROOT}")
    print(f"base_url={base} require_http={require_http} disk_only={disk_only}")
    print("=" * 64)

    glb = check_glb_assets(fails)
    sre = check_sre_bindings(fails, require_http=require_http)
    assets = check_web_assets_md5(fails)
    health = check_health(
        fails, base_url=base, require_http=require_http, disk_only=disk_only
    )
    ui = check_ui(fails, base_url=base, require_http=require_http, disk_only=disk_only)

    report = {
        "ok": len(fails) == 0,
        "failures": fails,
        "glb": glb,
        "sre_bindings": sre,
        "web_assets": assets,
        "health": health,
        "ui": ui,
        "contract": {
            "blocking": [
                "glb_integrity_>100KiB",
                "pipeline_health_status==NOMINAL",
                "ais_lag<300",
                "port_8765_not_8478",
                "db_sentinel_ais_only",
                "web_output_md5_sync",
                "ui_html",
            ],
            "informational": ["fleet_sample_status"],
            "note": (
                "fleet_sample never uses NOMINAL; FULL/LIMITED/INSUFFICIENT only. "
                "N<100 does not fail this gate (G3 terrestrial ceiling)."
            ),
        },
    }
    out = ROOT / "output" / "verify_3d_and_health_report.json"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        _info(f"wrote {out.relative_to(ROOT)}")
    except OSError as exc:
        _warn(f"could not write report: {exc}")

    print()
    print("=" * 64)
    if fails:
        print(f"FAIL — {len(fails)} assertion(s):")
        for item in fails:
            print(f"  - {item}")
        print("=" * 64)
        return 1
    print("PASS — GLB + pipeline health + UI integrity OK")
    print("=" * 64)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=None,
        help="Dashboard origin (default http://127.0.0.1:$DASHBOARD_PORT)",
    )
    parser.add_argument(
        "--require-http",
        action="store_true",
        help="Fail if HTTP health/UI probes are unreachable (use with --serve)",
    )
    parser.add_argument(
        "--disk-only",
        action="store_true",
        help="Skip all HTTP probes; assert disk artifacts + health.json only",
    )
    args = parser.parse_args(argv)
    if args.disk_only and args.require_http:
        print("FAIL: --disk-only and --require-http are mutually exclusive", file=sys.stderr)
        return 1
    return run_checks(
        base_url=args.base_url,
        require_http=bool(args.require_http),
        disk_only=bool(args.disk_only),
    )


if __name__ == "__main__":
    raise SystemExit(main())
