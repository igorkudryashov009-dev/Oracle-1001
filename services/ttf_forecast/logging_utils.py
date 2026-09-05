"""Shared logging for TTF quantitative engines → logs/quant_engine.log"""

from __future__ import annotations

import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG_PATH = ROOT / "logs" / "quant_engine.log"


def get_quant_logger(name: str = "ttf.quant") -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    # Attach file handler once per process for this log path
    marker = str(LOG_PATH.resolve())
    has_file = False
    for h in logger.handlers:
        if isinstance(h, logging.FileHandler) and Path(getattr(h, "baseFilename", "")).resolve() == Path(marker):
            has_file = True
            break
    if not has_file:
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(fh)
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in logger.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(sh)
    # Propagate to root only if needed; keep package-local
    logger.propagate = False
    return logger
