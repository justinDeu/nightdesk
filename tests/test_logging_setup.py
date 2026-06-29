"""Tests for log-dir resolution in nightdesk.logging_setup.

Covers the bug where no-arg callers (per-run handler, UI download path)
resolved against env/default and ignored ``log_dir`` / ``data_dir`` set
in config.toml. The fix routes them through ``load_config().log_dir``,
the same precedence chain the daemons already use.
"""
import logging
from pathlib import Path

from nightdesk.logging_setup import per_run_log_handler, run_log_path


def _clear_path_env(monkeypatch):
    """Strip path-related NIGHTDESK_* env vars so tests start clean."""
    for key in (
        "NIGHTDESK_DATA_DIR",
        "NIGHTDESK_LOG_DIR",
        "NIGHTDESK_DB_PATH",
        "NIGHTDESK_TRANSCRIPT_ROOT",
    ):
        monkeypatch.delenv(key, raising=False)


def test_run_log_path_follows_config_log_dir(tmp_path, monkeypatch):
    """``log_dir`` in config.toml drives run_log_path() with no explicit arg."""
    _clear_path_env(monkeypatch)
    logs = tmp_path / "mylogs"
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(f'log_dir = "{logs}"\n')
    monkeypatch.setenv("NIGHTDESK_CONFIG", str(cfg_file))

    path = run_log_path("rid")

    assert path == logs / "runs" / "rid.log"


def test_run_log_path_follows_config_data_dir(tmp_path, monkeypatch):
    """``data_dir`` in config.toml derives the log dir for run_log_path()."""
    _clear_path_env(monkeypatch)
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(f'data_dir = "{tmp_path}"\n')
    monkeypatch.setenv("NIGHTDESK_CONFIG", str(cfg_file))

    path = run_log_path("rid")

    assert path == tmp_path / "logs" / "runs" / "rid.log"


def test_per_run_handler_follows_config_log_dir(tmp_path, monkeypatch):
    """per_run_log_handler() writes under the configured log dir."""
    _clear_path_env(monkeypatch)
    logs = tmp_path / "mylogs"
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(f'log_dir = "{logs}"\n')
    monkeypatch.setenv("NIGHTDESK_CONFIG", str(cfg_file))

    handler = per_run_log_handler("rid")
    try:
        base = Path(handler.baseFilename)
        assert base == logs / "runs" / "rid.log"
        assert base.is_file()
    finally:
        handler.close()


def test_explicit_log_dir_arg_beats_config(tmp_path, monkeypatch):
    """Explicit log_dir arg wins over the file/env precedence chain."""
    _clear_path_env(monkeypatch)
    configured = tmp_path / "configured"
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(f'log_dir = "{configured}"\n')
    monkeypatch.setenv("NIGHTDESK_CONFIG", str(cfg_file))

    other = tmp_path / "elsewhere"
    path = run_log_path("rid", log_dir=other)

    assert path == other / "runs" / "rid.log"
    assert path != configured / "runs" / "rid.log"


def test_resolve_log_dir_falls_back_to_default_on_unreadable_config(tmp_path, monkeypatch, caplog):
    """Malformed config (ValueError from tomli) falls back to the built-in default.

    The fallback also emits a warning so a malformed config.toml does not
    silently strand per-run logs in the built-in default dir.
    """
    _clear_path_env(monkeypatch)
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("this is not = = valid toml\n")
    monkeypatch.setenv("NIGHTDESK_CONFIG", str(cfg_file))

    from nightdesk.logging_setup import _DEFAULT_LOG_DIR, _resolve_log_dir

    with caplog.at_level(logging.WARNING, logger="nightdesk.logging_setup"):
        resolved = _resolve_log_dir(None)

    assert resolved == _DEFAULT_LOG_DIR
    assert any(
        "could not read log_dir" in record.message and record.levelno == logging.WARNING
        for record in caplog.records
    )
