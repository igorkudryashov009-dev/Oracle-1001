#!/usr/bin/env python3
"""
Generative image-to-3D PILOT gate (BU SAMRA only).

Does NOT call any provider until:
  --provider {meshy_api|tripo_api|triposr_local} is chosen AND
  credentials / weights are supplied as required.

Never overwrites production vessel_*.glb.
Never loops the remaining 9 vessels.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from services.generative_3d_adapter import (
        FIDELITY_BADGE_GENERATIVE,
        PILOT_IMO,
        PILOT_NAME,
        PROVIDER_CATALOG,
        assert_pilot_vessel,
        get_adapter,
        pilot_output_paths,
    )
    from services.top10_3d_mesh import resolve_ortho_paths
    from services.top10_vessels import REF_BASE, TOP10_VESSELS

    parser = argparse.ArgumentParser(description="Generative 3D pilot gate (one vessel)")
    parser.add_argument(
        "--provider",
        choices=sorted(PROVIDER_CATALOG.keys()),
        default="",
        help="Human-selected provider id (required to leave stub)",
    )
    parser.add_argument(
        "--list-providers",
        action="store_true",
        help="Print provider catalog and exit (no generation)",
    )
    parser.add_argument(
        "--credentials-json",
        type=str,
        default="",
        help="Path to operator-supplied credentials JSON (cloud APIs) — never invent keys",
    )
    parser.add_argument(
        "--weights-path",
        type=str,
        default="",
        help="Optional local TripoSR checkout / HF cache root",
    )
    parser.add_argument(
        "--allow-cpu-slow",
        action="store_true",
        help="EXPLICIT human override to run TripoSR on CPU (not recommended; default refuse)",
    )
    parser.add_argument(
        "--imo",
        type=str,
        default=PILOT_IMO,
        help=f"Must remain {PILOT_IMO} (pilot lock)",
    )
    args = parser.parse_args()

    if args.list_providers or not args.provider:
        from services.generative_3d_adapter import probe_nvidia_vram

        report = {
            "ok": True,
            "phase": "human_gate_awaiting_provider",
            "pilot_imo": PILOT_IMO,
            "pilot_name": PILOT_NAME,
            "production_glb_untouched": True,
            "rollout_to_9_blocked": True,
            "fidelity_badge_if_enabled": FIDELITY_BADGE_GENERATIVE,
            "providers": PROVIDER_CATALOG,
            "gpu_probe": probe_nvidia_vram(),
            "pilot_paths": {k: str(v) for k, v in pilot_output_paths().items()},
            "next_step": (
                "Choose provider id, then re-run with --provider <id>. "
                "triposr_local requires ≥6GB VRAM (no silent CPU)."
            ),
        }
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    vessel = next((v for v in TOP10_VESSELS if str(v["imo"]) == str(args.imo)), None)
    if not vessel:
        print(json.dumps({"ok": False, "error": f"IMO {args.imo} not in TOP10"}), file=sys.stderr)
        return 1
    try:
        assert_pilot_vessel(vessel)
    except PermissionError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2

    creds = None
    if args.credentials_json:
        creds = json.loads(Path(args.credentials_json).read_text(encoding="utf-8"))

    catalog = PROVIDER_CATALOG[args.provider]
    if catalog.get("needs_credentials") and not creds:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "credentials_required",
                    "provider": args.provider,
                    "hint": "Pass --credentials-json with operator-supplied secrets. Do not invent keys.",
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 3
    # triposr_local: weights optional (HF download inside isolated venv after VRAM gate)

    paths = resolve_ortho_paths(vessel, REF_BASE)
    images = [paths["side"], paths["bow"], paths["sat"]]
    missing = [str(p) for p in images if not p.exists()]
    if missing:
        print(json.dumps({"ok": False, "error": "missing_ortho", "missing": missing}), file=sys.stderr)
        return 4

    adapter = get_adapter(
        provider=args.provider,
        credentials=creds,
        weights_path=args.weights_path or None,
        allow_cpu=bool(args.allow_cpu_slow),
    )
    try:
        result = adapter.generate(images, vessel)
    except NotImplementedError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "phase": "provider_selected_but_not_implemented",
                    "provider": args.provider,
                    "catalog": catalog,
                    "error": str(exc),
                    "pilot_paths": {k: str(v) for k, v in pilot_output_paths().items()},
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 5

    # Persist gate report for TripoSR VRAM stops
    if args.provider == "triposr_local":
        report_path = ROOT / "logs" / "triposr_pilot_report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(
                {
                    "phase": result.status,
                    "result": result.to_dict(),
                    "single_image_architecture": True,
                    "cpu_forced": False,
                    "rollout_to_9": "blocked",
                    "production_glb_untouched": True,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=True))
    return 0 if result.ok else 6


if __name__ == "__main__":
    raise SystemExit(main())
