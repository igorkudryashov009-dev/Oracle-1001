"""
Generative few-shot image-to-3D adapter — STUB + TripoSR local (HOST-ABORTED).

============================================================================
OPTION D LOCKED (2026-09-09) — Senior Architect mandate
  Active Digital Twin pipeline = v3.2-presentation voxel envelope ONLY.
  triposr_local on this host (RTX 2050 / 4GB VRAM < 6GB gate) is PERMANENTLY
  ABORTED. Do NOT force CPU fallback, single-image mesh rollout, or inject
  generative prior-hallucinated detail into OSINT HUD.
  Production GLBs: output/assets/3d_models/vessel_*.glb (glTF v2).
============================================================================

CRITICAL — TripoSR is SINGLE-IMAGE → 3D (architectural hallucination risk):
  Only one photo would be consumed; unseen structure is training-prior fiction,
  not observed OSINT. That path is not production on this host.

VRAM gate (triposr_local):
  Official TripoSR baseline ≈6GB VRAM. Hard refuse below that — no silent CPU.

Pilot scope (if ever re-enabled on a >=6GB GPU host — separate human decision):
  - One vessel only: BU SAMRA / IMO 9388833
  - Output under output/generative_pilot/ — never overwrite production GLB
  - No automatic rollout to the remaining 9 vessels
"""

from __future__ import annotations

import json
import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]

# ── Pilot identity (hard-coded — do not widen without human stop-gate) ──────
PILOT_IMO = "9388833"
PILOT_NAME = "BU SAMRA"
PILOT_GLB_NAME = f"vessel_{PILOT_IMO}_generative_pilot.glb"
# Isolated from production manifest paths (prompt: output/generative_pilot/)
PILOT_OUT_DIR = ROOT / "output" / "generative_pilot"
PILOT_WEB_DIR = ROOT / "web" / "assets" / "3d_models" / "generative_pilot"

TRIPOSR_MIN_VRAM_MIB = 6144  # 6 GiB — do not soft-fail to CPU
TRIPOSR_HF_REPO = "stabilityai/TripoSR"
TRIPOSR_GITHUB = "https://github.com/VAST-AI-Research/TripoSR"

FIDELITY_BADGE_GENERATIVE = (
    "AI-GENERATED RECONSTRUCTION · SPECULATIVE DETAIL · GEOMETRY NOT OSINT-VERIFIED"
)
FIDELITY_BADGE_ENVELOPE = (
    "PHOTO-COMPOSITE 3D RECONSTRUCTION · TEXTURE FROM ORTHO TRIPLET · "
    "NOT VERIFIED STRUCTURAL MODEL"
)

PROVIDER_CATALOG: dict[str, dict[str, Any]] = {
    "meshy_api": {
        "label": "Meshy Image-to-3D API",
        "kind": "paid_cloud_api",
        "cost_model": "credits_per_generation (Pro+ for API; ~20–35 credits/task typical)",
        "needs_credentials": True,
        "needs_local_gpu": False,
    },
    "tripo_api": {
        "label": "Tripo / Tripo3D commercial API",
        "kind": "paid_cloud_api",
        "cost_model": "paid plan / credits per generation (confirm on vendor site)",
        "needs_credentials": True,
        "needs_local_gpu": False,
    },
    "triposr_local": {
        "label": "TripoSR (self-hosted open-source)",
        "kind": "self_hosted_oss",
        "cost_model": "no API fee; local NVIDIA GPU ~6GB+ VRAM (CPU slow — NOT auto-used)",
        "needs_credentials": False,
        "needs_local_gpu": True,
        "min_vram_mib": TRIPOSR_MIN_VRAM_MIB,
        "hf_repo": TRIPOSR_HF_REPO,
        "github": TRIPOSR_GITHUB,
        "input_mode": "single_image",
        "host_status": "permanently_aborted_option_d",
        "host_reason": "RTX 2050 4GB VRAM < 6GB TripoSR gate; v3.2 envelope remains SoT",
        "hallucination_note": (
            "Single-image prior invents unseen sides (bow/sat details) — OSINT risk"
        ),
    },
}


@dataclass
class GLBResult:
    """Outcome of a generative (or stub) image→3D call — never mutates production GLB."""

    ok: bool
    imo: str
    path: Optional[Path] = None
    bytes: int = 0
    provider: Optional[str] = None
    fidelity_badge: str = FIDELITY_BADGE_GENERATIVE
    status: str = "not_activated"
    error: Optional[str] = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.path is not None:
            d["path"] = str(self.path)
        return d


class InsufficientGPUError(RuntimeError):
    """Raised when TripoSR VRAM gate fails — caller must not fall back to CPU."""


def probe_nvidia_vram() -> dict[str, Any]:
    """Return GPU name / total / free MiB via nvidia-smi, or unavailable."""
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free,memory.used",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            errors="replace",
            timeout=15,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "error": str(exc)}

    line = out.strip().splitlines()[0] if out.strip() else ""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 4:
        return {"available": False, "error": f"unparseable nvidia-smi: {line!r}"}
    try:
        total = int(float(parts[1]))
        free = int(float(parts[2]))
        used = int(float(parts[3]))
    except ValueError:
        return {"available": False, "error": f"bad nvidia-smi numbers: {line!r}"}
    return {
        "available": True,
        "name": parts[0],
        "memory_total_mib": total,
        "memory_free_mib": free,
        "memory_used_mib": used,
        "meets_triposr_min": free >= TRIPOSR_MIN_VRAM_MIB and total >= TRIPOSR_MIN_VRAM_MIB,
        "min_required_mib": TRIPOSR_MIN_VRAM_MIB,
    }


def assert_triposr_vram(*, allow_cpu: bool = False) -> dict[str, Any]:
    """
    Hard gate: refuse TripoSR local run if VRAM < 6GiB.
    allow_cpu must be an explicit human override — never default True.
    """
    probe = probe_nvidia_vram()
    if allow_cpu:
        probe["cpu_override"] = True
        return probe
    if not probe.get("available"):
        raise InsufficientGPUError(
            f"nvidia-smi unavailable — cannot verify >={TRIPOSR_MIN_VRAM_MIB} MiB VRAM. "
            f"detail={probe.get('error')}. Do not auto-run TripoSR on CPU."
        )
    total = int(probe["memory_total_mib"])
    free = int(probe["memory_free_mib"])
    if total < TRIPOSR_MIN_VRAM_MIB or free < TRIPOSR_MIN_VRAM_MIB:
        raise InsufficientGPUError(
            f"TripoSR requires >={TRIPOSR_MIN_VRAM_MIB} MiB VRAM. "
            f"Detected {probe.get('name')}: total={total} MiB free={free} MiB. "
            "Refusing CPU fallback per pilot contract. "
            "Options: cloud GPU with >=6GB, different provider (meshy_api/tripo_api), "
            "or explicit human --allow-cpu-slow (not recommended)."
        )
    return probe


class Generative3DAdapter(ABC):
    """Abstract socket for few-shot generative image→GLB."""

    name = "generative_3d_stub"

    def __init__(
        self,
        *,
        provider: str | None = None,
        credentials: dict[str, Any] | None = None,
        weights_path: Path | str | None = None,
        allow_cpu: bool = False,
    ):
        self.provider = provider
        self._credentials = credentials
        self.weights_path = Path(weights_path) if weights_path else None
        self.allow_cpu = bool(allow_cpu)

    @abstractmethod
    def generate(
        self,
        images: Sequence[Path],
        vessel_meta: dict[str, Any],
    ) -> GLBResult:
        raise NotImplementedError

    def coverage_report(self) -> dict[str, Any]:
        return {
            "adapter": self.name,
            "kind": "generative_image_to_3d",
            "status": "not_activated",
            "provider": self.provider,
            "credentials_present": bool(self._credentials),
            "weights_path": str(self.weights_path) if self.weights_path else None,
            "pilot_imo_only": PILOT_IMO,
            "pilot_glb_name": PILOT_GLB_NAME,
            "fidelity_badge_if_enabled": FIDELITY_BADGE_GENERATIVE,
            "provider_catalog": PROVIDER_CATALOG,
            "gpu_probe": probe_nvidia_vram(),
            "note": (
                "Select provider + supply credentials or TripoSR checkout; "
                "triposr_local enforces ≥6GB VRAM (no silent CPU)."
            ),
        }


class Generative3DStubAdapter(Generative3DAdapter):
    name = "generative_3d_stub"

    def generate(
        self,
        images: Sequence[Path],
        vessel_meta: dict[str, Any],
    ) -> GLBResult:
        imo = str(vessel_meta.get("imo") or "")
        raise NotImplementedError(
            "Generative3DAdapter requires an explicit provider implementation "
            "(meshy_api | tripo_api | triposr_local) after human selection. "
            f"Pilot target IMO={PILOT_IMO} only; refused imo={imo!r}."
        )


class TripoSRLocalAdapter(Generative3DAdapter):
    """
    Local TripoSR (stabilityai/TripoSR) — single-image reconstruction.

    Uses Side ortho as the sole generative input. Additional images in `images`
    are recorded in meta as unused_context (bow/sat) for QA — they are NOT fed
    into TripoSR (model limitation).
    """

    name = "triposr_local"

    def generate(
        self,
        images: Sequence[Path],
        vessel_meta: dict[str, Any],
    ) -> GLBResult:
        assert_pilot_vessel(vessel_meta)
        imo = str(vessel_meta["imo"])
        img_list = [Path(p) for p in images]
        side = img_list[0] if img_list else None
        unused = [str(p) for p in img_list[1:]]

        base_meta: dict[str, Any] = {
            "provider": "triposr_local",
            "hf_repo": TRIPOSR_HF_REPO,
            "github": TRIPOSR_GITHUB,
            "input_mode": "single_image",
            "input_image_role": "side_lateral",
            "input_image": str(side) if side else None,
            "unused_ortho_context": unused,
            "hallucination_architecture": (
                "TripoSR sees only Side; bow/deck/stern structure is prior hallucination, "
                "not multi-view photogrammetry"
            ),
            "fidelity_badge": FIDELITY_BADGE_GENERATIVE,
            "production_glb_untouched": True,
        }

        try:
            gpu = assert_triposr_vram(allow_cpu=self.allow_cpu)
        except InsufficientGPUError as exc:
            probe = probe_nvidia_vram()
            return GLBResult(
                ok=False,
                imo=imo,
                provider="triposr_local",
                status="blocked_insufficient_vram",
                error=str(exc),
                meta={**base_meta, "gpu_probe": probe},
            )
        base_meta["gpu_probe"] = gpu

        if side is None or not side.exists():
            return GLBResult(
                ok=False,
                imo=imo,
                provider="triposr_local",
                status="missing_side_image",
                error=f"Side image missing: {side}",
                meta=base_meta,
            )

        # Inference lives in isolated TripoSR venv — invoked via helper script once
        # environment passes the VRAM gate (never auto-installed into production venv).
        helper = ROOT / "scripts" / "_triposr_infer_isolated.py"
        if not helper.exists():
            return GLBResult(
                ok=False,
                imo=imo,
                provider="triposr_local",
                status="infer_helper_missing",
                error=f"Missing {helper} — install TripoSR env after VRAM gate passes",
                meta=base_meta,
            )

        out_paths = pilot_output_paths()
        out_paths["out"].parent.mkdir(parents=True, exist_ok=True)

        py = self._resolve_triposr_python()
        if py is None:
            return GLBResult(
                ok=False,
                imo=imo,
                provider="triposr_local",
                status="triposr_venv_missing",
                error=(
                    "Isolated venv not found at .venv-triposr/. "
                    "Create only after GPU ≥6GB VRAM is available "
                    "(see scripts/setup_triposr_env.ps1)."
                ),
                meta=base_meta,
            )

        cmd = [
            str(py),
            str(helper),
            "--image",
            str(side),
            "--out-glb",
            str(out_paths["out"]),
            "--imo",
            imo,
        ]
        if self.weights_path:
            cmd.extend(["--weights-path", str(self.weights_path)])
        if self.allow_cpu:
            cmd.append("--device-cpu")

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3600,
            )
        except subprocess.TimeoutExpired:
            return GLBResult(
                ok=False,
                imo=imo,
                provider="triposr_local",
                status="timeout",
                error="TripoSR inference timed out (>3600s)",
                meta=base_meta,
            )

        payload: dict[str, Any] = {}
        for line in reversed((proc.stdout or "").splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    payload = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue

        if proc.returncode != 0 or not payload.get("ok"):
            return GLBResult(
                ok=False,
                imo=imo,
                provider="triposr_local",
                status="infer_failed",
                error=payload.get("error")
                or (proc.stderr or proc.stdout or "")[-2000:]
                or f"exit={proc.returncode}",
                meta={**base_meta, "infer_payload": payload},
            )

        glb_path = Path(payload.get("path") or out_paths["out"])
        if not glb_path.exists():
            return GLBResult(
                ok=False,
                imo=imo,
                provider="triposr_local",
                status="glb_missing_after_infer",
                error=f"Expected GLB missing: {glb_path}",
                meta={**base_meta, "infer_payload": payload},
            )

        meta_path = out_paths["meta"]
        full_meta = {
            **base_meta,
            **{k: v for k, v in payload.items() if k != "ok"},
            "vessel": {
                "imo": imo,
                "name": vessel_meta.get("name"),
                "loa_m": vessel_meta.get("loa_m"),
                "beam_m": vessel_meta.get("beam_m"),
                "draft_m": vessel_meta.get("draft_m"),
            },
        }
        meta_path.write_text(json.dumps(full_meta, indent=2), encoding="utf-8")

        return GLBResult(
            ok=True,
            imo=imo,
            path=glb_path,
            bytes=glb_path.stat().st_size,
            provider="triposr_local",
            status="pilot_ok",
            fidelity_badge=FIDELITY_BADGE_GENERATIVE,
            meta=full_meta,
        )

    def _resolve_triposr_python(self) -> Optional[Path]:
        cand = ROOT / ".venv-triposr" / "Scripts" / "python.exe"
        if cand.is_file():
            return cand
        cand2 = ROOT / ".venv-triposr" / "bin" / "python"
        if cand2.is_file():
            return cand2
        return None


def assert_pilot_vessel(vessel_meta: dict[str, Any]) -> None:
    imo = str(vessel_meta.get("imo") or "")
    if imo != PILOT_IMO:
        raise PermissionError(
            f"Generative 3D pilot is locked to IMO {PILOT_IMO} ({PILOT_NAME}). "
            f"Refused IMO {imo}."
        )


def pilot_output_paths() -> dict[str, Path]:
    return {
        "out": PILOT_OUT_DIR / PILOT_GLB_NAME,
        "web": PILOT_WEB_DIR / PILOT_GLB_NAME,
        "meta": PILOT_OUT_DIR / f"vessel_{PILOT_IMO}_generative_pilot.meta.json",
    }


def get_adapter(
    *,
    provider: str | None = None,
    credentials: dict[str, Any] | None = None,
    weights_path: Path | str | None = None,
    allow_cpu: bool = False,
) -> Generative3DAdapter:
    if provider and provider not in PROVIDER_CATALOG:
        raise ValueError(
            f"Unknown provider {provider!r}. Choose one of: {sorted(PROVIDER_CATALOG)}"
        )
    if provider == "triposr_local":
        return TripoSRLocalAdapter(
            provider=provider,
            credentials=credentials,
            weights_path=weights_path,
            allow_cpu=allow_cpu,
        )
    # meshy_api / tripo_api remain unimplemented
    return Generative3DStubAdapter(
        provider=provider,
        credentials=credentials,
        weights_path=weights_path,
        allow_cpu=allow_cpu,
    )
