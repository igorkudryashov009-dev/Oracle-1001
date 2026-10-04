"""Non-blocking exclusive lock (flock -n on Linux, msvcrt on Windows)."""

from __future__ import annotations

from pathlib import Path
from typing import IO

ROOT = Path(__file__).resolve().parents[1]
LOCK_DIR = ROOT / "data" / "archive" / "locks"


def try_lock(name: str) -> IO[str] | None:
    """Return an open handle holding the lock, or None if another holder has it."""
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    path = LOCK_DIR / f"{name}.lock"
    fh = path.open("a+", encoding="utf-8")
    try:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except ImportError:
        pass
    except OSError:
        fh.close()
        return None
    try:
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        return fh
    except OSError:
        fh.close()
        return None


def release_lock(fh: IO[str] | None) -> None:
    if fh is None:
        return
    try:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except ImportError:
        try:
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    except OSError:
        pass
    try:
        fh.close()
    except OSError:
        pass
