"""Tests for the harness-aware ``nightdesk-install-skills`` command.

Covers harness detection, per-harness target resolution, per-harness version
marker isolation, the ``--list-harnesses`` / ``--harness`` / ``--all`` flags,
the bundled-source guard, and the Claude-Code-only backward-compat path.
"""

import sys

import pytest

from nightdesk.cli import (
    _HARNESSES,
    _claude_harness,
    _detect_harnesses,
    _harness_by_name,
    _hash_skills,
    _install_into_target,
    _install_one,
    _read_version_marker,
    _VERSION_MARKER,
    install_skills,
)


BUNDLED_SKILL_NAMES = {
    "nightdesk-api",
    "nightdesk-monitor-tickets",
    "nightdesk-ticket-ops",
}


def _isolate_home(monkeypatch, tmp_path):
    """Point HOME at a tmp dir, clear harness env overrides, neuter PATH checks.

    Makes detection purely a function of which config dirs we create, so tests
    are deterministic regardless of what's actually installed on the host.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("CLAUDE_CONFIG_DIR", "OPENCODE_CONFIG_DIR",
                "PI_CODING_AGENT_DIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(var, raising=False)
    # No agent binaries "on PATH" from the test's perspective.
    monkeypatch.setattr("nightdesk.cli._on_path", lambda binary: False)
    return tmp_path


def _run_cli(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["nightdesk-install-skills", *argv])


# -- registry shape --------------------------------------------------------


class TestRegistry:
    def test_three_supported_harnesses(self):
        assert [h.name for h in _HARNESSES] == ["claude", "opencode", "pi"]

    def test_claude_first(self):
        assert _HARNESSES[0].name == "claude"

    def test_lookup_by_name(self):
        assert _harness_by_name("opencode").name == "opencode"
        assert _harness_by_name("pi").name == "pi"
        assert _harness_by_name("nope") is None

    def test_claude_harness_present(self):
        assert _claude_harness().name == "claude"


# -- config-root / skills-dir resolution -----------------------------------


class TestTargetResolution:
    def test_claude_default(self, monkeypatch, tmp_path):
        _isolate_home(monkeypatch, tmp_path)
        assert _harness_by_name("claude").skills_dir() == tmp_path / ".claude" / "skills"

    def test_claude_config_dir_override(self, monkeypatch, tmp_path):
        _isolate_home(monkeypatch, tmp_path)
        other = tmp_path / "ccfg"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
        assert _harness_by_name("claude").skills_dir() == other / "skills"

    def test_opencode_default_is_xdg(self, monkeypatch, tmp_path):
        _isolate_home(monkeypatch, tmp_path)
        assert _harness_by_name("opencode").skills_dir() == (
            tmp_path / ".config" / "opencode" / "skills"
        )

    def test_opencode_xdg_config_home_override(self, monkeypatch, tmp_path):
        _isolate_home(monkeypatch, tmp_path)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert _harness_by_name("opencode").skills_dir() == (
            tmp_path / "xdg" / "opencode" / "skills"
        )

    def test_opencode_config_dir_override(self, monkeypatch, tmp_path):
        _isolate_home(monkeypatch, tmp_path)
        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "oc"))
        assert _harness_by_name("opencode").skills_dir() == tmp_path / "oc" / "skills"

    def test_pi_default(self, monkeypatch, tmp_path):
        _isolate_home(monkeypatch, tmp_path)
        assert _harness_by_name("pi").skills_dir() == (
            tmp_path / ".pi" / "agent" / "skills"
        )

    def test_pi_config_dir_override(self, monkeypatch, tmp_path):
        _isolate_home(monkeypatch, tmp_path)
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "picfg"))
        assert _harness_by_name("pi").skills_dir() == tmp_path / "picfg" / "skills"

    def test_pi_ignores_xdg(self, monkeypatch, tmp_path):
        """pi uses ~/.pi/agent and does NOT honor XDG_CONFIG_HOME."""
        _isolate_home(monkeypatch, tmp_path)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert _harness_by_name("pi").skills_dir() == tmp_path / ".pi" / "agent" / "skills"


# -- detection -------------------------------------------------------------


class TestDetection:
    def test_nothing_detected_on_clean_home(self, monkeypatch, tmp_path):
        _isolate_home(monkeypatch, tmp_path)
        assert _detect_harnesses() == []

    def test_claude_detected_when_dir_present(self, monkeypatch, tmp_path):
        _isolate_home(monkeypatch, tmp_path)
        (tmp_path / ".claude").mkdir()
        assert [h.name for h in _detect_harnesses()] == ["claude"]

    def test_env_override_counts_as_installed(self, monkeypatch, tmp_path):
        _isolate_home(monkeypatch, tmp_path)
        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "oc"))
        assert {h.name for h in _detect_harnesses()} == {"opencode"}

    def test_multiple_detected(self, monkeypatch, tmp_path):
        _isolate_home(monkeypatch, tmp_path)
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".config" / "opencode").mkdir(parents=True)
        (tmp_path / ".pi" / "agent").mkdir(parents=True)
        assert [h.name for h in _detect_harnesses()] == ["claude", "opencode", "pi"]

    def test_path_binary_counts_as_installed(self, monkeypatch, tmp_path):
        _isolate_home(monkeypatch, tmp_path)
        monkeypatch.setattr("nightdesk.cli._on_path", lambda b: b == "pi")
        assert [h.name for h in _detect_harnesses()] == ["pi"]


# -- core install helper ---------------------------------------------------


class TestInstallIntoTarget:
    def test_copies_all_skills_and_writes_marker(self, monkeypatch, tmp_path):
        from nightdesk.cli import _find_bundled_skills_dir
        bundled = _find_bundled_skills_dir()
        skills_hash = _hash_skills(bundled)
        target = tmp_path / "skills"
        result = _install_into_target(target, bundled, skills_hash, "9.9.9", False)

        assert set(result["installed"]) == BUNDLED_SKILL_NAMES
        assert result["updated"] == []
        for name in BUNDLED_SKILL_NAMES:
            assert (target / name / "SKILL.md").is_file()
        marker = _read_version_marker(target)
        assert marker["nightdesk_version"] == "9.9.9"
        assert marker["skills_hash"] == skills_hash

    def test_idempotent_when_up_to_date(self, monkeypatch, tmp_path, capsys):
        from nightdesk.cli import _find_bundled_skills_dir
        bundled = _find_bundled_skills_dir()
        skills_hash = _hash_skills(bundled)
        target = tmp_path / "skills"
        _install_into_target(target, bundled, skills_hash, "1.0.0", False)
        capsys.readouterr()  # clear first run's output

        again = _install_into_target(target, bundled, skills_hash, "1.0.0", False)
        assert again is None
        assert "up to date" in capsys.readouterr().out

    def test_refuses_bundled_source(self, monkeypatch, tmp_path):
        from nightdesk.cli import _find_bundled_skills_dir
        bundled = _find_bundled_skills_dir()
        skills_hash = _hash_skills(bundled)
        with pytest.raises(SystemExit):
            _install_into_target(bundled, bundled, skills_hash, "1.0.0", False)

    def test_force_reinstalls(self, monkeypatch, tmp_path):
        from nightdesk.cli import _find_bundled_skills_dir
        bundled = _find_bundled_skills_dir()
        skills_hash = _hash_skills(bundled)
        target = tmp_path / "skills"
        _install_into_target(target, bundled, skills_hash, "1.0.0", False)
        result = _install_into_target(target, bundled, skills_hash, "1.0.0", True)
        assert set(result["updated"]) == BUNDLED_SKILL_NAMES
        assert result["force"] is True


# -- per-harness marker isolation ------------------------------------------


class TestMarkerIsolation:
    def test_each_harness_has_independent_marker(self, monkeypatch, tmp_path):
        from nightdesk.cli import _find_bundled_skills_dir
        _isolate_home(monkeypatch, tmp_path)
        bundled = _find_bundled_skills_dir()
        skills_hash = _hash_skills(bundled)

        claude_dir = tmp_path / ".claude" / "skills"
        pi_dir = tmp_path / ".pi" / "agent" / "skills"
        _install_into_target(claude_dir, bundled, skills_hash, "1.0.0", False)
        _install_into_target(pi_dir, bundled, skills_hash, "1.0.0", False)

        # Both markers exist, independently, in their own dirs.
        assert (claude_dir / _VERSION_MARKER).is_file()
        assert (pi_dir / _VERSION_MARKER).is_file()

        # Corrupt only the pi marker -> claude stays up to date, pi drifts.
        (pi_dir / _VERSION_MARKER).write_text('{"skills_hash": "stale", "nightdesk_version": "0.0.1"}')

        claude_result = _install_into_target(claude_dir, bundled, skills_hash, "1.0.0", False)
        pi_result = _install_into_target(pi_dir, bundled, skills_hash, "1.0.0", False)
        assert claude_result is None  # untouched marker -> up to date
        assert pi_result is not None  # corrupted marker -> reinstalled


# -- CLI flags -------------------------------------------------------------


class TestListHarnesses:
    def test_prints_all_with_detection_marks(self, monkeypatch, tmp_path, capsys):
        _isolate_home(monkeypatch, tmp_path)
        (tmp_path / ".claude").mkdir()  # only claude detected
        _run_cli(monkeypatch, "--list-harnesses")
        install_skills()
        out = capsys.readouterr().out
        assert "claude" in out and "opencode" in out and "pi" in out
        assert "* claude" in out          # detected
        assert "  opencode" in out        # not detected (space prefix, no star)
        assert "  pi" in out


class TestAllFlag:
    def test_installs_into_every_detected_no_prompt(self, monkeypatch, tmp_path, capsys):
        _isolate_home(monkeypatch, tmp_path)
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".config" / "opencode").mkdir(parents=True)
        # A stray input() must never be called -- prove it by failing if it is.
        monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("prompted")))
        _run_cli(monkeypatch, "--all")
        install_skills()
        out = capsys.readouterr().out
        assert "Claude Code" in out
        assert "opencode" in out

        for sub in (tmp_path / ".claude" / "skills", tmp_path / ".config" / "opencode" / "skills"):
            for name in BUNDLED_SKILL_NAMES:
                assert (sub / name / "SKILL.md").is_file()
            assert (sub / _VERSION_MARKER).is_file()

    def test_all_with_none_detected_advises(self, monkeypatch, tmp_path, capsys):
        _isolate_home(monkeypatch, tmp_path)
        _run_cli(monkeypatch, "--all")
        install_skills()
        out = capsys.readouterr().out
        assert "No supported harnesses detected" in out
        assert "--harness" in out


class TestHarnessFlag:
    def test_installs_one_named_harness(self, monkeypatch, tmp_path, capsys):
        _isolate_home(monkeypatch, tmp_path)
        _run_cli(monkeypatch, "--harness", "pi")
        install_skills()
        pi_skills = tmp_path / ".pi" / "agent" / "skills"
        for name in BUNDLED_SKILL_NAMES:
            assert (pi_skills / name / "SKILL.md").is_file()
        # Nothing installed into the other harnesses.
        assert not (tmp_path / ".claude" / "skills").exists()
        assert not (tmp_path / ".config" / "opencode" / "skills").exists()

    def test_unknown_harness_exits(self, monkeypatch, tmp_path):
        _isolate_home(monkeypatch, tmp_path)
        _run_cli(monkeypatch, "--harness", "bogus")
        with pytest.raises(SystemExit):
            install_skills()

    def test_target_and_harness_are_mutually_exclusive(self, monkeypatch, tmp_path):
        _isolate_home(monkeypatch, tmp_path)
        _run_cli(monkeypatch, "--target", str(tmp_path), "--harness", "pi")
        with pytest.raises(SystemExit):
            install_skills()


class TestTargetFlag:
    def test_project_local_install(self, monkeypatch, tmp_path, capsys):
        _isolate_home(monkeypatch, tmp_path)
        project = tmp_path / "proj"
        project.mkdir()
        _run_cli(monkeypatch, "--target", str(project))
        install_skills()
        target = project / ".claude" / "skills"
        for name in BUNDLED_SKILL_NAMES:
            assert (target / name / "SKILL.md").is_file()
        assert (target / _VERSION_MARKER).is_file()


# -- backward-compat & multi-harness prompt --------------------------------


class TestBackwardCompat:
    def test_claude_only_no_flag_is_straight_install(self, monkeypatch, tmp_path, capsys):
        """Only Claude Code detected, no flags -> install with no prompt."""
        _isolate_home(monkeypatch, tmp_path)
        (tmp_path / ".claude").mkdir()
        monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("prompted")))
        _run_cli(monkeypatch)  # no flags
        install_skills()
        out = capsys.readouterr().out
        assert "Multiple coding-agent" not in out
        target = tmp_path / ".claude" / "skills"
        for name in BUNDLED_SKILL_NAMES:
            assert (target / name / "SKILL.md").is_file()
        assert (target / _VERSION_MARKER).is_file()

    def test_nothing_detected_still_installs_into_claude_default(self, monkeypatch, tmp_path):
        """A clean machine still lands skills in ~/.claude/skills as before."""
        _isolate_home(monkeypatch, tmp_path)
        _run_cli(monkeypatch)  # no flags, nothing detected
        install_skills()
        target = tmp_path / ".claude" / "skills"
        for name in BUNDLED_SKILL_NAMES:
            assert (target / name / "SKILL.md").is_file()


class TestMultiHarnessPrompt:
    def test_prompts_per_harness_and_installs_accepted(self, monkeypatch, tmp_path, capsys):
        _isolate_home(monkeypatch, tmp_path)
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".config" / "opencode").mkdir(parents=True)

        answers = iter(["y", "n"])  # yes to claude, no to opencode
        monkeypatch.setattr("builtins.input", lambda *a: next(answers))
        _run_cli(monkeypatch)  # no flags -> prompt flow
        install_skills()
        out = capsys.readouterr().out
        assert "Multiple coding-agent harnesses detected" in out

        # claude accepted, opencode declined.
        assert (tmp_path / ".claude" / "skills" / "nightdesk-api" / "SKILL.md").is_file()
        assert not (tmp_path / ".config" / "opencode" / "skills").exists()

    def test_decline_all_installs_nothing(self, monkeypatch, tmp_path, capsys):
        _isolate_home(monkeypatch, tmp_path)
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".pi" / "agent").mkdir(parents=True)
        monkeypatch.setattr("builtins.input", lambda *a: "n")
        _run_cli(monkeypatch)
        install_skills()
        out = capsys.readouterr().out
        assert "Nothing selected" in out
        assert not (tmp_path / ".claude" / "skills").exists()


# -- internal skills are never shipped --------------------------------------


def _make_skill(parent: Path, name: str, *, internal: bool | None = None) -> Path:
    """Create a fake bundled skill dir with a SKILL.md frontmatter block."""
    d = parent / name
    d.mkdir(parents=True)
    fm = ["---", f"name: {name}", "description: x"]
    if internal is not None:
        fm.append(f"internal: {'true' if internal else 'false'}")
    fm.append("---")
    (d / "SKILL.md").write_text("\n".join(fm) + "\n# skill\n")
    return d


class TestInternalSkills:
    def test_detects_internal_flag(self, tmp_path):
        from nightdesk.cli import _is_internal_skill
        assert _is_internal_skill(_make_skill(tmp_path, "a", internal=True)) is True

    def test_false_flag_is_shippable(self, tmp_path):
        from nightdesk.cli import _is_internal_skill
        assert _is_internal_skill(_make_skill(tmp_path, "a", internal=False)) is False

    def test_missing_flag_is_shippable(self, tmp_path):
        from nightdesk.cli import _is_internal_skill
        assert _is_internal_skill(_make_skill(tmp_path, "a")) is False

    def test_no_frontmatter_is_shippable(self, tmp_path):
        from nightdesk.cli import _is_internal_skill
        d = tmp_path / "a"
        d.mkdir()
        (d / "SKILL.md").write_text("# no frontmatter at all\n")
        assert _is_internal_skill(d) is False

    def test_unclosed_frontmatter_is_shippable(self, tmp_path):
        from nightdesk.cli import _is_internal_skill
        d = tmp_path / "a"
        d.mkdir()
        (d / "SKILL.md").write_text("---\ninternal: true\n# never closed\n")
        assert _is_internal_skill(d) is False

    def test_no_skill_md_is_shippable(self, tmp_path):
        from nightdesk.cli import _is_internal_skill
        d = tmp_path / "a"
        d.mkdir()
        (d / "README.md").write_text("not a skill\n")
        assert _is_internal_skill(d) is False

    def test_install_excludes_internal(self, tmp_path):
        from nightdesk.cli import _install_into_target, _hash_skills
        bundled = tmp_path / "bundled"
        bundled.mkdir()
        _make_skill(bundled, "ship-me")
        _make_skill(bundled, "dev-only", internal=True)
        skills_hash = _hash_skills(bundled)
        target = tmp_path / "skills"
        result = _install_into_target(target, bundled, skills_hash, "1.0.0", False)

        assert set(result["installed"]) == {"ship-me"}
        assert (target / "ship-me" / "SKILL.md").is_file()
        assert not (target / "dev-only").exists()

    def test_hash_ignores_internal_skill_changes(self, tmp_path):
        from nightdesk.cli import _hash_skills
        bundled = tmp_path / "bundled"
        bundled.mkdir()
        _make_skill(bundled, "ship-me")
        _make_skill(bundled, "dev-only", internal=True)
        before = _hash_skills(bundled)
        # Edit the internal skill — shipped hash must not move.
        (bundled / "dev-only" / "SKILL.md").write_text(
            "---\nname: dev-only\ninternal: true\n---\n# totally different\n"
        )
        assert _hash_skills(bundled) == before
        # Editing a shippable skill DOES move the hash.
        (bundled / "ship-me" / "SKILL.md").write_text("# changed\n")
        assert _hash_skills(bundled) != before
