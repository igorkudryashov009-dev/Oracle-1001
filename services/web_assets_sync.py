"""Atomic web → output asset sync (JS/CSS) for Oracle-1001 / Sentinel.

Prevents deployment drift: manual edits in web/js|css must land in output/.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Any

ROOT_DEFAULT = Path(__file__).resolve().parents[1]


def md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def md5_file(path: Path) -> str:
    return md5_bytes(path.read_bytes())


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def atomic_write_bytes(dst: Path, data: bytes) -> None:
    """Write via temp file in the same directory, then os.replace (atomic on Win/POSIX)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, dst)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def _iter_source_assets(root: Path) -> list[tuple[Path, Path, str]]:
    """Return list of (src, dst, kind) for managed web assets.

    kind: 'js' | 'css' | 'css_dual' (also mirrored into output/js for HTML hrefs).
    """
    web = root / "web"
    out_js = root / "output" / "js"
    out_css = root / "output" / "css"
    pairs: list[tuple[Path, Path, str]] = []

    web_js = web / "js"
    if web_js.is_dir():
        for src in sorted(web_js.glob("*.js")):
            pairs.append((src, out_js / src.name, "js"))

    web_css = web / "css"
    if web_css.is_dir():
        for src in sorted(web_css.glob("*.css")):
            pairs.append((src, out_css / src.name, "css"))
            # HTML references href="js/sentinel_hud.css" — dual deploy
            pairs.append((src, out_js / src.name, "css_dual"))

    # Root-level web/*.js and web/*.css (engine, premium themes, etc.)
    if web.is_dir():
        web_js_names = {p.name for p in web_js.glob("*.js")} if web_js.is_dir() else set()
        for src in sorted(web.glob("*.js")):
            # Prefer web/js/<name> as SoT for output/js/<name> (avoid clobber
            # by thin re-exports like web/oracle_sheet.js).
            if src.name in web_js_names:
                if src.name == "oracle_sheet.js":
                    pairs.append((src, root / "output" / "oracle_sheet.js", "js_root_mirror"))
                continue
            pairs.append((src, out_js / src.name, "js"))
        for src in sorted(web.glob("*.css")):
            pairs.append((src, out_js / src.name, "css_dual"))
            pairs.append((src, out_css / src.name, "css"))

    # 3D assets & voxel data (web/assets/3d_models/ -> output/assets/3d_models/)
    web_3d = web / "assets" / "3d_models"
    out_3d = root / "output" / "assets" / "3d_models"
    if web_3d.is_dir():
        for src in sorted(web_3d.iterdir()):
            if src.is_file() and src.suffix.lower() in (".glb", ".json", ".gltf"):
                pairs.append((src, out_3d / src.name, "3d_model"))

    return pairs


def sync_one(src: Path, dst: Path, *, force: bool = False) -> dict[str, Any]:
    """Copy src→dst atomically if MD5 differs (or force)."""
    data = src.read_bytes()
    src_md5 = md5_bytes(data)
    src_sha = sha256_bytes(data)
    size = len(data)
    rel_src = src.as_posix()
    rel_dst = dst.as_posix()

    if dst.is_file() and not force:
        try:
            existing = dst.read_bytes()
            if md5_bytes(existing) == src_md5:
                return {
                    "status": "SKIP",
                    "src": rel_src,
                    "dst": rel_dst,
                    "bytes": size,
                    "md5": src_md5,
                    "sha256": src_sha,
                }
        except OSError:
            pass

    atomic_write_bytes(dst, data)
    return {
        "status": "OK",
        "src": rel_src,
        "dst": rel_dst,
        "bytes": size,
        "md5": src_md5,
        "sha256": src_sha,
    }


def format_sync_line(result: dict[str, Any], root: Path | None = None) -> str:
    """Human log: [SYNC] web/js/x.js (N bytes) -> output/js/x.js OK|SKIP md5=..."""
    root = root or ROOT_DEFAULT

    def _rel(p: str) -> str:
        try:
            return Path(p).resolve().relative_to(root.resolve()).as_posix()
        except Exception:
            return Path(p).as_posix()

    src = _rel(str(result["src"]))
    dst = _rel(str(result["dst"]))
    status = result["status"]
    return (
        f"[SYNC] {src} ({result['bytes']} bytes) -> {dst} {status} "
        f"md5={result['md5'][:12]}"
    )


def sync_web_assets(
    root: Path | None = None,
    *,
    force: bool = False,
    log: bool = True,
) -> list[dict[str, Any]]:
    """Sync ALL managed JS/CSS from web/ → output/ with MD5 gate + atomic write."""
    root = (root or ROOT_DEFAULT).resolve()
    results: list[dict[str, Any]] = []
    for src, dst, _kind in _iter_source_assets(root):
        if not src.is_file():
            continue
        result = sync_one(src, dst, force=force)
        results.append(result)
        if log:
            print(format_sync_line(result, root), flush=True)
    if log:
        copied = sum(1 for r in results if r["status"] == "OK")
        skipped = sum(1 for r in results if r["status"] == "SKIP")
        print(
            f"[SYNC] complete: {copied} written, {skipped} unchanged, {len(results)} total",
            flush=True,
        )
    return results


def verify_sha256_pairs(root: Path | None = None) -> tuple[bool, list[dict[str, Any]]]:
    """Compare SHA-256 of every managed source file vs its primary output target.

    For CSS dual-deploy, both output/css and output/js must match source.
    """
    root = (root or ROOT_DEFAULT).resolve()
    rows: list[dict[str, Any]] = []
    ok = True
    for src, dst, kind in _iter_source_assets(root):
        if not src.is_file():
            rows.append(
                {
                    "ok": False,
                    "src": src.as_posix(),
                    "dst": dst.as_posix(),
                    "kind": kind,
                    "error": "missing_source",
                }
            )
            ok = False
            continue
        src_sha = sha256_file(src)
        if not dst.is_file():
            rows.append(
                {
                    "ok": False,
                    "src": src.as_posix(),
                    "dst": dst.as_posix(),
                    "kind": kind,
                    "src_sha256": src_sha,
                    "error": "missing_dest",
                }
            )
            ok = False
            continue
        dst_sha = sha256_file(dst)
        match = src_sha == dst_sha
        if not match:
            ok = False
        rows.append(
            {
                "ok": match,
                "src": src.as_posix(),
                "dst": dst.as_posix(),
                "kind": kind,
                "src_sha256": src_sha,
                "dst_sha256": dst_sha,
                "bytes": src.stat().st_size,
            }
        )
    return ok, rows


def ensure_file_synced(root: Path, rel_under_output: str) -> Path | None:
    """For --dev HTTP: if request is under output/js|css, sync matching web source.

    Returns the path that should be served (prefer live web source when present).
    """
    rel = rel_under_output.replace("\\", "/").lstrip("/")
    # output/js/foo.js or output/css/foo.css
    parts = rel.split("/")
    if len(parts) < 3 or parts[0] != "output":
        return None
    bucket, name = parts[1], parts[-1]
    if ".." in parts:
        return None

    web = root / "web"
    candidates: list[Path] = []
    if bucket == "js":
        candidates = [web / "js" / name, web / name, web / "css" / name]
    elif bucket == "css":
        candidates = [web / "css" / name, web / name]
    else:
        return None

    for src in candidates:
        if src.is_file():
            # Keep output warm for non-dev consumers / CDN mirrors
            dst = root / "output" / bucket / name
            if bucket == "js" and src.suffix.lower() == ".css":
                dst = root / "output" / "js" / name
            sync_one(src, dst)
            # Dual CSS into output/js when serving css from web/css
            if src.suffix.lower() == ".css":
                sync_one(src, root / "output" / "js" / name)
                sync_one(src, root / "output" / "css" / name)
            return src
    return None
