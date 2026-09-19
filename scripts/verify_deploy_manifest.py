#!/usr/bin/env python3
"""Deploy Manifest Verification — auto-discovery (closes stale-on-Node-A class).

Discovers HUD-critical assets via globs so new files under web/js, web/css,
services/, arctic videos, and 3D GLBs are verified WITHOUT hand-editing a list.

Compares sha256 of the git working tree against bytes served by Node A (:8765).
Also detects local web/ ↔ output/ drift. Service .py files are checked against
the remote /output/deploy_manifest.json snapshot (no public HTTP for /app/services).

Usage:
  python scripts/verify_deploy_manifest.py
  python scripts/verify_deploy_manifest.py --live --strict
  python scripts/verify_deploy_manifest.py --base-url http://45.8.230.214:8765
  python scripts/verify_deploy_manifest.py --local-only
  python scripts/verify_deploy_manifest.py --list-discovered

Env:
  SENTINEL_NODE_A          default host (45.8.230.214)
  SENTINEL_VERIFY_LIVE=1   force live check (strict when unreachable → fail)
  SENTINEL_VERIFY_STRICT=1 mismatch / unreachable → exit 1
"""

from __future__ import annotations

import argparse
import fnmatch
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
REMOTE_MANIFEST_PATH = "/output/deploy_manifest.json"

# ---------------------------------------------------------------------------
# Auto-discovery rules — includes grow with the tree; excludes stay explicit.
# ---------------------------------------------------------------------------

EXCLUDE_GLOBS: tuple[str, ...] = (
    "**/node_modules/**",
    "**/__pycache__/**",
    "**/tests/**",
    "**/test_*.py",
    "**/*_test.py",
    "**/*validation*",
    "**/*_source.mp4",
    "**/.git/**",
    "**/venv/**",
)

# Basenames that MUST remain covered after switching from the old hard list
# (regression lock — do not shrink coverage when adding globs).
LEGACY_REQUIRED_BASENAMES: frozenset[str] = frozenset(
    {
        "sentinel_engine.js",
        "top10_sheet.js",
        "vessel_card_metrics.js",
        "oracle_engine.js",
        "oracle_sheet.js",
        "oracle_event_bus.js",
        "map_tiles.js",
        "route_map_view.js",
        "route_sheet.js",
        "arctic_sheet.js",
        "arctic_vessels_manifest.js",
        "vessel_9737187.mp4",
        "vessel_9750749.mp4",
        "archive_sheet.js",
        "balance_engine.js",
        "hud_state.js",
        "top10_3d_viewer.js",
        "vessel_3d_reconstruction.js",
        "top10_vessels_manifest.js",
        "uaip_quant_visualizer.js",
        "route_analytics_engine.js",
        "route_infographics.js",
        "sentinel_hud.css",
        "sentinel_premium.css",
        "oracle_engine.py",
        "dual_gate.py",
        "quant_risk_service.py",
    }
)

CRITICAL_BASENAMES: frozenset[str] = frozenset(
    {
        "oracle_engine.js",
        "oracle_sheet.js",
        "oracle_event_bus.js",
        "oracle_engine.py",
        "vessel_card_metrics.js",
        "sentinel_hud.css",
        "top10_sheet.js",
        "arctic_sheet.js",
        "sentinel_engine.js",
        "dual_gate.py",
        "quant_risk_service.py",
        "ais_health.py",
        "top10_vessels_manifest.js",
    }
)


@dataclass(frozen=True)
class ManifestEntry:
    """One deploy-critical asset.

    local_paths: ordered candidates; first existing file is the expected hash SoT.
    url_path: public HUD path, or None for image/manifest-only (services/*.py).
    kind: discovery class tag.
    """

    name: str
    local_paths: tuple[str, ...]
    url_path: str | None
    kind: str = "hud"


def _posix(rel: Path | str) -> str:
    return str(rel).replace("\\", "/")


def _is_excluded(rel_posix: str) -> bool:
    for pat in EXCLUDE_GLOBS:
        if fnmatch.fnmatch(rel_posix, pat) or fnmatch.fnmatch(Path(rel_posix).name, pat):
            return True
        # Also match basename-only patterns like test_*.py
        if fnmatch.fnmatch(Path(rel_posix).name, pat.lstrip("*/")):
            return True
    return False


def _iter_glob(pattern: str) -> list[Path]:
    """Glob under ROOT; pattern uses / separators."""
    matches: list[Path] = []
    for p in ROOT.glob(pattern):
        if not p.is_file():
            continue
        rel = _posix(p.relative_to(ROOT))
        if _is_excluded(rel):
            continue
        matches.append(p)
    return sorted(matches, key=lambda x: _posix(x.relative_to(ROOT)))


def _entry_for_web_js(src: Path) -> ManifestEntry:
    rel = _posix(src.relative_to(ROOT))
    name = src.name
    # Prefer web/js SoT; thin re-export web/oracle_sheet.js → output/oracle_sheet.js
    if rel.startswith("web/js/"):
        return ManifestEntry(
            name=name,
            local_paths=(f"output/js/{name}", f"web/js/{name}"),
            url_path=f"/output/js/{name}",
            kind="hud_js",
        )
    # web/*.js at root
    if name == "oracle_sheet.js":
        return ManifestEntry(
            name="oracle_sheet.js.root",
            local_paths=("output/oracle_sheet.js", "web/oracle_sheet.js"),
            url_path="/output/oracle_sheet.js",
            kind="hud_js_root",
        )
    return ManifestEntry(
        name=name,
        local_paths=(f"output/js/{name}", f"web/{name}"),
        url_path=f"/output/js/{name}",
        kind="hud_js_root",
    )


def _entry_for_web_css(src: Path) -> ManifestEntry:
    name = src.name
    rel = _posix(src.relative_to(ROOT))
    if rel.startswith("web/css/"):
        # Dual-deploy: HTML hrefs often point at /output/js/<css>
        return ManifestEntry(
            name=name,
            local_paths=(f"output/js/{name}", f"output/css/{name}", f"web/css/{name}"),
            url_path=f"/output/js/{name}",
            kind="hud_css",
        )
    return ManifestEntry(
        name=name,
        local_paths=(f"output/js/{name}", f"output/css/{name}", f"web/{name}"),
        url_path=f"/output/js/{name}",
        kind="hud_css_root",
    )


def _entry_for_service_py(src: Path) -> ManifestEntry:
    rel = _posix(src.relative_to(ROOT))
    return ManifestEntry(
        name=src.name if src.parent == ROOT / "services" else rel.replace("/", "__"),
        local_paths=(rel,),
        url_path=None,
        kind="service_py",
    )


def _entry_for_arctic_mp4(src: Path) -> ManifestEntry:
    name = src.name
    return ManifestEntry(
        name=f"arctic_video_{src.stem.split('_')[-1]}" if name.startswith("vessel_") else name,
        local_paths=(
            f"assets/arctic/videos/{name}",
            f"output/assets/arctic/videos/{name}",
        ),
        url_path=f"/assets/arctic/videos/{name}",
        kind="arctic_video",
    )


def _entry_for_glb(src: Path) -> ManifestEntry:
    name = src.name
    return ManifestEntry(
        name=name,
        local_paths=(
            f"output/assets/3d_models/{name}",
            f"web/assets/3d_models/{name}",
        ),
        url_path=f"/output/assets/3d_models/{name}",
        kind="glb",
    )


def discover_manifest_entries() -> tuple[ManifestEntry, ...]:
    """Build the live MANIFEST from globs (order stable, deduped by url/local)."""
    entries: list[ManifestEntry] = []
    seen_keys: set[str] = set()

    def _add(entry: ManifestEntry) -> None:
        key = entry.url_path or f"local:{entry.local_paths[0]}"
        if key in seen_keys:
            return
        # Prefer web/js over web/ root for same basename url
        seen_keys.add(key)
        entries.append(entry)

    for p in _iter_glob("web/js/**/*.js"):
        _add(_entry_for_web_js(p))
    for p in _iter_glob("web/*.js"):
        # Skip if same basename already covered by web/js (except oracle root mirror)
        if p.name != "oracle_sheet.js" and any(
            e.name == p.name and e.kind == "hud_js" for e in entries
        ):
            continue
        _add(_entry_for_web_js(p))
    for p in _iter_glob("web/css/**/*.css"):
        _add(_entry_for_web_css(p))
    for p in _iter_glob("web/*.css"):
        _add(_entry_for_web_css(p))
    for p in _iter_glob("services/**/*.py"):
        _add(_entry_for_service_py(p))
    for p in _iter_glob("assets/arctic/videos/*.mp4"):
        _add(_entry_for_arctic_mp4(p))
    for p in _iter_glob("web/assets/3d_models/*.glb"):
        _add(_entry_for_glb(p))

    return tuple(entries)


def discover_web_output_pairs() -> tuple[tuple[str, str], ...]:
    """Auto web SoT ↔ packaged output pairs (local drift guard)."""
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(web_rel: str, out_rel: str) -> None:
        if web_rel in seen:
            return
        if not (ROOT / web_rel).is_file():
            return
        seen.add(web_rel)
        pairs.append((web_rel, out_rel))

    for p in _iter_glob("web/js/**/*.js"):
        name = p.name
        _add(f"web/js/{name}", f"output/js/{name}")
    for p in _iter_glob("web/*.js"):
        name = p.name
        if name == "oracle_sheet.js" and (ROOT / "web" / "js" / "oracle_sheet.js").is_file():
            _add("web/oracle_sheet.js", "output/oracle_sheet.js")
            continue
        if (ROOT / "web" / "js" / name).is_file():
            continue
        _add(f"web/{name}", f"output/js/{name}")
    for p in _iter_glob("web/css/**/*.css"):
        name = p.name
        _add(f"web/css/{name}", f"output/js/{name}")
    for p in _iter_glob("web/*.css"):
        name = p.name
        _add(f"web/{name}", f"output/js/{name}")
    return tuple(pairs)


# Public aliases — rebuilt on import so callers always see current tree.
MANIFEST: tuple[ManifestEntry, ...] = discover_manifest_entries()
WEB_OUTPUT_PAIRS: tuple[tuple[str, str], ...] = discover_web_output_pairs()


def refresh_discovered() -> None:
    """Re-run discovery (tests / write_deploy_manifest after tree changes)."""
    global MANIFEST, WEB_OUTPUT_PAIRS
    MANIFEST = discover_manifest_entries()
    WEB_OUTPUT_PAIRS = discover_web_output_pairs()


def assert_legacy_coverage(entries: Iterable[ManifestEntry] | None = None) -> list[str]:
    """Return list of LEGACY_REQUIRED_BASENAMES missing from discovery."""
    ents = list(entries or MANIFEST)
    found: set[str] = set()
    for e in ents:
        for lp in e.local_paths:
            found.add(Path(lp).name)
        found.add(e.name)
        # arctic_video_* aliases
        if e.kind == "arctic_video":
            for lp in e.local_paths:
                found.add(Path(lp).name)
    missing = sorted(b for b in LEGACY_REQUIRED_BASENAMES if b not in found)
    return missing


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
            "User-Agent": "sentinel-verify-deploy-manifest/2.0-autodiscover",
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
    kind: str | None = None


def check_local_web_output_sync(
    pairs: Iterable[tuple[str, str]] | None = None,
) -> list[Row]:
    rows: list[Row] = []
    for web_rel, out_rel in pairs or WEB_OUTPUT_PAIRS:
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
                    detail=f"web!=output ({out_rel})",
                )
            )
    return rows


def _index_remote_manifest(doc: dict) -> dict[str, dict]:
    """Map basename / resolved_path / url_path → asset dict."""
    idx: dict[str, dict] = {}
    for asset in doc.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        for key in (
            asset.get("name"),
            asset.get("resolved_path"),
            asset.get("url_path"),
            Path(str(asset.get("resolved_path") or "")).name,
        ):
            if key:
                idx[str(key)] = asset
        for lp in asset.get("local_paths") or []:
            idx[str(lp)] = asset
            idx[Path(str(lp)).name] = asset
    return idx


def check_live(
    base_url: str,
    entries: Iterable[ManifestEntry] | None = None,
) -> list[Row]:
    base = base_url.rstrip("/")
    rows: list[Row] = []
    remote_idx: dict[str, dict] = {}
    remote_manifest_err: str | None = None
    try:
        body = fetch_url(f"{base}{REMOTE_MANIFEST_PATH}?nocache=1")
        remote_idx = _index_remote_manifest(json.loads(body.decode("utf-8", errors="replace")))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        remote_manifest_err = str(exc)

    for entry in entries or MANIFEST:
        local = resolve_local(entry)
        if local is None:
            rows.append(
                Row(
                    entry.name,
                    "MISSING_LOCAL",
                    detail=f"none of {list(entry.local_paths)} exist",
                    kind=entry.kind,
                )
            )
            continue
        local_sha = sha256_file(local)
        local_rel = _posix(local.relative_to(ROOT))

        # --- services/*.py: compare against Node A deploy_manifest snapshot ---
        if entry.url_path is None:
            if remote_manifest_err:
                rows.append(
                    Row(
                        entry.name,
                        "FETCH_ERROR",
                        local_path=local_rel,
                        local_sha=local_sha,
                        url=f"{base}{REMOTE_MANIFEST_PATH}",
                        detail=f"remote deploy_manifest: {remote_manifest_err}",
                        kind=entry.kind,
                    )
                )
                continue
            asset = (
                remote_idx.get(entry.name)
                or remote_idx.get(local_rel)
                or remote_idx.get(Path(local_rel).name)
            )
            if asset is None:
                rows.append(
                    Row(
                        entry.name,
                        "MISMATCH",
                        local_path=local_rel,
                        local_sha=local_sha,
                        detail="MISSING on Node A deploy_manifest (auto-discovered locally)",
                        kind=entry.kind,
                    )
                )
                continue
            remote_sha = asset.get("sha256")
            if not remote_sha:
                rows.append(
                    Row(
                        entry.name,
                        "MISMATCH",
                        local_path=local_rel,
                        local_sha=local_sha,
                        detail="Node A manifest entry has no sha256",
                        kind=entry.kind,
                    )
                )
                continue
            status = "MATCH" if remote_sha == local_sha else "MISMATCH"
            rows.append(
                Row(
                    entry.name,
                    status,
                    local_path=local_rel,
                    local_sha=local_sha,
                    remote_sha=remote_sha,
                    url=f"{base}{REMOTE_MANIFEST_PATH}",
                    detail=None if status == "MATCH" else "Node A manifest sha != git working tree",
                    kind=entry.kind,
                )
            )
            continue

        # --- HTTP-served HUD assets ---
        url = f"{base}{entry.url_path}?nocache={local_sha[:12]}"
        try:
            body = fetch_url(url)
            remote_sha = sha256_bytes(body)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            rows.append(
                Row(
                    entry.name,
                    "FETCH_ERROR",
                    local_path=local_rel,
                    local_sha=local_sha,
                    url=url,
                    detail=str(exc),
                    kind=entry.kind,
                )
            )
            continue
        status = "MATCH" if remote_sha == local_sha else "MISMATCH"
        rows.append(
            Row(
                entry.name,
                status,
                local_path=local_rel,
                local_sha=local_sha,
                remote_sha=remote_sha,
                url=url,
                detail=None if status == "MATCH" else "Node A bytes != git working tree",
                kind=entry.kind,
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
        print(f"  {mark} {r.name:40s} {r.status}{loc}{rem}{extra}")
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
            "kind": r.kind,
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
    refresh_discovered()
    all_rows: list[Row] = []

    legacy_missing = assert_legacy_coverage()
    if legacy_missing:
        print("\n=== Legacy regression coverage ===")
        print(f"  FAIL discovery dropped required basenames: {legacy_missing}")
        all_rows.append(
            Row(
                "legacy_coverage",
                "MISMATCH",
                detail=f"missing: {legacy_missing}",
            )
        )
    else:
        print(
            f"\n=== Legacy regression coverage ===\n"
            f"  OK   all {len(LEGACY_REQUIRED_BASENAMES)} required basenames discovered "
            f"(total entries={len(MANIFEST)})"
        )

    if local_sync:
        sync_rows = check_local_web_output_sync()
        print_report("Local web/ <-> output sync (auto)", sync_rows)
        all_rows.extend(sync_rows)

    live_rows: list[Row] = []
    if live:
        print(f"\nLive base: {base_url}")
        print(f"Discovered entries: {len(MANIFEST)} (auto-glob)")
        live_rows = check_live(base_url)
        print_report("Node A served bytes / manifest vs working tree", live_rows)
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
    ap = argparse.ArgumentParser(
        description="Verify HUD deploy manifest (auto-discovery git tree vs Node A)"
    )
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
    ap.add_argument("--local-only", action="store_true", help="Only web↔output sync + discovery")
    ap.add_argument("--no-local-sync", action="store_true", help="Skip web↔output check")
    ap.add_argument(
        "--strict",
        action="store_true",
        default=os.environ.get("SENTINEL_VERIFY_STRICT", "").strip().lower() in {"1", "true", "yes"},
        help="Fail when Node A unreachable",
    )
    ap.add_argument(
        "--list-discovered",
        action="store_true",
        help="Print auto-discovered entries and exit 0",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON summary to stdout after report",
    )
    args = ap.parse_args(argv)

    refresh_discovered()

    if args.list_discovered:
        print(f"discovered={len(MANIFEST)} pairs={len(WEB_OUTPUT_PAIRS)}")
        for e in MANIFEST:
            print(f"  [{e.kind:14s}] {e.name:40s} url={e.url_path or '-'}  local={e.local_paths[0]}")
        miss = assert_legacy_coverage()
        if miss:
            print(f"LEGACY_GAP {miss}")
            return 1
        print("LEGACY_COVERAGE_OK")
        return 0

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
            "discovery": "auto-glob",
            "manifest_count": len(MANIFEST),
            "rows": rows_to_dict(rows),
            "counts": summarize(rows),
            "exit_code": rc,
            "legacy_missing": assert_legacy_coverage(),
        }
        print(json.dumps(payload, indent=2))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
