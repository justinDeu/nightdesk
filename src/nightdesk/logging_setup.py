"""Centralized logging setup for the API and worker processes.

Both daemons need:

- Stdout/stderr at INFO so journald captures them (systemd's
  ``StandardOutput=journal`` does the routing).
- A rotating on-disk file for users who don't know journalctl. Defaults
  to ``~/.local/share/nightdesk/logs/{api,worker}.log``, 10 MB x 5
  backups.

Per-run log capture is separate: ``per_run_log_handler`` returns a
file handler scoped to a single run id. The worker attaches it for the
duration of one run and removes it on completion so log lines from
different runs don't bleed together.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path
from typing import Optional


_DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DEFAULT_LOG_DIR = Path(os.path.expanduser("~/.local/share/nightdesk/logs"))


def configure_root_logging(
    *,
    component: str,
    log_dir: Optional[Path] = None,
    level: int = logging.INFO,
) -> Path:
    """Wire console + rotating-file handlers onto the root logger.

    Returns the resolved log file path so callers (e.g. ``nightdesk setup``)
    can tell the user where logs went.

    Safe to call more than once; existing handlers of the same kind are
    replaced rather than duplicated, which keeps reloads from spamming
    multiple lines per message.
    """
    root = logging.getLogger()
    root.setLevel(level)

    # Drop any prior nightdesk handlers so a reimport doesn't double-log.
    for h in list(root.handlers):
        if getattr(h, "_nightdesk", False):
            root.removeHandler(h)

    formatter = logging.Formatter(_DEFAULT_FORMAT)

    stream = logging.StreamHandler()
    stream.setLevel(level)
    stream.setFormatter(formatter)
    stream._nightdesk = True  # type: ignore[attr-defined]
    root.addHandler(stream)

    log_dir = log_dir or _DEFAULT_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{component}.log"
    rotating = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    rotating.setLevel(level)
    rotating.setFormatter(formatter)
    rotating._nightdesk = True  # type: ignore[attr-defined]
    root.addHandler(rotating)
    return log_path


def per_run_log_handler(
    run_id: str,
    *,
    log_dir: Optional[Path] = None,
    level: int = logging.INFO,
) -> logging.Handler:
    """Build a file handler that captures all log records for one run.

    Caller is responsible for ``root_logger.addHandler(h)`` before the
    run starts and ``root_logger.removeHandler(h); h.close()`` after.
    The file is ``<log_dir>/runs/<run_id>.log``.
    """
    log_dir = log_dir or _DEFAULT_LOG_DIR
    runs_dir = log_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(runs_dir / f"{run_id}.log", encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT))
    handler._nightdesk_run_id = run_id  # type: ignore[attr-defined]
    return handler


def run_log_path(run_id: str, log_dir: Optional[Path] = None) -> Path:
    """Where the per-run log lives. Used by the UI 'download log' link."""
    log_dir = log_dir or _DEFAULT_LOG_DIR
    return log_dir / "runs" / f"{run_id}.log"
