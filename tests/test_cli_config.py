"""Tests for `nightdesk-config list` and `nightdesk-config set`."""

import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from nightdesk.cli import (
    _mask_token,
    _validate_value,
    _write_config_key,
    config_cmd,
)
from nightdesk.config import DEFAULT_CONFIG_PATH


# -- _mask_token tests -----------------------------------------------------


class TestMaskToken:
    def test_short_token(self):
        assert _mask_token("abc") == "***"

    def test_exactly_8_chars(self):
        assert _mask_token("12345678") == "********"

    def test_long_token(self):
        assert _mask_token("shortlongpart") == "*****longpart"


# -- _validate_value tests -------------------------------------------------


class TestValidateValue:
    def test_valid_port(self):
        assert _validate_value("bind_port", "9000") == "9000"

    def test_invalid_port(self):
        with pytest.raises(SystemExit):
            _validate_value("bind_port", "abc")

    def test_valid_max_parallel(self):
        assert _validate_value("max_parallel", "4") == "4"

    def test_invalid_max_parallel(self):
        with pytest.raises(SystemExit):
            _validate_value("max_parallel", "many")

    def test_valid_window_start(self):
        assert _validate_value("window_start", "22:00") == "22:00"

    def test_valid_window_end(self):
        assert _validate_value("window_end", "07:00") == "07:00"

    def test_invalid_window_start(self):
        with pytest.raises(SystemExit):
            _validate_value("window_start", "25:00")

    def test_invalid_window_end_format(self):
        with pytest.raises(SystemExit):
            _validate_value("window_end", "7:00")

    def test_string_key_passes_through(self):
        assert _validate_value("bind_host", "0.0.0.0") == "0.0.0.0"


# -- _write_config_key tests -----------------------------------------------


class TestWriteConfigKey:
    def test_overwrites_existing_string_key(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            'bearer_token = "old"\n'
            'bind_host = "127.0.0.1"\n'
            'bind_port = 8765\n'
        )
        monkeypatch.setattr("nightdesk.cli.DEFAULT_CONFIG_PATH", cfg_file)

        _write_config_key("bind_host", "0.0.0.0")

        text = cfg_file.read_text()
        assert 'bind_host = "0.0.0.0"' in text
        assert 'bearer_token = "old"' in text
        assert "bind_port = 8765" in text

    def test_appends_new_key(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text('bind_host = "127.0.0.1"\n')
        monkeypatch.setattr("nightdesk.cli.DEFAULT_CONFIG_PATH", cfg_file)

        _write_config_key("bind_host", "0.0.0.0")

        text = cfg_file.read_text()
        assert 'bind_host = "0.0.0.0"' in text

    def test_writes_integer_for_bind_port(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text('bind_host = "127.0.0.1"\nbind_port = 8765\n')
        monkeypatch.setattr("nightdesk.cli.DEFAULT_CONFIG_PATH", cfg_file)

        _write_config_key("bind_port", "9000")

        text = cfg_file.read_text()
        assert "bind_port = 9000" in text

    def test_fails_if_config_missing(self, tmp_path, monkeypatch):
        missing = tmp_path / "nope.toml"
        monkeypatch.setattr("nightdesk.cli.DEFAULT_CONFIG_PATH", missing)

        with pytest.raises(SystemExit):
            _write_config_key("bind_host", "0.0.0.0")

    def test_invalid_port_value_exits(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text('bind_port = 8765\n')
        monkeypatch.setattr("nightdesk.cli.DEFAULT_CONFIG_PATH", cfg_file)

        with pytest.raises(SystemExit):
            _write_config_key("bind_port", "abc")


# -- config_cmd integration tests ------------------------------------------


class TestConfigListCmd:
    def test_prints_file_config(self, tmp_path, monkeypatch, capsys):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text(
            'bearer_token = "tok_abcdefgh12345678"\n'
            'bind_host = "127.0.0.1"\n'
            'bind_port = 8765\n'
        )
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text("")

        from nightdesk.config import load_config as _real_load_config
        _fake_cfg = _real_load_config(config_path=cfg_file, secrets_path=secrets_file)

        # API unreachable — should still print file config.
        with patch("nightdesk.cli._api_get_config", return_value=None), \
             patch("nightdesk.cli.load_config", return_value=_fake_cfg):
            monkeypatch.setattr(sys, "argv", ["nightdesk-config", "list"])
            config_cmd()

        out = capsys.readouterr().out
        assert "bind_host = 127.0.0.1" in out
        assert "bind_port = 8765" in out
        assert "bearer_token = ************12345678" in out
        assert "window_start = (API unreachable)" in out

    def test_prints_runtime_config_when_api_available(self, tmp_path, monkeypatch, capsys):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text('bearer_token = "tok"\nbind_host = "127.0.0.1"\nbind_port = 8765\n')
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text("")

        from nightdesk.config import load_config as _real_load_config
        _fake_cfg = _real_load_config(config_path=cfg_file, secrets_path=secrets_file)

        fake_runtime = {"window_start": "22:00", "window_end": "07:00", "max_parallel": 4}
        with patch("nightdesk.cli._api_get_config", return_value=fake_runtime), \
             patch("nightdesk.cli.load_config", return_value=_fake_cfg):
            monkeypatch.setattr(sys, "argv", ["nightdesk-config", "list"])
            config_cmd()

        out = capsys.readouterr().out
        assert "window_start = 22:00" in out
        assert "window_end = 07:00" in out
        assert "max_parallel = 4" in out


class TestConfigSetCmd:
    def test_rejects_unknown_key(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["nightdesk-config", "set", "bogus", "val"])
        with pytest.raises(SystemExit):
            config_cmd()
        err = capsys.readouterr().err
        assert "Unknown config key 'bogus'" in err

    def test_writes_file_only_key(self, tmp_path, monkeypatch, capsys):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text('bind_host = "127.0.0.1"\nbind_port = 8765\n')
        monkeypatch.setattr("nightdesk.cli.DEFAULT_CONFIG_PATH", cfg_file)
        monkeypatch.setattr("nightdesk.config.DEFAULT_CONFIG_PATH", cfg_file)

        monkeypatch.setattr(sys, "argv", ["nightdesk-config", "set", "bind_host", "0.0.0.0"])
        config_cmd()

        out = capsys.readouterr().out
        assert "Written bind_host = 0.0.0.0" in out
        assert "requires a server restart" in out
        assert 'bind_host = "0.0.0.0"' in cfg_file.read_text()

    def test_writes_runtime_key_and_patches_api(self, tmp_path, monkeypatch, capsys):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text('bearer_token = "tok"\nbind_host = "127.0.0.1"\nbind_port = 8765\n')
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text("")

        from nightdesk.config import load_config as _real_load_config
        _fake_cfg = _real_load_config(config_path=cfg_file, secrets_path=secrets_file)

        with patch("nightdesk.cli._api_patch_config", return_value=True) as mock_patch, \
             patch("nightdesk.cli.load_config", return_value=_fake_cfg):
            monkeypatch.setattr(sys, "argv", ["nightdesk-config", "set", "window_start", "23:00"])
            config_cmd()
            mock_patch.assert_called_once()

        out = capsys.readouterr().out
        assert "Applied window_start = 23:00 to running server" in out

    def test_rejects_invalid_time_format(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["nightdesk-config", "set", "window_start", "25:00"])
        with pytest.raises(SystemExit):
            config_cmd()

    def test_rejects_invalid_integer(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["nightdesk-config", "set", "max_parallel", "abc"])
        with pytest.raises(SystemExit):
            config_cmd()

    def test_worktree_base_ref_writes_both(self, tmp_path, monkeypatch, capsys):
        cfg_file = tmp_path / "config.toml"
        cfg_file.write_text('bearer_token = "tok"\nbind_host = "127.0.0.1"\nbind_port = 8765\n')
        secrets_file = tmp_path / "secrets.env"
        secrets_file.write_text("")

        from nightdesk.config import load_config as _real_load_config
        _fake_cfg = _real_load_config(config_path=cfg_file, secrets_path=secrets_file)

        monkeypatch.setattr("nightdesk.cli.DEFAULT_CONFIG_PATH", cfg_file)

        with patch("nightdesk.cli._api_patch_config", return_value=True) as mock_patch, \
             patch("nightdesk.cli.load_config", return_value=_fake_cfg):
            monkeypatch.setattr(
                sys, "argv", ["nightdesk-config", "set", "worktree_base_ref", "origin/main"]
            )
            config_cmd()
            mock_patch.assert_called_once()

        text = cfg_file.read_text()
        assert 'worktree_base_ref = "origin/main"' in text
        out = capsys.readouterr().out
        assert "Written worktree_base_ref = origin/main" in out
        assert "Applied worktree_base_ref = origin/main to running server" in out
        assert "requires a server restart" not in out


class TestConfigCmdNoArgs:
    def test_prints_help_and_exits(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["nightdesk-config"])
        with pytest.raises(SystemExit) as exc_info:
            config_cmd()
        assert exc_info.value.code == 1
