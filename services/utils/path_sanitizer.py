"""Universal path sanitizer for analytical JSON / health artifacts.

Strips host-specific Windows absolute paths so Linux containers can consume
outputs written on a developer workstation.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_UNC_RE = re.compile(r"^\\\\")
_PROJECT_MARKERS = ("Oracle-1001", "oracle-1001", "7000")


def _as_posix_str(value: str) -> str:
    text = value.replace("\\", "/")
    # Collapse duplicate slashes (keep http:// intact)
    if "://" in text:
        scheme, rest = text.split("://", 1)
        while "//" in rest:
            rest = rest.replace("//", "/")
        return f"{scheme}://{rest}"
    while "//" in text:
        text = text.replace("//", "/")
    return text


def _try_relative_to_root(path_str: str, root: Path) -> str | None:
    """Return POSIX relative path (./...) if path is under project root."""
    norm = _as_posix_str(path_str)
    try:
        p = Path(path_str)
        rel = p.resolve().relative_to(root.resolve())
        return "./" + PurePosixPath(rel.as_posix()).as_posix()
    except (ValueError, OSError):
        pass

    root_posix = _as_posix_str(str(root.resolve()))
    lower = norm.lower()
    root_lower = root_posix.lower()
    if root_lower in lower:
        idx = lower.index(root_lower) + len(root_lower)
        tail = norm[idx:].lstrip("/")
        return "./" + tail if tail else "./"

    for marker in ("/Oracle-1001/7000/", "/oracle-1001/7000/", "/7000/"):
        if marker.lower() in lower:
            idx = lower.index(marker.lower()) + len(marker)
            tail = norm[idx:].lstrip("/")
            return "./" + tail if tail else "./"
    return None


def sanitize_path_string(value: str, *, root: Path | None = None) -> str:
    """Normalize a single path-like string to container-safe relative POSIX form."""
    if not value or not isinstance(value, str):
        return value

    root = root or ROOT
    text = value.strip()
    if not text:
        return text

    # Leave network URLs alone (except file:// which may embed Windows paths)
    lower = text.lower()
    if lower.startswith(("http://", "https://", "wss://", "ws://")):
        return text
    if lower.startswith("file:"):
        body = re.sub(r"^file:/+", "", text, flags=re.I)
        rel = _try_relative_to_root(body, root)
        return rel if rel else _as_posix_str(body)

    looks_like_path = (
        bool(_DRIVE_RE.match(text))
        or bool(_UNC_RE.match(text))
        or ("\\" in text)
        or any(m in text for m in _PROJECT_MARKERS)
        or text.startswith(("/", "./", "../"))
        or (
            "/" in text
            and any(
                token in text.lower()
                for token in (
                    ".db",
                    ".json",
                    ".cbm",
                    ".parquet",
                    ".html",
                    ".md",
                    ".css",
                    ".js",
                    ".csv",
                    ".xlsx",
                    "output/",
                    "models/",
                    "история",
                )
            )
        )
    )
    if not looks_like_path:
        return text

    rel = _try_relative_to_root(text, root)
    if rel is not None:
        return rel

    posix = _as_posix_str(text)
    if _DRIVE_RE.match(text):
        for marker in ("7000", "Oracle-1001", "oracle-1001"):
            if marker in posix:
                idx = posix.index(marker) + len(marker)
                # If marker is Oracle-1001, continue to 7000 when present
                if marker.lower().startswith("oracle") and "/7000" in posix[idx:].lower():
                    idx2 = posix.lower().index("/7000", idx) + len("/7000")
                    tail = posix[idx2:].lstrip("/")
                    return "./" + tail if tail else "./"
                tail = posix[idx:].lstrip("/")
                return "./" + tail if tail else "./"
        return "./" + posix.split(":", 1)[-1].lstrip("/")
    return posix


def sanitize_structure(obj: Any, *, root: Path | None = None) -> Any:
    """Recursively sanitize path strings inside dict/list/tuple structures."""
    root = root or ROOT
    if isinstance(obj, dict):
        return {k: sanitize_structure(v, root=root) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_structure(v, root=root) for v in obj]
    if isinstance(obj, tuple):
        return tuple(sanitize_structure(v, root=root) for v in obj)
    if isinstance(obj, Path):
        return sanitize_path_string(str(obj), root=root)
    if isinstance(obj, str):
        return sanitize_path_string(obj, root=root)
    return obj


def dumps_sanitized(obj: Any, *, root: Path | None = None, **json_kwargs: Any) -> str:
    """json.dumps after sanitizing path strings."""
    import json

    clean = sanitize_structure(obj, root=root)
    return json.dumps(clean, **json_kwargs)


__all__ = [
    "ROOT",
    "sanitize_path_string",
    "sanitize_structure",
    "dumps_sanitized",
]
