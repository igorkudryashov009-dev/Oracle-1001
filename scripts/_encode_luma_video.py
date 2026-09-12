#!/usr/bin/env python3
"""Compress FRAIJAH LUMA.ai source MP4 (do not overwrite the original)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMO = "9372743"
SRC = ROOT / "assets" / "7000" / "videos" / f"vessel_{IMO}_luma_source.mp4"
OUT = ROOT / "output" / "assets" / "videos" / f"vessel_{IMO}_luma.mp4"
ASSET = ROOT / "assets" / "7000" / "videos" / f"vessel_{IMO}_luma.mp4"
LOG = ROOT / "logs" / "luma_video_encode.json"


def find_ffmpeg() -> str:
    for cand in (
        shutil.which("ffmpeg"),
        str(ROOT / "tools" / "ffmpeg.exe"),
    ):
        if cand and Path(cand).exists():
            return cand
    # Docker HUD container (bind-mounts assets/7000)
    try:
        r = subprocess.run(
            ["docker", "exec", "sentinel-web", "which", "ffmpeg"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if r.returncode == 0 and r.stdout.strip():
            return "docker:sentinel-web"
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise SystemExit("ffmpeg not found (PATH / tools/ffmpeg.exe / docker exec sentinel-web)")


def probe(ff: str, src_in_container: bool) -> dict:
    args_base = ["-hide_banner", "-i", str(SRC) if not src_in_container else f"/app/assets/7000/videos/{SRC.name}"]
    if ff == "docker:sentinel-web":
        cmd = ["docker", "exec", "sentinel-web", "ffmpeg", *args_base]
    else:
        cmd = [ff, *args_base]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    text = (r.stderr or "") + (r.stdout or "")
    has_audio = "Audio:" in text
    width = None
    import re

    m = re.search(r"(\d{3,5})x(\d{3,5})", text)
    if m:
        width = int(m.group(1))
    dur = None
    md = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", text)
    if md:
        dur = int(md.group(1)) * 3600 + int(md.group(2)) * 60 + float(md.group(3))
    return {"has_audio": has_audio, "width": width, "duration_s": dur, "probe": text[-2500:]}


def encode(ff: str, crf: int, src_w: int | None) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    target_w = 1280
    if src_w and src_w < 1280:
        target_w = src_w
    vf = f"scale={target_w}:-2"
    common = [
        "-an",
        "-c:v",
        "libx264",
        "-crf",
        str(crf),
        "-preset",
        "slow",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-vf",
        vf,
        "-y",
    ]
    if ff == "docker:sentinel-web":
        src_c = f"/app/assets/7000/videos/{SRC.name}"
        tmp_c = f"/tmp/vessel_{IMO}_luma.mp4"
        cmd = ["docker", "exec", "sentinel-web", "ffmpeg", "-i", src_c, *common, tmp_c]
        subprocess.check_call(cmd)
        subprocess.check_call(["docker", "cp", f"sentinel-web:{tmp_c}", str(OUT)])
    else:
        cmd = [ff, "-i", str(SRC), *common, str(OUT)]
        subprocess.check_call(cmd)
    shutil.copy2(OUT, ASSET)


def main() -> int:
    if not SRC.exists():
        raise SystemExit(f"missing source {SRC}")
    ff = find_ffmpeg()
    info = probe(ff, src_in_container=(ff == "docker:sentinel-web"))
    crf = 28
    encode(ff, crf, info.get("width"))
    size = OUT.stat().st_size
    if size > 1_500_000:
        crf = 32
        encode(ff, crf, info.get("width"))
        size = OUT.stat().st_size
    src_b = SRC.stat().st_size
    report = {
        "imo": IMO,
        "ffmpeg": ff,
        "source_bytes": src_b,
        "output_bytes": size,
        "ratio": round(size / max(src_b, 1), 4),
        "crf": crf,
        "has_audio_source": info.get("has_audio"),
        "audio_stripped": True,
        "source_width": info.get("width"),
        "duration_s": info.get("duration_s"),
        "output": str(OUT),
        "asset_copy": str(ASSET),
    }
    LOG.parent.mkdir(exist_ok=True)
    LOG.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
