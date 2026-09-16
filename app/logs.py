"""Rotating structured logging for the app.

Wires the root logger to `data/logs/app.log` with size-bounded rotation so
long-running services don't fill the SD card. journalctl continues to
receive the same records via the stream handler.
"""
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


_configured = False


def configure(log_dir: Path, level=logging.INFO):
    """Install the rotating file handler + stdout stream handler.

    Safe to call multiple times — subsequent calls are no-ops.
    """
    global _configured
    if _configured:
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "app.log"

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(level)

    # Only attach handlers if the root logger is otherwise unconfigured;
    # pytest and the Flask dev server sometimes pre-configure their own.
    if not root.handlers:
        stream = logging.StreamHandler(stream=sys.stderr)
        stream.setFormatter(fmt)
        root.addHandler(stream)

    file_handler = RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    _configured = True
