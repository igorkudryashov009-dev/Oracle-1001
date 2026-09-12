#!/usr/bin/env python3
"""Add SAT-face palette indices to existing voxel JSON (no recarve)."""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _write_h4_overlay(imo, name, sat_rgb, sat_m, sat_b, ux, uy, pos, pal, idx, idx_sat) -> None:
    """Camera-free: SAT photo vs blend top vs SAT-index top (same XY)."""
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    from services.top10_voxel_cubes import uv_to_pixel

    h, w = sat_m.shape
    blend_img = np.zeros((h, w, 3), dtype=np.uint8)
    satidx_img = np.zeros((h, w, 3), dtype=np.uint8)
    key = np.round(np.stack([ux, uy], axis=1) * 200).astype(np.int32)
    top = {}
    for i in np.argsort(pos[:, 2]):
        top[tuple(key[i])] = int(i)
    top_i = np.array(list(top.values()), dtype=np.int32)
    col, row = uv_to_pixel(ux[top_i], uy[top_i], size=w, bounds=sat_b)
    rr = np.clip(np.round(row).astype(np.int32), 0, h - 1)
    cc = np.clip(np.round(col).astype(np.int32), 0, w - 1)
    blend_rgb = pal[np.clip(idx[top_i], 0, len(pal) - 1)]
    satc_rgb = pal[np.clip(idx_sat[top_i], 0, len(pal) - 1)]
    blend_img[rr, cc] = blend_rgb
    satidx_img[rr, cc] = satc_rgb
    photo = (np.clip(sat_rgb, 0, 1) * 255).astype(np.uint8)
    photo[~sat_m] = (18, 24, 36)
    try:
        from scipy import ndimage

        vis = ndimage.binary_dilation(satidx_img.any(axis=2), iterations=2)
        # Fill 1-cell gaps so the camera-free panel is readable at 420px
        for img in (blend_img, satidx_img):
            for c in range(3):
                ch = img[:, :, c].astype(np.float32)
                filled = ndimage.maximum_filter(ch, size=3)
                img[:, :, c] = np.where(img[:, :, c] > 0, img[:, :, c], filled.astype(np.uint8))
            img[~vis & (img.sum(axis=2) == 0)] = 0
    except Exception:
        pass

    cell = (420, 280)
    panels = []
    for arr, lab in (
        (photo, "SAT photo (truth)"),
        (blend_img, "BEFORE: side-weighted cell color"),
        (satidx_img, "AFTER: SAT-face palette index"),
    ):
        im = Image.fromarray(arr, mode="RGB")
        im = ImageOps.contain(im, cell)
        panels.append((im, lab))
    pad, header, lab_h = 14, 44, 26
    W = pad * 4 + cell[0] * 3
    H = header + pad + cell[1] + lab_h + pad
    sheet = Image.new("RGB", (W, H), (11, 18, 32))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("arial.ttf", 13)
        font_t = ImageFont.truetype("arial.ttf", 16)
    except Exception:
        font = font_t = ImageFont.load_default()
    draw.text((pad, 12), f"H4 SAT deck colors · {name} · IMO {imo} (no camera)", fill=(224, 247, 252), font=font_t)
    for i, (im, lab) in enumerate(panels):
        x = pad + i * (cell[0] + pad)
        y = header
        sheet.paste(im, (x + (cell[0] - im.width) // 2, y + (cell[1] - im.height) // 2))
        draw.text((x, y + cell[1] + 4), lab, fill=(148, 163, 184), font=font)
    (ROOT / "logs" / f"overhead_h4_satcolor_{imo}.png").parent.mkdir(exist_ok=True)
    sheet.save(ROOT / "logs" / f"overhead_h4_satcolor_{imo}.png")


def main() -> int:
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    from services.top10_3d_mesh import (
        MASK_DILATE_PX,
        ORTHO_TEX_SIZE,
        extract_silhouette,
        load_ortho_rgb,
        mask_content_uv_bounds,
        prepare_masked_ortho,
        resolve_ortho_paths,
    )
    from services.top10_vessels import REF_BASE, TOP10_VESSELS, write_js_manifest
    from services.top10_voxel_cubes import (
        OUT_VOXEL_DIR,
        WEB_VOXEL_DIR,
        _sample_ortho,
        quantize_rgb_to_palette,
        uv_to_pixel,
    )

    rows = []
    for v in TOP10_VESSELS:
        imo = str(v["imo"])
        path = OUT_VOXEL_DIR / f"vessel_{imo}_voxels.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        n = int(payload["n_occupied"])
        pos = np.frombuffer(
            base64.b64decode(payload["positions_f32_b64"]), dtype="<f4"
        ).reshape(-1, 3)
        hx, hy, _ = payload["half_extents"]
        pal = np.asarray(payload["palette_rgb_u8"], dtype=np.uint8)
        paths = resolve_ortho_paths(v, REF_BASE)
        sat_m = extract_silhouette(paths["sat"], size=ORTHO_TEX_SIZE)
        sat_rgb = prepare_masked_ortho(
            load_ortho_rgb(paths["sat"], size=ORTHO_TEX_SIZE), sat_m, dilate_px=MASK_DILATE_PX
        )
        sat_b = mask_content_uv_bounds(sat_m)
        ux = (pos[:, 0] / max(float(hx), 1e-6)).clip(-1, 1)
        uy = (pos[:, 1] / max(float(hy), 1e-6)).clip(-1, 1)
        rgb = _sample_ortho(sat_rgb, ux, uy, bounds=sat_b)
        idx_sat = quantize_rgb_to_palette(rgb, pal)
        payload["sat_indices_u8_b64"] = base64.b64encode(
            idx_sat.astype(np.uint8).tobytes()
        ).decode("ascii")
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        path.write_text(text, encoding="utf-8")
        (WEB_VOXEL_DIR / path.name).write_text(text, encoding="utf-8")
        meta_p = path.with_suffix(".meta.json")
        if meta_p.exists():
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            meta["bytes"] = path.stat().st_size
            meta["has_sat_indices"] = True
            meta_text = json.dumps(meta, indent=2)
            meta_p.write_text(meta_text, encoding="utf-8")
            (WEB_VOXEL_DIR / meta_p.name).write_text(meta_text, encoding="utf-8")
        # color delta: blended vs SAT on top layer
        idx = np.frombuffer(base64.b64decode(payload["color_indices_u8_b64"]), dtype=np.uint8)
        blend = pal[np.clip(idx, 0, len(pal) - 1)] / 255.0
        satc = pal[np.clip(idx_sat, 0, len(pal) - 1)] / 255.0
        d_before = float(np.median(np.sqrt(((blend - rgb) ** 2).sum(axis=1))))
        d_after = float(np.median(np.sqrt(((satc - rgb) ** 2).sum(axis=1))))
        # Top-layer only (what OVERHEAD actually shows)
        key = np.round(np.stack([ux, uy], axis=1) * 200).astype(np.int32)
        top = {}
        for i in np.argsort(pos[:, 2]):
            top[tuple(key[i])] = int(i)
        top_i = np.array(list(top.values()), dtype=np.int32)
        d_top_b = float(np.median(np.sqrt(((blend[top_i] - rgb[top_i]) ** 2).sum(axis=1))))
        d_top_a = float(np.median(np.sqrt(((satc[top_i] - rgb[top_i]) ** 2).sum(axis=1))))
        landmarks = []
        for lab, tx, ty in (
            ("bow", 0.72, 0.0),
            ("fwd_deck", 0.32, 0.0),
            ("midships", 0.0, 0.0),
            ("aft_deck", -0.32, 0.0),
            ("stern", -0.72, 0.0),
        ):
            dxy = (ux - tx) ** 2 + (uy - ty) ** 2
            j = int(np.argmin(dxy))
            landmarks.append(
                {
                    "id": lab,
                    "ux": round(float(ux[j]), 3),
                    "uy": round(float(uy[j]), 3),
                    "sat_rgb": [round(float(x), 3) for x in rgb[j]],
                    "blend_rgb": [round(float(x), 3) for x in blend[j]],
                    "satidx_rgb": [round(float(x), 3) for x in satc[j]],
                    "l2_blend": round(float(np.sqrt(((blend[j] - rgb[j]) ** 2).sum())), 4),
                    "l2_satidx": round(float(np.sqrt(((satc[j] - rgb[j]) ** 2).sum())), 4),
                    "satidx_closer": bool(
                        np.sqrt(((satc[j] - rgb[j]) ** 2).sum())
                        <= np.sqrt(((blend[j] - rgb[j]) ** 2).sum()) + 1e-6
                    ),
                }
            )
        if imo in ("9388833", "9337755", "9372731"):
            _write_h4_overlay(imo, v["name"], sat_rgb, sat_m, sat_b, ux, uy, pos, pal, idx, idx_sat)
        rows.append(
            {
                "imo": imo,
                "name": v.get("name"),
                "n": n,
                "bytes": path.stat().st_size,
                "median_rgb_l2_blend_vs_sat": round(d_before, 4),
                "median_rgb_l2_satidx_vs_sat": round(d_after, 4),
                "median_rgb_l2_top_blend_vs_sat": round(d_top_b, 4),
                "median_rgb_l2_top_satidx_vs_sat": round(d_top_a, 4),
                "landmarks": landmarks,
            }
        )
        print(json.dumps(rows[-1], ensure_ascii=False))
    write_js_manifest()
    (ROOT / "logs" / "overhead_sat_index_patch.json").write_text(
        json.dumps({"ok": True, "vessels": rows}, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
