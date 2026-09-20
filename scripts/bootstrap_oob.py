"""OOB zero-touch bootstrap — Contract 1.8.0-ops-gis-sot.

Ensures cold-start directories, validates ``.env`` against ``config_keys``,
and seeds offline JSON mocks when remote APIs have never been contacted.

Usage::

    python scripts/bootstrap_oob.py
    python scripts/bootstrap_oob.py --strict
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONTRACT_VERSION = "1.8.0-ops-gis-sot"

REQUIRED_DIRS = (
    ROOT / "logs",
    ROOT / "output" / "archive",
    ROOT / "output" / "cache",
    ROOT / "data" / "cache",
    ROOT / "data" / "archive",
    ROOT / "data" / "db",
)

MOCK_NEWS = {
    "ok": True,
    "contract_version": CONTRACT_VERSION,
    "fetched_at": None,
    "topics": ["LNG", "Nord Stream", "Gas Pipeline", "Baltic", "TTF"],
    "sources_ok": [],
    "count": 1,
    "items": [
        {
            "source": "oob_mock",
            "title": "OOB mock · LNG / TTF feed unavailable",
            "description": "Seeded by bootstrap_oob.py — replace after first live NewsAPI/GIE pull.",
            "url": None,
            "published_at": None,
            "provider": "sentinel_oob",
        }
    ],
    "is_cached": True,
    "is_mock": True,
    "errors": ["seeded_by_bootstrap"],
}

MOCK_FIRMS = {
    "ok": True,
    "contract_version": CONTRACT_VERSION,
    "fetched_at": None,
    "type": "FeatureCollection",
    "features": [],
    "count": 0,
    "raw_fire_count": 0,
    "proximity_buffer_nm": 50.0,
    "is_cached": True,
    "is_mock": True,
    "errors": ["seeded_by_bootstrap"],
}

MOCK_MARKET = {
    "ok": True,
    "contract_version": CONTRACT_VERSION,
    "fetched_at": None,
    "fx": {"configured": False, "base": "USD", "rates": {}},
    "commodities": {},
    "is_cached": True,
    "is_mock": True,
    "errors": ["seeded_by_bootstrap"],
}

MOCK_AIS_LIVE = {
    "ok": True,
    "contract_version": CONTRACT_VERSION,
    "updated_at": None,
    "count": 0,
    "vessels": [],
    "source": "oob_mock",
    "note": "Filled by services.ais_worker from G3 DB / connector — empty until first ingest.",
}

MOCK_FALLBACK_BUNDLE = {
    "ok": True,
    "contract_version": CONTRACT_VERSION,
    "is_mock": True,
    "purpose": "zero-network cold-start fallback for intel + AIS live cache",
    "news": MOCK_NEWS,
    "firms": MOCK_FIRMS,
    "market": MOCK_MARKET,
    "ais_live": MOCK_AIS_LIVE,
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_dirs() -> list[str]:
    created: list[str] = []
    for d in REQUIRED_DIRS:
        if not d.is_dir():
            d.mkdir(parents=True, exist_ok=True)
            created.append(str(d.relative_to(ROOT)).replace("\\", "/"))
    return created


def ensure_env(*, strict: bool = False) -> dict:
    from services.config_keys import API_REGISTRY_KEYS, registry_status

    env_path = ROOT / ".env"
    example = ROOT / ".env.example"
    report: dict = {
        "env_present": env_path.is_file(),
        "env_example_present": example.is_file(),
        "configured": 0,
        "total": len(API_REGISTRY_KEYS),
        "keys": [],
        "ok": True,
        "errors": [],
    }
    if not env_path.is_file():
        report["ok"] = False
        report["errors"].append("missing_.env — copy .env.example and fill keys")
        if example.is_file() and not strict:
            # Soft OOB: do not invent secrets; leave operator to fill.
            report["errors"].append("hint: copy .env.example → .env")
        if strict:
            raise SystemExit(2)
        return report

    st = registry_status()
    report["configured"] = st["configured"]
    report["keys"] = st["keys"]
    if st["configured"] < 1:
        report["errors"].append("no_api_keys_configured")
        # Not fatal for OOB — Dual Gate / GIS still boot with mocks
    return report


def _seed_json(path: Path, payload: dict, *, force: bool = False) -> bool:
    if path.is_file() and not force:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    body = dict(payload)
    body["fetched_at"] = body.get("fetched_at") or _now()
    body["updated_at"] = body.get("updated_at") or _now()
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def seed_mocks(*, force: bool = False) -> list[str]:
    seeded: list[str] = []
    targets = (
        (ROOT / "output" / "cache" / "news_latest.json", MOCK_NEWS),
        (ROOT / "output" / "cache" / "firms_anomalies.json", MOCK_FIRMS),
        (ROOT / "output" / "cache" / "market_summary.json", MOCK_MARKET),
        (ROOT / "data" / "cache" / "ais_live.json", MOCK_AIS_LIVE),
        (ROOT / "data" / "cache" / "mock_fallback_data.json", MOCK_FALLBACK_BUNDLE),
    )
    for path, payload in targets:
        if _seed_json(path, payload, force=force):
            seeded.append(str(path.relative_to(ROOT)).replace("\\", "/"))
    return seeded


def run(*, strict: bool = False, force_mocks: bool = False) -> dict:
    created = ensure_dirs()
    env_rep = ensure_env(strict=strict)
    seeded = seed_mocks(force=force_mocks)
    keys_ok = int(env_rep.get("configured") or 0)
    keys_total = int(env_rep.get("total") or 7)
    out = {
        "ok": bool(env_rep.get("ok", True)) or not strict,
        "contract_version": CONTRACT_VERSION,
        "dirs_created": created,
        "env": env_rep,
        "keys_configured": f"{keys_ok}/{keys_total}",
        "mocks_seeded": seeded,
        "mock_fallback": str(
            (ROOT / "data" / "cache" / "mock_fallback_data.json").relative_to(ROOT)
        ).replace("\\", "/"),
        "ts": _now(),
    }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sentinel OOB bootstrap (1.8.0-ops-gis-sot)")
    ap.add_argument("--strict", action="store_true", help="Fail if .env missing")
    ap.add_argument("--force-mocks", action="store_true", help="Overwrite cache mocks")
    ap.add_argument("--json", action="store_true", help="Print JSON report")
    args = ap.parse_args(argv)
    report = run(strict=args.strict, force_mocks=args.force_mocks)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"[OOB] contract={report['contract_version']}")
        print(f"[OOB] dirs_created={report['dirs_created'] or 'none'}")
        print(
            f"[OOB] env_present={report['env']['env_present']} "
            f"keys={report['env']['configured']}/{report['env']['total']}"
        )
        print(f"[OOB] mocks_seeded={report['mocks_seeded'] or 'none (already present)'}")
        print(f"[OOB] ok={report['ok']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
