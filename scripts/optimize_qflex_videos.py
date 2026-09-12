#!/usr/bin/env python3
"""Optimize Q-Flex / TOP-10 digital-twin flight MP4s for Sentinel HUD streaming.

Target: ~1.5–2.0 MB each, H.264 yuv420p, audio stripped, moov faststart.
Native frame rate preserved (sources are typically 24 fps — do NOT invent 60 fps).

Usage:
  python scripts/optimize_qflex_videos.py --input-dir "C:\\111\\1001\\1-10" --crf 24 --preset slow
  python scripts/optimize_qflex_videos.py --input-dir "C:\\111\\1001\\1-10" --sync-repo
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "logs"
ASSET_VIDEOS = ROOT / "assets" / "7000" / "videos"
OUT_VIDEOS = ROOT / "output" / "assets" / "videos"

# Exact Q-Flex / flight mapping (filename → IMO). Do not invent missing files.
QFLEX_FLIGHTS: list[dict[str, str]] = [
    {"imo": "9388833", "name": "BU SAMRA", "file": "bu-samra-parallel-flight-1.mp4"},
    {"imo": "9397303", "name": "MEKAINES", "file": "mekaines-parallel-flight-2.mp4"},
    {"imo": "9397315", "name": "AL MAFYAR", "file": "al-mafyar-parallel-flight-3.mp4"},
    {"imo": "9397327", "name": "AL KHARAITIYAT", "file": "al-kharaitiyat-parallel-flight-4.mp4"},
    {"imo": "9337755", "name": "MOZAH", "file": "mozah-parallel-flight-5.mp4"},
    {"imo": "9372731", "name": "UMM SLAL", "file": "umm-slal-parallel-flight-6.mp4"},
    {"imo": "9372743", "name": "AL GHUWAIRIYA", "file": "al-ghuwairiya-digital-twin-flight-7.mp4"},
    {"imo": "9388819", "name": "LIJMILIYA", "file": "lijmiliya-parallel-flight-8.mp4"},
    {"imo": "9388821", "name": "AL SAMRIYA", "file": "al-samriya-parallel-flight-9.mp4"},
    {"imo": "9397298", "name": "AL MAYEDA", "file": "al-mayeda-digital-twin-flight-10.mp4"},
]

MB = 1024 * 1024
TARGET_MIN_BYTES = int(1.5 * MB)
TARGET_MAX_BYTES = int(2.0 * MB)


def find_ffmpeg() -> str:
    candidates: list[str] = []
    which = shutil.which("ffmpeg")
    if which:
        candidates.append(which)
    candidates.append(str(ROOT / "tools" / "ffmpeg.exe"))
    try:
        import imageio_ffmpeg

        candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:  # noqa: BLE001
        pass
    for cand in candidates:
        if cand and Path(cand).is_file():
            return cand
    raise SystemExit(
        "ffmpeg not found — install FFmpeg on PATH, place tools/ffmpeg.exe, "
        "or ensure imageio-ffmpeg is installed in the venv"
    )


def probe(ffmpeg: str, src: Path) -> dict[str, Any]:
    r = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(src)],
        capture_output=True,
        text=True,
        errors="replace",
    )
    text = (r.stderr or "") + (r.stdout or "")
    width = height = None
    m = re.search(r"(\d{3,5})x(\d{3,5})", text)
    if m:
        width, height = int(m.group(1)), int(m.group(2))
    fps = None
    mf = re.search(r"([\d.]+)\s*fps", text)
    if mf:
        try:
            fps = float(mf.group(1))
        except ValueError:
            fps = None
    dur = None
    md = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", text)
    if md:
        dur = int(md.group(1)) * 3600 + int(md.group(2)) * 60 + float(md.group(3))
    return {
        "width": width,
        "height": height,
        "fps": fps,
        "duration_s": dur,
        "has_audio": "Audio:" in text,
        "probe_tail": text[-1200:],
    }


def _vf_scale(width: int | None, height: int | None, *, max_w: int | None) -> str:
    """Even dimensions for yuv420p; optional containment width."""
    if max_w and width and width > max_w:
        return f"scale={max_w}:-2"
    return "scale=trunc(iw/2)*2:trunc(ih/2)*2"


def encode_one(
    ffmpeg: str,
    src: Path,
    dst: Path,
    *,
    crf: int,
    preset: str,
    max_w: int | None,
    info: dict[str, Any],
) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp.mp4")
    if tmp.exists():
        tmp.unlink()
    vf = _vf_scale(info.get("width"), info.get("height"), max_w=max_w)
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-i",
        str(src),
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-crf",
        str(crf),
        "-preset",
        preset,
        "-profile:v",
        "high",
        "-level",
        "4.1",
        "-vf",
        vf,
        "-movflags",
        "+faststart",
        str(tmp),
    ]
    subprocess.check_call(cmd)
    tmp.replace(dst)


def optimize_file(
    ffmpeg: str,
    src: Path,
    *,
    crf: int,
    preset: str,
    max_bytes: int,
    min_bytes: int,
    keep_backup: bool,
) -> dict[str, Any]:
    if not src.is_file():
        return {"file": src.name, "ok": False, "error": "missing"}

    info = probe(ffmpeg, src)
    src_bytes = src.stat().st_size
    backup = src.with_suffix(src.suffix + ".source.bak")
    work_src = src
    if keep_backup:
        if not backup.exists():
            shutil.copy2(src, backup)
        work_src = backup if backup.exists() else src

    out_tmp = src.with_suffix(".opt.tmp.mp4")
    attempts: list[dict[str, Any]] = []
    # Ladder: CRF → optional 1080p containment if still oversized
    plans: list[tuple[int, int | None]] = [
        (crf, None),
        (min(crf + 1, 28), None),
        (min(crf + 2, 28), None),
        (min(crf + 1, 28), 1920),
        (min(crf + 2, 28), 1920),
        (26, 1920),
        (28, 1600),
    ]
    # Deduplicate while preserving order
    seen: set[tuple[int, int | None]] = set()
    uniq_plans: list[tuple[int, int | None]] = []
    for p in plans:
        if p not in seen:
            seen.add(p)
            uniq_plans.append(p)

    best: Path | None = None
    best_size = 10**18
    chosen_crf = crf
    chosen_max_w: int | None = None

    for try_crf, max_w in uniq_plans:
        encode_one(
            ffmpeg,
            work_src,
            out_tmp,
            crf=try_crf,
            preset=preset,
            max_w=max_w,
            info=info,
        )
        size = out_tmp.stat().st_size
        attempts.append({"crf": try_crf, "max_w": max_w, "bytes": size})
        if size < best_size:
            best_size = size
            best = out_tmp
            chosen_crf = try_crf
            chosen_max_w = max_w
        if min_bytes <= size <= max_bytes:
            break
        if size <= max_bytes and size < min_bytes:
            # Under-min is acceptable if quality ladder already used lower CRF first
            break

    if best is None or not out_tmp.exists():
        return {"file": src.name, "ok": False, "error": "encode_failed", "attempts": attempts}

    # Atomic replace of working MP4 (leave .source.bak intact)
    final = src
    shutil.move(str(out_tmp), str(final))
    out_bytes = final.stat().st_size
    return {
        "file": src.name,
        "ok": True,
        "imo": None,
        "source_bytes": src_bytes if not backup.exists() else backup.stat().st_size,
        "output_bytes": out_bytes,
        "output_mb": round(out_bytes / MB, 3),
        "ratio": round(out_bytes / max(src_bytes, 1), 4),
        "crf": chosen_crf,
        "max_w": chosen_max_w,
        "preset": preset,
        "within_target": min_bytes <= out_bytes <= max_bytes,
        "under_2mb": out_bytes <= max_bytes,
        "probe": {
            "width": info.get("width"),
            "height": info.get("height"),
            "fps": info.get("fps"),
            "duration_s": info.get("duration_s"),
            "had_audio": info.get("has_audio"),
        },
        "attempts": attempts,
        "backup": str(backup) if backup.exists() else None,
    }


def sync_to_repo(input_dir: Path, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ASSET_VIDEOS.mkdir(parents=True, exist_ok=True)
    OUT_VIDEOS.mkdir(parents=True, exist_ok=True)
    synced: list[dict[str, Any]] = []
    by_file = {r["file"]: r for r in results if r.get("ok")}
    for row in QFLEX_FLIGHTS:
        src = input_dir / row["file"]
        if not src.is_file():
            synced.append({"imo": row["imo"], "ok": False, "error": "missing_after_opt"})
            continue
        dest_name = f"vessel_{row['imo']}_flight.mp4"
        asset = ASSET_VIDEOS / dest_name
        out = OUT_VIDEOS / dest_name
        shutil.copy2(src, asset)
        shutil.copy2(src, out)
        # Keep AL GHUWAIRIYA / 9372743 also as luma alias for existing HUD path
        if row["imo"] == "9372743":
            for alias in (
                ASSET_VIDEOS / "vessel_9372743_luma.mp4",
                OUT_VIDEOS / "vessel_9372743_luma.mp4",
            ):
                shutil.copy2(src, alias)
        meta = {
            "imo": row["imo"],
            "name": row["name"],
            "ok": True,
            "asset": str(asset),
            "output": str(out),
            "bytes": src.stat().st_size,
            "from": by_file.get(row["file"], {}).get("output_mb"),
        }
        synced.append(meta)
    return synced


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Optimize Q-Flex flight MP4s for Sentinel HUD")
    ap.add_argument("--input-dir", type=Path, default=Path(r"C:\111\1001\1-10"))
    ap.add_argument("--crf", type=int, default=24)
    ap.add_argument("--preset", default="slow", choices=("slow", "slower", "medium", "fast"))
    ap.add_argument("--max-mb", type=float, default=2.0)
    ap.add_argument("--min-mb", type=float, default=1.5)
    ap.add_argument("--no-backup", action="store_true", help="Do not keep .mp4.source.bak")
    ap.add_argument(
        "--sync-repo",
        action="store_true",
        help="Copy optimized files → assets/7000/videos + output/assets/videos",
    )
    ap.add_argument("--only", nargs="*", help="Optional subset of filenames")
    args = ap.parse_args(argv)

    input_dir = args.input_dir
    if not input_dir.is_dir():
        print(f"FAIL: input dir missing: {input_dir}", file=sys.stderr)
        return 2

    ffmpeg = find_ffmpeg()
    max_bytes = int(args.max_mb * MB)
    min_bytes = int(args.min_mb * MB)
    files = QFLEX_FLIGHTS
    if args.only:
        only = set(args.only)
        files = [f for f in files if f["file"] in only]

    print(f"ffmpeg={ffmpeg}")
    print(f"input={input_dir}")
    print(f"target={args.min_mb:.1f}-{args.max_mb:.1f} MB  crf={args.crf} preset={args.preset}")
    print(f"items={len(files)}")

    results: list[dict[str, Any]] = []
    t0 = time.time()
    for row in files:
        src = input_dir / row["file"]
        print(f"\n>> {row['name']} ({row['imo']})  {row['file']}")
        rec = optimize_file(
            ffmpeg,
            src,
            crf=args.crf,
            preset=args.preset,
            max_bytes=max_bytes,
            min_bytes=min_bytes,
            keep_backup=not args.no_backup,
        )
        rec["imo"] = row["imo"]
        rec["name"] = row["name"]
        results.append(rec)
        if rec.get("ok"):
            flag = "OK" if rec.get("under_2mb") else "OVER"
            print(
                f"  {flag}  {rec['output_mb']:.2f} MB  crf={rec['crf']}  "
                f"max_w={rec.get('max_w')}  fps={rec.get('probe', {}).get('fps')}"
            )
        else:
            print(f"  FAIL  {rec.get('error')}")

    synced: list[dict[str, Any]] = []
    if args.sync_repo:
        print("\n>> sync-repo -> assets/7000/videos + output/assets/videos")
        # Fix mkdir typo path — ensure dirs exist
        ASSET_VIDEOS.mkdir(parents=True, exist_ok=True)
        OUT_VIDEOS.mkdir(parents=True, exist_ok=True)
        synced = sync_to_repo(input_dir, results)

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ffmpeg": ffmpeg,
        "input_dir": str(input_dir),
        "crf_start": args.crf,
        "preset": args.preset,
        "target_mb": [args.min_mb, args.max_mb],
        "elapsed_sec": round(time.time() - t0, 1),
        "note": (
            "Sources are typically 24 fps @ 1440p — frame rate preserved "
            "(do not upsample to 60 fps: invents frames and bloats bitrate)."
        ),
        "results": results,
        "synced": synced,
        "summary": {
            "ok": sum(1 for r in results if r.get("ok")),
            "fail": sum(1 for r in results if not r.get("ok")),
            "under_2mb": sum(1 for r in results if r.get("under_2mb")),
        },
    }
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "qflex_video_optimize.json"
    log_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n=== SUMMARY ok={report['summary']['ok']} under_2mb={report['summary']['under_2mb']} ===")
    for r in results:
        if r.get("ok"):
            print(f"  {r['file']}: {r['output_mb']:.2f} MB")
    print(f"report -> {log_path}")
    return 0 if report["summary"]["fail"] == 0 else 1


if __name__ == "__main__":
    # Fix accidental typo guard if present in older draft
    raise SystemExit(main())
