"""Tests for nightdesk-stop and nightdesk-uninstall commands.

Covers:
- stop: services stopped+disabled, files preserved
- uninstall: units+config removed, data preserved by default
- uninstall --purge-data: data dir removed
- uninstall --purge-worktrees: worktree dirs removed
- uninstall --dry-run: no filesystem/systemctl side effects
- idempotency: running twice without error when already removed
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import nightdesk.cli as cli_mod

# Save the real expanduser before any patching occurs.
_REAL_EXPANDUSER = os.path.expanduser
_REAL_HOME = _REAL_EXPANDUSER("~")


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_home(tmp_path):
    """Create a fake home directory structure under tmp_path."""
    cfg_dir = tmp_path / ".config" / "nightdesk"
    data_dir = tmp_path / ".local" / "share" / "nightdesk"
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    worktree_dir = tmp_path / ".local" / "share" / "nightdesk-worktrees"
    cc_sessions_dir = tmp_path / ".local" / "share" / "nightdesk-cc-sessions"

    for d in (cfg_dir, data_dir / "transcripts", data_dir / "logs" / "runs",
              unit_dir, worktree_dir, cc_sessions_dir):
        d.mkdir(parents=True, exist_ok=True)

    (cfg_dir / "config.toml").write_text('bearer_token = "test"\n')
    (unit_dir / "nightdesk-api.service").write_text(cli_mod._render_api_unit())
    (unit_dir / "nightdesk-worker.service").write_text(cli_mod._render_worker_unit())
    (data_dir / "nightdesk.db").write_text("fake-db")

    return {
        "home": tmp_path,
        "cfg_dir": cfg_dir,
        "data_dir": data_dir,
        "unit_dir": unit_dir,
        "worktree_dir": worktree_dir,
        "cc_sessions_dir": cc_sessions_dir,
    }


def _make_expanduser_redirect(tmp_path):
    """Return a side_effect that redirects ~ paths to tmp_path."""
    redirects = {
        "/.config/nightdesk": tmp_path / ".config" / "nightdesk",
        "/.config/nightdesk/config.toml": tmp_path / ".config" / "nightdesk" / "config.toml",
        "/.config/nightdesk/secrets.env": tmp_path / ".config" / "nightdesk" / "secrets.env",
        "/.local/share/nightdesk": tmp_path / ".local" / "share" / "nightdesk",
        "/.local/share/nightdesk-worktrees": tmp_path / ".local" / "share" / "nightdesk-worktrees",
        "/.local/share/nightdesk-cc-sessions": tmp_path / ".local" / "share" / "nightdesk-cc-sessions",
        "/.config/systemd/user": tmp_path / ".config" / "systemd" / "user",
    }

    def _expand(path):
        resolved = _REAL_EXPANDUSER(str(path))
        for suffix, target in redirects.items():
            if resolved == _REAL_HOME + suffix:
                return str(target)
        return resolved

    return _expand


# ---------------------------------------------------------------------------
# Stop command tests.
# ---------------------------------------------------------------------------


class TestStop:
    def test_stop_calls_systemctl(self, fake_home, capsys):
        """stop() should call systemctl stop, disable, daemon-reload."""
        calls_log = []

        def mock_run(cmd, **kwargs):
            calls_log.append(cmd)
            return MagicMock(stdout="", stderr="", returncode=0)

        with (
            patch("nightdesk.cli.subprocess.run", side_effect=mock_run),
            patch("sys.argv", ["nightdesk-stop"]),
        ):
            cli_mod.stop()

        assert any("stop" in c for c in calls_log), f"Expected stop in calls: {calls_log}"
        assert any("disable" in c for c in calls_log), f"Expected disable in calls: {calls_log}"
        assert any("daemon-reload" in c for c in calls_log), f"Expected daemon-reload in calls: {calls_log}"

        captured = capsys.readouterr()
        assert "stopped and disabled" in captured.out.lower()

    def test_stop_dry_run(self, capsys):
        """stop --dry-run should print actions but not call systemctl."""
        with (
            patch("nightdesk.cli.subprocess.run") as mock_run,
            patch("sys.argv", ["nightdesk-stop", "--dry-run"]),
        ):
            cli_mod.stop()
            mock_run.assert_not_called()

        captured = capsys.readouterr()
        assert "[dry-run]" in captured.out

    def test_stop_preserves_files(self, fake_home):
        """stop() should not remove any files."""
        config_path = fake_home["cfg_dir"] / "config.toml"
        unit_api = fake_home["unit_dir"] / "nightdesk-api.service"
        db_path = fake_home["data_dir"] / "nightdesk.db"

        with (
            patch("nightdesk.cli.subprocess.run", return_value=MagicMock(stdout="", returncode=0)),
            patch("sys.argv", ["nightdesk-stop"]),
        ):
            cli_mod.stop()

        assert config_path.exists(), "stop should preserve config"
        assert unit_api.exists(), "stop should preserve unit files"
        assert db_path.exists(), "stop should preserve database"

    def test_stop_idempotent_no_units(self, capsys):
        """stop() should not error when units don't exist."""
        with (
            patch("nightdesk.cli.subprocess.run", return_value=MagicMock(stdout="", stderr="", returncode=0)),
            patch("sys.argv", ["nightdesk-stop"]),
        ):
            cli_mod.stop()

        captured = capsys.readouterr()
        assert "stopped and disabled" in captured.out.lower()


# ---------------------------------------------------------------------------
# Uninstall command tests.
# ---------------------------------------------------------------------------


class TestUninstall:
    def _run_uninstall(self, fake_home, extra_args=None):
        """Helper to run uninstall with redirected paths."""
        home = fake_home["home"]
        argv = ["nightdesk-uninstall"] + (extra_args or [])

        with (
            patch("nightdesk.cli.subprocess.run", return_value=MagicMock(stdout="", stderr="", returncode=0)),
            patch("nightdesk.cli.os.path.expanduser", side_effect=_make_expanduser_redirect(home)),
            patch("sys.argv", argv),
            patch.object(cli_mod, "DEFAULT_CONFIG_PATH", home / ".config" / "nightdesk" / "config.toml"),
        ):
            cli_mod.uninstall()

    def test_uninstall_removes_units_and_config(self, fake_home):
        """uninstall() should remove unit files and config but preserve data."""
        self._run_uninstall(fake_home)

        assert not (fake_home["unit_dir"] / "nightdesk-api.service").exists()
        assert not (fake_home["unit_dir"] / "nightdesk-worker.service").exists()
        assert not (fake_home["cfg_dir"] / "config.toml").exists()
        assert (fake_home["data_dir"] / "nightdesk.db").exists()
        assert fake_home["data_dir"].exists()

    def test_uninstall_preserves_data_by_default(self, fake_home, capsys):
        """Without --purge-data, data directory should remain."""
        self._run_uninstall(fake_home)

        assert fake_home["data_dir"].exists()
        assert (fake_home["data_dir"] / "nightdesk.db").exists()
        assert (fake_home["data_dir"] / "transcripts").exists()

        captured = capsys.readouterr()
        assert "preserved" in captured.out.lower()

    def test_uninstall_purge_data(self, fake_home, capsys):
        """--purge-data should remove the data directory."""
        self._run_uninstall(fake_home, ["--purge-data"])

        assert not fake_home["data_dir"].exists()

        captured = capsys.readouterr()
        assert "removed data" in captured.out.lower()

    def test_uninstall_purge_worktrees(self, fake_home, capsys):
        """--purge-worktrees should remove worktree and cc-sessions dirs."""
        (fake_home["worktree_dir"] / "some-repo").mkdir()
        (fake_home["cc_sessions_dir"] / "session-1").mkdir()

        self._run_uninstall(fake_home, ["--purge-worktrees"])

        assert not fake_home["worktree_dir"].exists()
        assert not fake_home["cc_sessions_dir"].exists()

        captured = capsys.readouterr()
        assert "removed" in captured.out.lower()

    def test_uninstall_purge_all(self, fake_home):
        """--purge-data --purge-worktrees should remove everything."""
        (fake_home["worktree_dir"] / "repo").mkdir()
        (fake_home["cc_sessions_dir"] / "s").mkdir()

        self._run_uninstall(fake_home, ["--purge-data", "--purge-worktrees"])

        assert not fake_home["data_dir"].exists()
        assert not fake_home["worktree_dir"].exists()
        assert not fake_home["cc_sessions_dir"].exists()
        assert not (fake_home["unit_dir"] / "nightdesk-api.service").exists()
        assert not (fake_home["cfg_dir"] / "config.toml").exists()

    def test_uninstall_dry_run_no_side_effects(self, fake_home, capsys):
        """--dry-run should not remove any files or call systemctl."""
        home = fake_home["home"]

        with (
            patch("nightdesk.cli.subprocess.run") as mock_run,
            patch("nightdesk.cli.os.path.expanduser", side_effect=_make_expanduser_redirect(home)),
            patch("sys.argv", ["nightdesk-uninstall", "--dry-run"]),
            patch.object(cli_mod, "DEFAULT_CONFIG_PATH", home / ".config" / "nightdesk" / "config.toml"),
        ):
            cli_mod.uninstall()

        mock_run.assert_not_called()

        assert (fake_home["unit_dir"] / "nightdesk-api.service").exists()
        assert (fake_home["cfg_dir"] / "config.toml").exists()
        assert (fake_home["data_dir"] / "nightdesk.db").exists()

        captured = capsys.readouterr()
        assert "[dry-run]" in captured.out
        assert "no changes made" in captured.out.lower()

    def test_uninstall_dry_run_with_purge_flags(self, fake_home, capsys):
        """--dry-run --purge-data --purge-worktrees should print but not remove."""
        home = fake_home["home"]

        with (
            patch("nightdesk.cli.subprocess.run") as mock_run,
            patch("nightdesk.cli.os.path.expanduser", side_effect=_make_expanduser_redirect(home)),
            patch("sys.argv", ["nightdesk-uninstall", "--dry-run", "--purge-data", "--purge-worktrees"]),
            patch.object(cli_mod, "DEFAULT_CONFIG_PATH", home / ".config" / "nightdesk" / "config.toml"),
        ):
            cli_mod.uninstall()

        mock_run.assert_not_called()
        assert fake_home["data_dir"].exists()
        assert fake_home["worktree_dir"].exists()

        captured = capsys.readouterr()
        assert "would remove data" in captured.out.lower()

    def test_uninstall_idempotent(self, fake_home, capsys):
        """Running uninstall twice should not error."""
        self._run_uninstall(fake_home)
        self._run_uninstall(fake_home)

        captured = capsys.readouterr()
        assert "not found" in captured.out.lower()

    def test_uninstall_stops_running_services_first(self, fake_home, capsys):
        """If services are active, uninstall should stop them first."""
        home = fake_home["home"]
        calls_log = []

        def mock_run(cmd, **kwargs):
            calls_log.append(cmd)
            r = MagicMock()
            cmd_str = " ".join(cmd)
            if "is-active" in cmd_str:
                if "nightdesk-api.service" in cmd_str:
                    r.stdout = "active"
                else:
                    r.stdout = "inactive"
            else:
                r.stdout = ""
            r.stderr = ""
            r.returncode = 0
            return r

        with (
            patch("nightdesk.cli.subprocess.run", side_effect=mock_run),
            patch("nightdesk.cli.os.path.expanduser", side_effect=_make_expanduser_redirect(home)),
            patch("sys.argv", ["nightdesk-uninstall"]),
            patch.object(cli_mod, "DEFAULT_CONFIG_PATH", home / ".config" / "nightdesk" / "config.toml"),
        ):
            cli_mod.uninstall()

        assert any("is-active" in c for c in calls_log)
        assert any("stop" in c for c in calls_log)
        captured = capsys.readouterr()
        assert "stopping them first" in captured.out.lower()

    def test_uninstall_removes_secrets_env_if_present(self, fake_home):
        """uninstall should remove secrets.env if it exists."""
        secrets_path = fake_home["cfg_dir"] / "secrets.env"
        secrets_path.write_text("API_KEY=test\n")

        self._run_uninstall(fake_home)

        assert not secrets_path.exists()

    def test_uninstall_removes_config_dir_if_empty(self, fake_home):
        """After removing all files, uninstall should remove the config dir."""
        self._run_uninstall(fake_home)

        assert not fake_home["cfg_dir"].exists()

    def test_uninstall_preserves_config_dir_if_not_empty(self, fake_home):
        """If other files remain in config dir, it should be preserved."""
        (fake_home["cfg_dir"] / "other-file.txt").write_text("keep me")

        self._run_uninstall(fake_home)

        assert fake_home["cfg_dir"].exists()
        assert (fake_home["cfg_dir"] / "other-file.txt").exists()

    def test_uninstall_prints_summary(self, fake_home, capsys):
        """uninstall should print a clear summary of what was done."""
        (fake_home["worktree_dir"] / "repo").mkdir()
        (fake_home["cc_sessions_dir"] / "s").mkdir()

        self._run_uninstall(fake_home, ["--purge-data", "--purge-worktrees"])

        captured = capsys.readouterr()
        assert "uninstall complete" in captured.out.lower()
        assert "removed: systemd units, config files" in captured.out.lower()
        assert "removed: data directory" in captured.out.lower()
        assert "removed: worktree directories" in captured.out.lower()
