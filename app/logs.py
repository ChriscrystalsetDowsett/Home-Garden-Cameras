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


class _QuietAccessLog(logging.Filter):
    """Drop routine request lines (the wall makes ~170,000 a day per camera,
    which pushed real errors out of the journal within 1.5 days). Keeps
    client errors (4xx) and server errors other than 503, which is just a
    camera asleep for the night."""
    def filter(self, record):
        args = record.args if isinstance(record.args, tuple) else ()
        code = str(args[1]) if len(args) >= 2 else ""
        if code[:1] in ("2", "3") or code == "503":
            return False
        return True


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
    logging.getLogger("werkzeug").addFilter(_QuietAccessLog())
    _configured = True
