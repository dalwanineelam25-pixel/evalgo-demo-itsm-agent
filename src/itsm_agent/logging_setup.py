"""Standard logging configuration: a rotating log file plus optional stderr output."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(log_path: Path, level: str = "INFO", console: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3)
    fh.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(fh)
    if console:
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(sh)
    # Third-party request logging is noisy at INFO.
    for name in ("httpx", "httpcore", "anthropic"):
        logging.getLogger(name).setLevel(logging.WARNING)
