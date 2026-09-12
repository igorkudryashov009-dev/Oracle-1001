"""Q-Flex Digital Twin Fleet — catalog + reference-image manifest (Oracle-1001 / Sentinel).

Canonical sheet id remains ``top10`` / ``?sheet=top10`` (URL stable). User-facing brand: Q-Flex Fleet.
Rank/IMO SoT must match the Q-Flex flight table (incl. Rank 10 = AL MAYEDA / 9397298).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import os as _os

ROOT = Path(__file__).resolve().parents[1]

def _resolve_ref_base() -> Path:
    """Env → repo-relative → ~/Desktop (no hardcoded C:\\Users\\...)."""
    env_path = _os.environ.get("ASSETS_7000_DIR", "").strip()
    if env_path and not (env_path[1:3] in (":\\", ":/") or env_path.startswith("\\\\")):
        return Path(env_path)
    if env_path and (_os.environ.get("APP_HOME") or Path("/.dockerenv").exists()):
        # Inside container: ignore Windows host paths injected by mistake
        return ROOT / "assets" / "7000"
    if env_path:
        return Path(env_path)
    repo_relative = ROOT / "assets" / "7000"
    if repo_relative.exists() or _os.environ.get("APP_HOME"):
        return repo_relative
    home = Path.home()
    for cand in (home / "OneDrive" / "Desktop" / "7000", home / "Desktop" / "7000"):
        if cand.exists():
            return cand
    return repo_relative


REF_BASE = _resolve_ref_base()
ASSET_OUT = ROOT / "output" / "assets" / "top10"
WEB_JS = ROOT / "web" / "js"
OUT_JS = ROOT / "output" / "js"
# Isolated LUMA.ai clip — AL GHUWAIRIYA (9372743) legacy path. Prefer vessel_{imo}_flight.mp4.
LUMA_VIDEO_IMO = "9372743"
LUMA_VIDEO_FIDELITY = (
    "AI-GENERATED VIDEO RECONSTRUCTION (LUMA.ai) · SPECULATIVE DETAIL · "
    "GEOMETRY AND MOTION NOT OSINT-VERIFIED"
)
FLIGHT_VIDEO_FIDELITY = (
    "Q-FLEX DIGITAL-TWIN FLIGHT LOOP · OPTIMIZED H.264 · MOTION NOT OSINT-VERIFIED"
)
# Fleet brand (UI). Internal module/URL ids keep top10 for compatibility.
QFLEX_FLEET_BRAND = "Q-Flex Digital Twin & Video Fleet"
QFLEX_FLEET_SHORT = "Q-Flex"

# Strict flight SoT — filenames under ASSETS_1_10_DIR (default C:\\111\\1001\\1-10).
QFLEX_FLIGHT_FILES: dict[str, str] = {
    "9388833": "bu-samra-parallel-flight-1.mp4",
    "9397303": "mekaines-parallel-flight-2.mp4",
    "9397315": "al-mafyar-parallel-flight-3.mp4",
    "9397327": "al-kharaitiyat-parallel-flight-4.mp4",
    "9337755": "mozah-parallel-flight-5.mp4",
    "9372731": "umm-slal-parallel-flight-6.mp4",
    "9372743": "al-ghuwairiya-digital-twin-flight-7.mp4",
    "9388819": "lijmiliya-parallel-flight-8.mp4",
    "9388821": "al-samriya-parallel-flight-9.mp4",
    "9397298": "al-mayeda-digital-twin-flight-10.mp4",
}


def _resolve_qflex_source_dir() -> Path:
    """Host path C:\\111\\1001\\1-10 or Docker /app/assets/1-10 via ASSETS_1_10_DIR."""
    env_path = _os.environ.get("ASSETS_1_10_DIR", "").strip()
    in_docker = bool(_os.environ.get("APP_HOME") or Path("/.dockerenv").exists())
    if env_path:
        # Ignore Windows host path injected into Linux containers
        if in_docker and (env_path[1:3] in (":\\", ":/") or env_path.startswith("\\\\")):
            pass
        else:
            return Path(env_path)
    if in_docker:
        docker_path = Path("/app/assets/1-10")
        if docker_path.is_dir():
            return docker_path
    host_default = Path(r"C:\111\1001\1-10")
    if host_default.is_dir():
        return host_default
    repo_copy = ROOT / "assets" / "1-10"
    return repo_copy


QFLEX_SOURCE_DIR = _resolve_qflex_source_dir()
LUMA_VIDEO_OUT = ROOT / "output" / "assets" / "videos" / f"vessel_{LUMA_VIDEO_IMO}_luma.mp4"
LUMA_VIDEO_ASSET = ROOT / "assets" / "7000" / "videos" / f"vessel_{LUMA_VIDEO_IMO}_luma.mp4"
LUMA_VIDEO_SOURCE = ROOT / "assets" / "7000" / "videos" / f"vessel_{LUMA_VIDEO_IMO}_luma_source.mp4"
FLIGHT_VIDEO_ASSET_DIR = ROOT / "assets" / "7000" / "videos"
FLIGHT_VIDEO_OUT_DIR = ROOT / "output" / "assets" / "videos"

# Fleet identity — Q-Max / Membrane class · LOA≈345 m · Beam≈53.8 m
TOP10_VESSELS: list[dict[str, Any]] = [
    {
        "rank": 1,
        "imo": "9388833",
        "name": "BU SAMRA",
        "class": "Q-Max / Membrane",
        "loa_m": 345.28,
        "beam_m": 53.83,
        "draft_m": 9.4,
        "dwt_tons": 159750,
        "ais_integrity_pct": 97.4,
        "destination_risk": "LOW",
        "status": "ACTIVE OSINT TRACK",
        "flag": "Marshall Islands",
    },
    {
        "rank": 2,
        "imo": "9397303",
        "name": "MEKAINES",
        "class": "Q-Max / Membrane",
        "loa_m": 345.0,
        "beam_m": 53.8,
        "draft_m": 9.4,
        "dwt_tons": 136740,
        "ais_integrity_pct": 88.2,
        "destination_risk": "EXTREME",
        "status": "ACTIVE OSINT TRACK",
        "flag": "Marshall Islands",
    },
    {
        "rank": 3,
        "imo": "9397315",
        "name": "AL MAFYAR",
        "class": "Q-Max / Membrane",
        "loa_m": 345.28,
        "beam_m": 53.8,
        "draft_m": 9.4,
        "dwt_tons": 130441,
        "ais_integrity_pct": 95.1,
        "destination_risk": "LOW",
        "status": "ACTIVE OSINT TRACK",
        "flag": "Marshall Islands",
    },
    {
        "rank": 4,
        "imo": "9397327",
        "name": "AL KHARAITIYAT",
        "class": "Q-Flex / Membrane",
        "loa_m": 315.16,
        "beam_m": 50.0,
        "draft_m": 12.4,
        "dwt_tons": 172050,
        "ais_integrity_pct": 91.6,
        "destination_risk": "HIGH",
        "status": "ACTIVE OSINT TRACK",
        "flag": "Marshall Islands",
    },
    {
        "rank": 5,
        "imo": "9337755",
        "name": "MOZAH",
        "class": "Q-Max / Membrane",
        "loa_m": 345.33,
        "beam_m": 53.8,
        "draft_m": 9.5,
        "dwt_tons": 130102,
        "ais_integrity_pct": 98.0,
        "destination_risk": "LOW",
        "status": "ACTIVE OSINT TRACK",
        "flag": "Marshall Islands",
    },
    {
        "rank": 6,
        "imo": "9372731",
        "name": "UMM SLAL",
        "class": "Q-Max / Membrane",
        "loa_m": 345.0,
        "beam_m": 53.83,
        "draft_m": 9.4,
        "dwt_tons": 130000,
        "ais_integrity_pct": 94.3,
        "destination_risk": "LOW",
        "status": "ACTIVE OSINT TRACK",
        "flag": "Marshall Islands",
    },
    {
        "rank": 7,
        "imo": "9372743",
        "name": "AL GHUWAIRIYA",
        "class": "Q-Max / Membrane",
        "loa_m": 345.0,
        "beam_m": 53.8,
        "draft_m": 9.4,
        "dwt_tons": 130000,
        "ais_integrity_pct": 93.0,
        "destination_risk": "MEDIUM",
        "status": "ACTIVE OSINT TRACK",
        "flag": "Marshall Islands",
    },
    {
        "rank": 8,
        "imo": "9388819",
        "name": "LIJMILIYA",
        "class": "Q-Max / Membrane",
        "loa_m": 345.0,
        "beam_m": 55.03,
        "draft_m": 9.2,
        "dwt_tons": 130000,
        "ais_integrity_pct": 96.2,
        "destination_risk": "LOW",
        "status": "ACTIVE OSINT TRACK",
        "flag": "Marshall Islands",
    },
    {
        "rank": 9,
        "imo": "9388821",
        "name": "AL SAMRIYA",
        "class": "Q-Max / Membrane",
        "loa_m": 345.0,
        "beam_m": 55.03,
        "draft_m": 9.4,
        "dwt_tons": 167300,
        "ais_integrity_pct": 95.8,
        "destination_risk": "LOW",
        "status": "ACTIVE OSINT TRACK",
        "flag": "Marshall Islands",
    },
    {
        "rank": 10,
        "imo": "9397298",
        "name": "AL MAYEDA",
        "class": "Q-Max / Membrane",
        "loa_m": 345.28,
        "beam_m": 53.8,
        "draft_m": 9.3,
        "dwt_tons": 69418,
        "ais_integrity_pct": 91.0,
        "destination_risk": "LOW",
        "status": "ACTIVE OSINT TRACK",
        "flag": "Marshall Islands",
    },
]


def ref_paths(rank: int) -> dict[str, Path]:
    """Absolute OneDrive orthographic triplet for integrity / sync."""
    return {
        "side": REF_BASE / f"{rank}-1.jpg",
        "bow": REF_BASE / f"{rank}-2.jpg",
        "overhead": REF_BASE / f"{rank}-3.jpg",
    }


def web_urls(rank: int) -> dict[str, str]:
    """HTTP URLs served via /assets/7000/ virtual mount (Desktop orthographics)."""
    return {
        "side": f"/assets/7000/{rank}-1.jpg",
        "bow": f"/assets/7000/{rank}-2.jpg",
        "overhead": f"/assets/7000/{rank}-3.jpg",
    }


def enrich_vessel(v: dict[str, Any]) -> dict[str, Any]:
    rank = int(v["rank"])
    paths = ref_paths(rank)
    urls = web_urls(rank)
    out = dict(v)
    out["refs"] = {
        "side": {
            "label": "Side Profile",
            "local": str(paths["side"]),
            "url": urls["side"],
            "fallback_url": f"assets/top10/{rank}-1.jpg",
        },
        "bow": {
            "label": "Front Bow",
            "local": str(paths["bow"]),
            "url": urls["bow"],
            "fallback_url": f"assets/top10/{rank}-2.jpg",
        },
        "overhead": {
            "label": "Top-Down Satellite",
            "local": str(paths["overhead"]),
            "url": urls["overhead"],
            "fallback_url": f"assets/top10/{rank}-3.jpg",
        },
    }
    imo = str(v["imo"])
    glb_web = ROOT / "web" / "assets" / "3d_models" / f"vessel_{imo}.glb"
    glb_out = ROOT / "output" / "assets" / "3d_models" / f"vessel_{imo}.glb"
    out["glb"] = {
        "path": str(glb_web if glb_web.exists() else glb_out),
        "url": f"/output/assets/3d_models/vessel_{imo}.glb",
        "ready": bool(glb_web.exists() or glb_out.exists()),
        "bytes": int(
            (glb_web if glb_web.exists() else glb_out).stat().st_size
            if (glb_web.exists() or glb_out.exists())
            else 0
        ),
    }
    vox_web = ROOT / "web" / "assets" / "3d_models" / f"vessel_{imo}_voxels.json"
    vox_out = ROOT / "output" / "assets" / "3d_models" / f"vessel_{imo}_voxels.json"
    vox_path = vox_web if vox_web.exists() else vox_out
    vox_ready = bool(vox_web.exists() or vox_out.exists())
    vox_n = 0
    if vox_ready:
        try:
            for candidate in (
                vox_path.with_suffix(".meta.json"),
                vox_out.with_suffix(".meta.json"),
                vox_web.with_suffix(".meta.json"),
            ):
                if candidate.exists():
                    vox_n = int(json.loads(candidate.read_text(encoding="utf-8")).get("n_occupied") or 0)
                    if vox_n:
                        break
            if not vox_n and vox_path.exists():
                # Fallback: peek payload (avoid full parse of huge base64 when possible)
                head = vox_path.read_text(encoding="utf-8")[:800]
                import re as _re

                m = _re.search(r'"n_occupied"\s*:\s*(\d+)', head)
                if m:
                    vox_n = int(m.group(1))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            vox_n = 0
    out["voxel_cubes"] = {
        "path": str(vox_path if vox_ready else vox_out),
        "url": f"/output/assets/3d_models/vessel_{imo}_voxels.json",
        "ready": vox_ready,
        "bytes": int(vox_path.stat().st_size) if vox_ready else 0,
        "n_occupied": vox_n,
        "alg": None,
        "palette_hex": None,
        "palette_k": None,
    }
    if vox_ready:
        try:
            for candidate in (
                vox_path.with_suffix(".meta.json"),
                vox_out.with_suffix(".meta.json"),
                vox_web.with_suffix(".meta.json"),
            ):
                if candidate.exists():
                    meta = json.loads(candidate.read_text(encoding="utf-8"))
                    out["voxel_cubes"]["alg"] = meta.get("alg")
                    out["voxel_cubes"]["palette_hex"] = meta.get("palette_hex")
                    pk = meta.get("palette_k")
                    if pk is None and meta.get("palette_hex"):
                        pk = len(meta.get("palette_hex") or [])
                    out["voxel_cubes"]["palette_k"] = pk
                    if not vox_n:
                        out["voxel_cubes"]["n_occupied"] = int(meta.get("n_occupied") or 0)
                    break
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    # Alias for prompt naming: TOP10_VESSELS[].voxel.palette
    out["voxel"] = {
        "palette": out["voxel_cubes"].get("palette_hex"),
        "n_occupied": out["voxel_cubes"].get("n_occupied"),
        "alg": out["voxel_cubes"].get("alg"),
        "palette_k": out["voxel_cubes"].get("palette_k"),
        "ready": vox_ready,
    }
    # Q-Flex REAL VIDEO — prefer /assets/1-10/{file}, fall back to optimized repo copies.
    flight_file = QFLEX_FLIGHT_FILES.get(imo)
    source_mp4 = (QFLEX_SOURCE_DIR / flight_file) if flight_file else None
    flight_asset = FLIGHT_VIDEO_ASSET_DIR / f"vessel_{imo}_flight.mp4"
    flight_out = FLIGHT_VIDEO_OUT_DIR / f"vessel_{imo}_flight.mp4"
    candidates = [
        p
        for p in (source_mp4, flight_asset, flight_out)
        if p is not None and p.exists() and p.stat().st_size > 10_000
    ]
    flight_path = candidates[0] if candidates else None
    flight_ready = flight_path is not None
    if flight_ready and flight_file:
        if source_mp4 is not None and flight_path == source_mp4:
            http_url = f"/assets/1-10/{flight_file}"
        elif flight_asset.exists():
            http_url = f"/assets/7000/videos/vessel_{imo}_flight.mp4"
        else:
            http_url = f"/output/assets/videos/vessel_{imo}_flight.mp4"
        local_display = str(source_mp4) if source_mp4 is not None else str(flight_path)
        out["flight_video"] = {
            "ready": True,
            "url": http_url,
            "http_url": http_url,
            "source_file": flight_file,
            "local_path": local_display,
            "real_video_path": local_display,
            "provider": "qflex_real_video",
            "kind": "real_video_flight",
            "fidelity_badge": FLIGHT_VIDEO_FIDELITY,
            "bytes": int(flight_path.stat().st_size),
            "imo": imo,
        }
        out["real_video_path"] = local_display
    # Generative LUMA clip: only IMO 9372743, only if compressed file exists.
    if imo == LUMA_VIDEO_IMO:
        luma_path = LUMA_VIDEO_OUT if LUMA_VIDEO_OUT.exists() else LUMA_VIDEO_ASSET
        luma_ready = luma_path.exists() and luma_path.stat().st_size > 10_000
        out["luma_video"] = {
            "ready": luma_ready,
            "url": f"/assets/7000/videos/vessel_{imo}_luma.mp4"
            if LUMA_VIDEO_ASSET.exists()
            else f"/output/assets/videos/vessel_{imo}_luma.mp4",
            "source_url": f"/assets/7000/videos/vessel_{imo}_luma_source.mp4",
            "provider": "luma.ai",
            "kind": "generative_video_reconstruction",
            "fidelity_badge": LUMA_VIDEO_FIDELITY,
            "bytes": int(luma_path.stat().st_size) if luma_ready else 0,
            "imo": imo,
        }
    return out


def build_catalog() -> list[dict[str, Any]]:
    return [enrich_vessel(v) for v in TOP10_VESSELS]


def sync_reference_assets(*, force: bool = True) -> dict[str, Any]:
    """Copy Desktop orthographic JPGs → output/assets/top10 for HTTP serving."""
    ASSET_OUT.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    missing: list[str] = []
    for v in TOP10_VESSELS:
        rank = int(v["rank"])
        for key, src in ref_paths(rank).items():
            dest = ASSET_OUT / f"{rank}-{1 if key == 'side' else 2 if key == 'bow' else 3}.jpg"
            if not src.exists():
                missing.append(str(src))
                continue
            if force or not dest.exists() or dest.stat().st_size != src.stat().st_size:
                shutil.copy2(src, dest)
            copied.append(dest.name)
    return {
        "ok": len(missing) == 0,
        "copied": len(copied),
        "missing": missing,
        "asset_dir": str(ASSET_OUT),
    }


def write_js_manifest() -> Path:
    """Emit ES module consumed by the TOP10 sheet."""
    catalog = build_catalog()
    # TOP10_REF_BASE is kept as a portable sentinel string — no hardcoded OS paths
    # in the browser bundle. HTTP assets are served via /assets/7000/ virtual mount.
    body = (
        "/** Auto-generated by services/top10_vessels.py — do not edit by hand. */\n"
        "// Q-Flex Digital Twin Fleet (sheet id: top10). Assets via /assets/7000/.\n"
        "export const TOP10_REF_BASE = \"/assets/7000\";\n"
        "export const QFLEX_FLEET_BRAND = "
        + json.dumps(QFLEX_FLEET_BRAND, ensure_ascii=False)
        + ";\n"
        "export const QFLEX_FLEET_SHORT = "
        + json.dumps(QFLEX_FLEET_SHORT, ensure_ascii=False)
        + ";\n"
        "export const QFLEX_FLIGHT_FILES = "
        + json.dumps(QFLEX_FLIGHT_FILES, ensure_ascii=False, indent=2)
        + ";\n"
        "export const QFLEX_SOURCE_DIR = "
        + json.dumps(str(QFLEX_SOURCE_DIR), ensure_ascii=False)
        + ";\n"
        "export const TOP10_VESSELS = "
        + json.dumps(catalog, ensure_ascii=False, indent=2)
        + ";\n"
        "export const QFLEX_VESSELS = TOP10_VESSELS;\n"
        "export default TOP10_VESSELS;\n"
        "if (typeof window !== 'undefined') {\n"
        "  window.__TOP10_VESSELS__ = TOP10_VESSELS;\n"
        "  window.__QFLEX_VESSELS__ = TOP10_VESSELS;\n"
        "  window.__QFLEX_FLEET_BRAND__ = QFLEX_FLEET_BRAND;\n"
        "}\n"
    )
    WEB_JS.mkdir(parents=True, exist_ok=True)
    OUT_JS.mkdir(parents=True, exist_ok=True)
    web_path = WEB_JS / "top10_vessels_manifest.js"
    out_path = OUT_JS / "top10_vessels_manifest.js"
    web_path.write_text(body, encoding="utf-8")
    out_path.write_text(body, encoding="utf-8")
    return web_path


def assert_reference_images_exist() -> dict[str, Any]:
    """Hard gate: all 10×3 orthographic JPGs must exist on disk."""
    missing: list[str] = []
    present = 0
    for v in TOP10_VESSELS:
        for p in ref_paths(int(v["rank"])).values():
            if p.exists() and p.stat().st_size > 1000:
                present += 1
            else:
                missing.append(str(p))
    if missing:
        from services.ttf_forecast.integrity import SREBuildError

        raise SREBuildError(
            "TOP10_REF_MISSING",
            f"missing {len(missing)} orthographic refs (need 30): {missing[:5]}...",
        )
    if len(TOP10_VESSELS) != 10:
        from services.ttf_forecast.integrity import SREBuildError

        raise SREBuildError("TOP10_COUNT", f"expected 10 vessels, got {len(TOP10_VESSELS)}")
    return {"ok": True, "vessels": 10, "images": present, "ref_base": str(REF_BASE)}
