"""OOB health overlay — Contract 1.8.0-ops-gis-sot.

Augments Dual Gate health with provider key status (masked), disk headroom,
AIS live-cache freshness, and Node A sync hints. Never embeds raw secrets.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_VERSION = "1.8.0-ops-gis-sot"
AIS_LIVE = ROOT / "data" / "cache" / "ais_live.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def disk_status(path: Path | None = None) -> dict[str, Any]:
    target = path or ROOT
    try:
        usage = shutil.disk_usage(target)
        free_pct = round(100.0 * usage.free / max(usage.total, 1), 1)
        return {
            "path": str(target),
            "total_gb": round(usage.total / (1024**3), 2),
            "free_gb": round(usage.free / (1024**3), 2),
            "disk_free_pct": free_pct,
            "ok": free_pct >= 10.0,
        }
    except OSError as exc:
        return {"ok": False, "error": str(exc)[:160]}


def provider_plane() -> dict[str, Any]:
    try:
        from services.config_keys import registry_status

        st = registry_status()
        return {
            "contract_version": CONTRACT_VERSION,
            "configured": st["configured"],
            "total": st["total"],
            "keys": st["keys"],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "contract_version": CONTRACT_VERSION,
            "configured": 0,
            "total": 7,
            "keys": [],
            "ok": False,
            "error": str(exc)[:160],
        }


def ais_live_cache_status() -> dict[str, Any]:
    if not AIS_LIVE.is_file():
        return {"present": False, "ok": False, "path": str(AIS_LIVE).replace("\\", "/")}
    try:
        data = json.loads(AIS_LIVE.read_text(encoding="utf-8"))
        updated = data.get("updated_at") or data.get("fetched_at")
        age_sec = None
        if updated:
            try:
                # Accept ...Z
                ts = updated.replace("Z", "+00:00")
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age_sec = round((datetime.now(timezone.utc) - dt).total_seconds(), 1)
            except ValueError:
                age_sec = None
        return {
            "present": True,
            "ok": True,
            "count": data.get("count"),
            "source": data.get("source"),
            "updated_at": updated,
            "age_sec": age_sec,
            "path": str(AIS_LIVE).replace("\\", "/"),
        }
    except Exception as exc:  # noqa: BLE001
        return {"present": True, "ok": False, "error": str(exc)[:160]}


def node_sync_status() -> dict[str, Any]:
    """Best-effort Node A sync hint from env / local markers (no SSH here)."""
    active = (os.getenv("SENTINEL_ACTIVE_NODE") or os.getenv("ACTIVE_NODE") or "korolev").strip()
    verify_live = (os.getenv("SENTINEL_VERIFY_LIVE") or "").strip() in ("1", "true", "yes")
    manifest = ROOT / "output" / "deploy_manifest.json"
    return {
        "active_node": active.lower(),
        "verify_live_requested": verify_live,
        "deploy_manifest_present": manifest.is_file(),
        "deploy_manifest_mtime": (
            datetime.fromtimestamp(manifest.stat().st_mtime, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            if manifest.is_file()
            else None
        ),
        "note": "Full Node A byte-diff is scripts/verify_deploy_manifest.py --live",
    }


def build_oob_health_overlay() -> dict[str, Any]:
    return {
        "oob": {
            "contract_version": CONTRACT_VERSION,
            "generated_at": _now_iso(),
            "providers": provider_plane(),
            "disk": disk_status(),
            "ais_live_cache": ais_live_cache_status(),
            "node_sync": node_sync_status(),
        }
    }


def attach_oob_plane(doc: dict[str, Any]) -> dict[str, Any]:
    """Mutate/return health document with OOB overlay (safe if called twice)."""
    overlay = build_oob_health_overlay()
    doc.update(overlay)
    # Convenience aliases at top-level for HUD probes
    oob = overlay["oob"]
    if "disk_free_pct" not in doc and isinstance(oob.get("disk"), dict):
        doc.setdefault("disk_free_pct", oob["disk"].get("disk_free_pct"))
    return doc


__all__ = (
    "CONTRACT_VERSION",
    "build_oob_health_overlay",
    "attach_oob_plane",
    "provider_plane",
    "disk_status",
    "ais_live_cache_status",
    "node_sync_status",
)
