#!/usr/bin/env python3
"""Deploy Manifest Verification — break the stale-JS-on-Node-A pattern.

Compares sha256 of HUD-critical assets in the git working tree against the
bytes actually served by Node A (:8765). Also detects local web/ ↔ output/js
drift (source vs packaged serve tree).

Usage:
  python scripts/verify_deploy_manifest.py
  python scripts/verify_deploy_manifest.py --live --strict
  python scripts/verify_deploy_manifest.py --base-url http://45.8.230.214:8765
  python scripts/verify_deploy_manifest.py --local-only

Env:
  SENTINEL_NODE_A          default host (45.8.230.214)
  SENTINEL_VERIFY_LIVE=1   force live check (strict when unreachable → fail)
  SENTINEL_VERIFY_STRICT=1 mismatch / unreachable → exit 1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_NODE_A = os.environ.get("SENTINEL_NODE_A", "45.8.230.214").strip() or "45.8.230.214"
DEFAULT_BASE = f"http://{DEFAULT_NODE_A}:8765"
TIMEOUT_SEC = float(os.environ.get("SENTINEL_VERIFY_TIMEOUT_SEC", "25"))


@dataclass(frozen=True)
class ManifestEntry:
    """One deploy-critical asset.

    local_paths: ordered candidates; first existing file is the expected hash SoT.
    url_path: path on the public HUD origin (Node A sentinel-web).
    """

    name: str
    local_paths: tuple[str, ...]
    url_path: str


# HUD behaviour surface — the files that repeatedly went stale on Node A.
MANIFEST: tuple[ManifestEntry, ...] = (
    ManifestEntry(
        "sentinel_engine.js",
        ("output/js/sentinel_engine.js", "web/sentinel_engine.js"),
        "/output/js/sentinel_engine.js",
    ),
    ManifestEntry(
        "top10_sheet.js",
        ("output/js/top10_sheet.js", "web/js/top10_sheet.js"),
        "/output/js/top10_sheet.js",
    ),
    ManifestEntry(
        "map_tiles.js",
        ("output/js/map_tiles.js", "web/js/map_tiles.js"),
        "/output/js/map_tiles.js",
    ),
    ManifestEntry(
        "route_map_view.js",
        ("output/js/route_map_view.js", "web/js/route_map_view.js"),
        "/output/js/route_map_view.js",
    ),
    ManifestEntry(
        "route_sheet.js",
        ("output/js/route_sheet.js", "web/js/route_sheet.js"),
        "/output/js/route_sheet.js",
    ),
    ManifestEntry(
        "arctic_sheet.js",
        ("output/js/arctic_sheet.js", "web/js/arctic_sheet.js"),
        "/output/js/arctic_sheet.js",
    ),
    ManifestEntry(
        "arctic_vessels_manifest.js",
        ("output/js/arctic_vessels_manifest.js", "web/js/arctic_vessels_manifest.js"),
        "/output/js/arctic_vessels_manifest.js",
    ),
    ManifestEntry(
        "arctic_video_9737187",
        (
            "assets/arctic/videos/vessel_9737187.mp4",
            "output/assets/arctic/videos/vessel_9737187.mp4",
        ),
        "/assets/arctic/videos/vessel_9737187.mp4",
    ),
    ManifestEntry(
        "arctic_video_9750749",
        (
            "assets/arctic/videos/vessel_9750749.mp4",
            "output/assets/arctic/videos/vessel_9750749.mp4",
        ),
        "/assets/arctic/videos/vessel_9750749.mp4",
    ),
    ManifestEntry(
        "archive_sheet.js",
        ("output/js/archive_sheet.js", "web/js/archive_sheet.js"),
        "/output/js/archive_sheet.js",
    ),
    ManifestEntry(
        "balance_engine.js",
        ("output/js/balance_engine.js", "web/js/balance_engine.js"),
        "/output/js/balance_engine.js",
    ),
    ManifestEntry(
        "hud_state.js",
        ("output/js/hud_state.js", "web/js/hud_state.js"),
        "/output/js/hud_state.js",
    ),
    ManifestEntry(
        "top10_3d_viewer.js",
        ("output/js/top10_3d_viewer.js", "web/js/top10_3d_viewer.js"),
        "/output/js/top10_3d_viewer.js",
    ),
    ManifestEntry(
        "vessel_3d_reconstruction.js",
        ("output/js/vessel_3d_reconstruction.js", "web/js/vessel_3d_reconstruction.js"),
        "/output/js/vessel_3d_reconstruction.js",
    ),
    ManifestEntry(
        "top10_vessels_manifest.js",
        ("output/js/top10_vessels_manifest.js", "web/js/top10_vessels_manifest.js"),
        "/output/js/top10_vessels_manifest.js",
    ),
    ManifestEntry(
        "uaip_quant_visualizer.js",
        ("output/js/uaip_quant_visualizer.js", "web/js/uaip_quant_visualizer.js"),
        "/output/js/uaip_quant_visualizer.js",
    ),
    ManifestEntry(
        "route_analytics_engine.js",
        ("output/js/route_analytics_engine.js", "web/js/route_analytics_engine.js"),
        "/output/js/route_analytics_engine.js",
    ),
    ManifestEntry(
        "route_infographics.js",
        ("output/js/route_infographics.js", "web/js/route_infographics.js"),
        "/output/js/route_infographics.js",
    ),
    ManifestEntry(
        "sentinel_hud.css",
        ("output/js/sentinel_hud.css", "web/css/sentinel_hud.css"),
        "/output/js/sentinel_hud.css",
    ),
    ManifestEntry(
        "sentinel_premium.css",
        ("output/js/sentinel_premium.css", "web/sentinel_premium.css"),
        "/output/js/sentinel_premium.css",
    ),
)


# web SoT ↔ packaged output/js pairs (local drift guard).
WEB_OUTPUT_PAIRS: tuple[tuple[str, str], ...] = (
    ("web/sentinel_engine.js", "output/js/sentinel_engine.js"),
    ("web/js/top10_sheet.js", "output/js/top10_sheet.js"),
    ("web/js/map_tiles.js", "output/js/map_tiles.js"),
    ("web/js/route_map_view.js", "output/js/route_map_view.js"),
    ("web/js/route_sheet.js", "output/js/route_sheet.js"),
    ("web/js/arctic_sheet.js", "output/js/arctic_sheet.js"),
    ("web/js/arctic_vessels_manifest.js", "output/js/arctic_vessels_manifest.js"),
    ("web/js/archive_sheet.js", "output/js/archive_sheet.js"),
    ("web/js/balance_engine.js", "output/js/balance_engine.js"),
    ("web/js/hud_state.js", "output/js/hud_state.js"),
    ("web/js/top10_3d_viewer.js", "output/js/top10_3d_viewer.js"),
    ("web/js/vessel_3d_reconstruction.js", "output/js/vessel_3d_reconstruction.js"),
    ("web/js/top10_vessels_manifest.js", "output/js/top10_vessels_manifest.js"),
    ("web/js/uaip_quant_visualizer.js", "output/js/uaip_quant_visualizer.js"),
    ("web/css/sentinel_hud.css", "output/js/sentinel_hud.css"),
    ("web/sentinel_premium.css", "output/js/sentinel_premium.css"),
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_local(entry: ManifestEntry) -> Path | None:
    for rel in entry.local_paths:
        p = ROOT / rel
        if p.is_file():
            return p
    return None


def fetch_url(url: str, *, timeout: float = TIMEOUT_SEC) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "User-Agent": "sentinel-verify-deploy-manifest/1.0",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


@dataclass
class Row:
    name: str
    status: str  # MATCH | MISMATCH | MISSING_LOCAL | FETCH_ERROR | SKIP
    local_path: str | None = None
    local_sha: str | None = None
    remote_sha: str | None = None
    url: str | None = None
    detail: str | None = None


def check_local_web_output_sync() -> list[Row]:
    rows: list[Row] = []
    for web_rel, out_rel in WEB_OUTPUT_PAIRS:
        web = ROOT / web_rel
        out = ROOT / out_rel
        name = f"sync:{Path(web_rel).name}"
        if not web.is_file():
            rows.append(Row(name, "MISSING_LOCAL", local_path=web_rel, detail="web SoT missing"))
            continue
        if not out.is_file():
            rows.append(
                Row(
                    name,
                    "MISMATCH",
                    local_path=web_rel,
                    local_sha=sha256_file(web),
                    detail=f"output missing: {out_rel}",
                )
            )
            continue
        wa = sha256_file(web)
        oa = sha256_file(out)
        if wa == oa:
            rows.append(Row(name, "MATCH", local_path=web_rel, local_sha=wa, remote_sha=oa))
        else:
            rows.append(
                Row(
                    name,
                    "MISMATCH",
                    local_path=web_rel,
                    local_sha=wa,
                    remote_sha=oa,
                    detail=f"web!=output/js ({out_rel})",
                )
            )
    return rows


def check_live(base_url: str, entries: Iterable[ManifestEntry] | None = None) -> list[Row]:
    base = base_url.rstrip("/")
    rows: list[Row] = []
    for entry in entries or MANIFEST:
        local = resolve_local(entry)
        if local is None:
            rows.append(
                Row(
                    entry.name,
                    "MISSING_LOCAL",
                    detail=f"none of {list(entry.local_paths)} exist",
                )
            )
            continue
        local_sha = sha256_file(local)
        url = f"{base}{entry.url_path}?nocache={local_sha[:12]}"
        try:
            body = fetch_url(url)
            remote_sha = sha256_bytes(body)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            rows.append(
                Row(
                    entry.name,
                    "FETCH_ERROR",
                    local_path=str(local.relative_to(ROOT)).replace("\\", "/"),
                    local_sha=local_sha,
                    url=url,
                    detail=str(exc),
                )
            )
            continue
        status = "MATCH" if remote_sha == local_sha else "MISMATCH"
        rows.append(
            Row(
                entry.name,
                status,
                local_path=str(local.relative_to(ROOT)).replace("\\", "/"),
                local_sha=local_sha,
                remote_sha=remote_sha,
                url=url,
                detail=None if status == "MATCH" else "Node A bytes != git working tree",
            )
        )
    return rows


def summarize(rows: list[Row]) -> dict[str, int]:
    counts = {"MATCH": 0, "MISMATCH": 0, "MISSING_LOCAL": 0, "FETCH_ERROR": 0, "SKIP": 0}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    return counts


def print_report(title: str, rows: list[Row]) -> None:
    print(f"\n=== {title} ===")
    for r in rows:
        mark = {
            "MATCH": "OK  ",
            "MISMATCH": "FAIL",
            "MISSING_LOCAL": "FAIL",
            "FETCH_ERROR": "FAIL",
            "SKIP": "SKIP",
        }.get(r.status, r.status)
        extra = f"  {r.detail}" if r.detail else ""
        loc = f"  local={r.local_sha[:12]}..." if r.local_sha else ""
        rem = f"  remote={r.remote_sha[:12]}..." if r.remote_sha else ""
        print(f"  {mark} {r.name:32s} {r.status}{loc}{rem}{extra}")
    c = summarize(rows)
    print(
        f"  -> match={c.get('MATCH', 0)} mismatch={c.get('MISMATCH', 0)} "
        f"missing={c.get('MISSING_LOCAL', 0)} fetch_err={c.get('FETCH_ERROR', 0)}"
    )


def rows_to_dict(rows: list[Row]) -> list[dict]:
    return [
        {
            "name": r.name,
            "status": r.status,
            "local_path": r.local_path,
            "local_sha256": r.local_sha,
            "remote_sha256": r.remote_sha,
            "url": r.url,
            "detail": r.detail,
        }
        for r in rows
    ]


def run(
    *,
    base_url: str = DEFAULT_BASE,
    live: bool = True,
    local_sync: bool = True,
    strict: bool = False,
    unreachable_is_fail: bool = False,
) -> tuple[int, list[Row]]:
    """Return (exit_code, rows). Used by CLI and assert_out_of_box_contract."""
    all_rows: list[Row] = []

    if local_sync:
        sync_rows = check_local_web_output_sync()
        print_report("Local web/ <-> output/js sync", sync_rows)
        all_rows.extend(sync_rows)

    live_rows: list[Row] = []
    if live:
        print(f"\nLive base: {base_url}")
        live_rows = check_live(base_url)
        print_report("Node A served bytes vs working tree", live_rows)
        all_rows.extend(live_rows)

    bad = [r for r in all_rows if r.status in {"MISMATCH", "MISSING_LOCAL"}]
    fetch_errors = [r for r in live_rows if r.status == "FETCH_ERROR"]

    if bad:
        print("\nStale / drifted files:")
        for r in bad:
            print(f"  - {r.name}: {r.detail or r.status}")
            if r.local_sha and r.remote_sha:
                print(f"      local  {r.local_sha}")
                print(f"      remote {r.remote_sha}")

    if bad:
        print("\nDEPLOY MANIFEST FAIL")
        return 1, all_rows

    if fetch_errors:
        msg = (
            "Node A unreachable/partial — "
            "set SENTINEL_VERIFY_LIVE=1 or --strict to fail the contract"
        )
        if unreachable_is_fail or strict:
            print(f"\nDEPLOY MANIFEST FAIL ({msg})")
            return 1, all_rows
        print(f"\n  WARN {msg}")
        print("\nDEPLOY MANIFEST PASS (with live-unreachable warnings)")
        return 0, all_rows

    print("\nDEPLOY MANIFEST PASS")
    return 0, all_rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Verify HUD deploy manifest (git tree vs Node A)")
    ap.add_argument(
        "--base-url",
        default=os.environ.get("SENTINEL_VERIFY_BASE_URL", DEFAULT_BASE),
        help="Node A HUD origin (default http://$SENTINEL_NODE_A:8765)",
    )
    ap.add_argument(
        "--live",
        action="store_true",
        help="Force live Node A check (default: on unless --local-only)",
    )
    ap.add_argument("--local-only", action="store_true", help="Only web↔output/js sync")
    ap.add_argument("--no-local-sync", action="store_true", help="Skip web↔output check")
    ap.add_argument(
        "--strict",
        action="store_true",
        default=os.environ.get("SENTINEL_VERIFY_STRICT", "").strip().lower() in {"1", "true", "yes"},
        help="Fail when Node A unreachable",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON summary to stdout after report",
    )
    args = ap.parse_args(argv)

    force_live = os.environ.get("SENTINEL_VERIFY_LIVE", "").strip().lower() in {"1", "true", "yes"}
    live = False if args.local_only else True
    strict = bool(args.strict) or force_live

    rc, rows = run(
        base_url=str(args.base_url),
        live=live,
        local_sync=not args.no_local_sync,
        strict=strict,
        unreachable_is_fail=strict,
    )

    if args.json:
        payload = {
            "base_url": args.base_url,
            "rows": rows_to_dict(rows),
            "counts": summarize(rows),
            "exit_code": rc,
        }
        print(json.dumps(payload, indent=2))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
