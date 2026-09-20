"""Quick probe: silhouette mask tightness + blue bleed after mask-aware prep."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from services.top10_3d_mesh import (
    MASK_DILATE_PX,
    ORTHO_TEX_SIZE,
    extract_silhouette,
    load_ortho_rgb,
    prepare_masked_ortho,
)


def blue_frac(img: np.ndarray) -> float:
    if img is None or getattr(img, "size", 0) == 0:
        return float("nan")
    if img.ndim == 2:
        return float("nan")
    return float(
        (
            (img[:, :, 2] > img[:, :, 0] + 25)
            & (img[:, :, 2] > img[:, :, 1] + 15)
            & (img[:, :, 2] > 140)
        ).mean()
    )


def main() -> None:
    for rank in (1, 5, 4, 2, 3):
        p = Path(f"assets/7000/{rank}-1.jpg")
        m = extract_silhouette(p, size=ORTHO_TEX_SIZE)
        raw = load_ortho_rgb(p, size=ORTHO_TEX_SIZE)
        prep = prepare_masked_ortho(raw, m, dilate_px=MASK_DILATE_PX)
        outside = prep[~m]
        bf_out = (
            float(
                (
                    (outside[:, 2] > outside[:, 0] + 25)
                    & (outside[:, 2] > outside[:, 1] + 15)
                    & (outside[:, 2] > 140)
                ).mean()
            )
            if outside.size
            else float("nan")
        )
        print(
            f"rank{rank}: mask_frac={m.mean():.3f} "
            f"blue_raw={blue_frac(raw):.3f} blue_prep={blue_frac(prep):.3f} "
            f"blue_outside={bf_out:.3f} dilate={MASK_DILATE_PX}"
        )


if __name__ == "__main__":
    main()
