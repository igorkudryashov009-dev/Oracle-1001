#!/usr/bin/env python3
"""Compress Q-Flex REAL VIDEO MP4s for Sentinel HUD (<2 MB, faststart, no audio).

Web-optimized H.264 @ ~1080p · CRF 24 · preset slow · -movflags +faststart · -an

Usage:
  python scripts/compress_qflex_videos.py
  python scripts/compress_qflex_videos.py --dir "C:\\111\\1001\\1-10"
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_DIR = Path(r"C:\111\1001\1-10")
MB = 1024 * 1024
TARGET_MAX_BYTES = int(2.0 * MB)

# Strict Q-Flex flight SoT filenames (do not invent extras).
QFLEX_FILES = [
    "bu-samra-parallel-flight-1.mp4",
    "mekaines-parallel-flight-2.mp4",
    "al-mafyar-parallel-flight-3.mp4",
    "al-kharaitiyat-parallel-flight-4.mp4",
    "mozah-parallel-flight-5.mp4",
    "umm-slal-parallel-flight-6.mp4",
    "al-ghuwairiya-digital-twin-flight-7.mp4",
    "lijmiliya-parallel-flight-8.mp4",
    "al-samriya-parallel-flight-9.mp4",
    "al-mayeda-digital-twin-flight-10.mp4",
]


def find_ffmpeg() -> str:
    which = shutil.which("ffmpeg")
    if which:
        return which
    tools = ROOT / "tools" / "ffmpeg.exe"
    if tools.is_file():
        return str(tools)
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"ffmpeg not found ({exc})") from exc


def compress_video(ffmpeg: str, input_path: Path, *, crf: int, preset: str) -> dict:
    if not input_path.is_file():
        return {"file": input_path.name, "ok": False, "error": "missing"}

    # Prefer .source.bak as true master when present (avoid generational re-encode).
    bak = input_path.with_suffix(input_path.suffix + ".source.bak")
    src = bak if bak.is_file() else input_path
    if not bak.is_file():
        shutil.copy2(input_path, bak)

    tmp = input_path.with_name(input_path.stem + "_compressed.mp4")
    attempts: list[dict] = []
    plans: list[tuple[int, str]] = [
        (crf, "scale=1920:-2:flags=lanczos"),
        (min(crf + 2, 28), "scale=1920:-2:flags=lanczos"),
        (28, "scale=1920:-2:flags=lanczos"),
        (28, "scale=1600:-2:flags=lanczos"),
        (30, "scale=1600:-2:flags=lanczos"),
    ]
    used_crf = crf
    used_vf = plans[0][1]
    size = 0

    for try_crf, vf in plans:
        if tmp.exists():
            tmp.unlink()
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-y",
            "-i",
            str(src),
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-level",
            "4.1",
            "-pix_fmt",
            "yuv420p",
            "-crf",
            str(try_crf),
            "-preset",
            preset,
            "-vf",
            vf,
            "-an",
            "-movflags",
            "+faststart",
            str(tmp),
        ]
        print(f"Compressing: {input_path.name} (crf={try_crf}, vf={vf}) ...")
        res = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if res.returncode != 0 or not tmp.is_file():
            err = (res.stderr or res.stdout or "")[-400:]
            attempts.append({"crf": try_crf, "vf": vf, "error": err})
            continue
        size = tmp.stat().st_size
        attempts.append({"crf": try_crf, "vf": vf, "bytes": size})
        used_crf = try_crf
        used_vf = vf
        if size <= TARGET_MAX_BYTES:
            break

    if not tmp.is_file():
        return {"file": input_path.name, "ok": False, "error": "encode_failed", "attempts": attempts}

    orig_size = bak.stat().st_size if bak.is_file() else input_path.stat().st_size
    os.replace(tmp, input_path)
    new_size = input_path.stat().st_size
    print(
        f"Done: {orig_size / MB:.2f}MB -> {new_size / MB:.2f}MB  "
        f"(crf={used_crf}, under_2mb={new_size <= TARGET_MAX_BYTES})"
    )
    return {
        "file": input_path.name,
        "ok": True,
        "source_bytes": orig_size,
        "output_bytes": new_size,
        "output_mb": round(new_size / MB, 3),
        "crf": used_crf,
        "vf": used_vf,
        "under_2mb": new_size <= TARGET_MAX_BYTES,
        "backup": str(bak),
        "attempts": attempts,
    }


def sync_repo_copies(video_dir: Path) -> list[dict]:
    """Refresh assets/7000/videos + assets/1-10 + output/assets/videos."""
    asset_dir = ROOT / "assets" / "7000" / "videos"
    out_dir = ROOT / "output" / "assets" / "videos"
    mirror = ROOT / "assets" / "1-10"
    asset_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    mirror.mkdir(parents=True, exist_ok=True)

    from services.top10_vessels import QFLEX_FLIGHT_FILES  # local import after path setup

    synced: list[dict] = []
    for imo, fname in QFLEX_FLIGHT_FILES.items():
        src = video_dir / fname
        if not src.is_file():
            synced.append({"imo": imo, "ok": False, "error": "missing"})
            continue
        dest_flight = f"vessel_{imo}_flight.mp4"
        shutil.copy2(src, asset_dir / dest_flight)
        shutil.copy2(src, out_dir / dest_flight)
        shutil.copy2(src, mirror / fname)
        if imo == "9372743":
            shutil.copy2(src, asset_dir / "vessel_9372743_luma.mp4")
            shutil.copy2(src, out_dir / "vessel_9372743_luma.mp4")
        synced.append({"imo": imo, "ok": True, "bytes": src.stat().st_size, "file": fname})
    return synced


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compress Q-Flex flight MP4s for HUD")
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--crf", type=int, default=24)
    ap.add_argument("--preset", default="slow")
    ap.add_argument("--sync-repo", action="store_true", default=True)
    ap.add_argument("--no-sync-repo", action="store_true")
    args = ap.parse_args(argv)

    video_dir = args.dir
    if not video_dir.is_dir():
        print(f"Directory {video_dir} not found!")
        return 1

    ffmpeg = find_ffmpeg()
    print(f"ffmpeg={ffmpeg}")
    print(f"dir={video_dir}")
    t0 = time.time()
    results: list[dict] = []
    for name in QFLEX_FILES:
        path = video_dir / name
        if not path.exists():
            # Also accept any .mp4 matching name without requiring list completeness
            print(f"Skip missing: {name}")
            results.append({"file": name, "ok": False, "error": "missing"})
            continue
        results.append(compress_video(ffmpeg, path, crf=args.crf, preset=args.preset))

    synced: list[dict] = []
    if args.sync_repo and not args.no_sync_repo:
        print("\nSyncing repo video copies...")
        synced = sync_repo_copies(video_dir)

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dir": str(video_dir),
        "elapsed_sec": round(time.time() - t0, 1),
        "results": results,
        "synced": synced,
        "summary": {
            "ok": sum(1 for r in results if r.get("ok")),
            "fail": sum(1 for r in results if not r.get("ok")),
            "under_2mb": sum(1 for r in results if r.get("under_2mb")),
        },
    }
    log = ROOT / "logs" / "qflex_video_compress.json"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        f"\nSUMMARY ok={report['summary']['ok']} "
        f"under_2mb={report['summary']['under_2mb']} "
        f"fail={report['summary']['fail']}"
    )
    print(f"report -> {log}")
    return 0 if report["summary"]["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
