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
_PER_RUN_FORMAT = "%(asctime)s %(levelname)s %(name)s [run:%(run_id)s] %(message)s"
_DEFAULT_LOG_DIR = Path(os.path.expanduser("~/.local/share/nightdesk/logs"))

_log = logging.getLogger(__name__)


def _resolve_log_dir(log_dir: Optional[Path]) -> Path:
    """Resolve the log directory.

    Precedence: explicit arg > load_config() (file + env + data_dir
    derivation) > built-in default.

    Reading the config file here (rather than re-implementing the env-var
    branches) keeps a single source of truth for path resolution, so that
    users who relocate logs via ``data_dir`` / ``log_dir`` in config.toml
    get the same answer from every caller, not just the ones that pass an
    explicit ``log_dir``. A lazy import dodges any import-cycle risk, and
    a narrow except guards callers that hit this before config is
    readable (tomli errors are ValueError subclasses).
    """
    if log_dir is not None:
        return log_dir
    try:
        from nightdesk.config import load_config

        return load_config().log_dir
    except (OSError, ValueError):
        _log.warning("could not read log_dir from config; falling back to %s", _DEFAULT_LOG_DIR, exc_info=True)
        return _DEFAULT_LOG_DIR


class RunIdFilter(logging.Filter):
    """Inject ``run_id`` into every log record passing through.

    Attached to the per-run file handler so lines written to the shared
    worker.log *and* the per-run log are attributable to a specific run.
    For the shared handler the format string picks up ``%(run_id)s`` only
    when the filter is present (the default format omits it).
    """

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id  # type: ignore[attr-defined]
        return True


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

    log_dir = _resolve_log_dir(log_dir)
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
    log_dir = _resolve_log_dir(log_dir)
    runs_dir = log_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(runs_dir / f"{run_id}.log", encoding="utf-8")
    handler.setLevel(level)
    handler.addFilter(RunIdFilter(run_id))
    handler.setFormatter(logging.Formatter(_PER_RUN_FORMAT))
    handler._nightdesk_run_id = run_id  # type: ignore[attr-defined]
    handler._nightdesk = True  # type: ignore[attr-defined]
    return handler


def run_log_path(run_id: str, log_dir: Optional[Path] = None) -> Path:
    """Where the per-run log lives. Used by the UI 'download log' link."""
    log_dir = _resolve_log_dir(log_dir)
    return log_dir / "runs" / f"{run_id}.log"
