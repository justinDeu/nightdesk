from pathlib import Path

from nightdesk.config import NightdeskConfig, load_config


def test_load_config_uses_defaults_when_file_missing(tmp_path):
    cfg = load_config(config_path=tmp_path / "missing.toml", secrets_path=tmp_path / "secrets.env")
    assert isinstance(cfg, NightdeskConfig)
    assert cfg.bearer_token == ""
    assert cfg.db_path.name == "nightdesk.db"
    assert cfg.secrets == {}


def test_load_config_reads_toml_and_secrets(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        'bearer_token = "abc"\n'
        f'db_path = "{tmp_path / "x.db"}"\n'
        'bind_port = 9000\n'
    )
    secrets_file = tmp_path / "secrets.env"
    secrets_file.write_text(
        '# comment\n'
        'GITHUB_TOKEN=ghp_xxx\n'
        'OTHER="quoted value"\n'
    )
    cfg = load_config(config_path=cfg_file, secrets_path=secrets_file)
    assert cfg.bearer_token == "abc"
    assert cfg.db_path == tmp_path / "x.db"
    assert cfg.bind_port == 9000
    assert cfg.secrets == {"GITHUB_TOKEN": "ghp_xxx", "OTHER": "quoted value"}


def test_default_config_path_uses_env(monkeypatch):
    from nightdesk.config import default_config_path
    monkeypatch.setenv("NIGHTDESK_CONFIG", "/tmp/nd-test/config.toml")
    assert default_config_path() == Path("/tmp/nd-test/config.toml")


def test_default_config_path_fallback():
    """When env var is absent, falls back to the default constant."""
    import os
    from nightdesk.config import DEFAULT_CONFIG_PATH, default_config_path
    assert "NIGHTDESK_CONFIG" not in os.environ
    assert default_config_path() == DEFAULT_CONFIG_PATH


def test_data_dir_in_file_derives_paths(tmp_path, monkeypatch):
    """data_dir in config file derives db_path/transcript_root/log_dir."""
    monkeypatch.delenv("NIGHTDESK_DATA_DIR", raising=False)
    monkeypatch.delenv("NIGHTDESK_DB_PATH", raising=False)
    monkeypatch.delenv("NIGHTDESK_TRANSCRIPT_ROOT", raising=False)
    monkeypatch.delenv("NIGHTDESK_LOG_DIR", raising=False)
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(f'data_dir = "{tmp_path}"\n')
    cfg = load_config(config_path=cfg_file, secrets_path=tmp_path / "nope.env")
    assert cfg.data_dir == tmp_path
    assert cfg.db_path == tmp_path / "nightdesk.db"
    assert cfg.transcript_root == tmp_path / "transcripts"
    assert cfg.log_dir == tmp_path / "logs"


def test_explicit_db_path_beats_data_dir_derivation(tmp_path, monkeypatch):
    """Explicit db_path in config beats data_dir derivation."""
    monkeypatch.delenv("NIGHTDESK_DATA_DIR", raising=False)
    monkeypatch.delenv("NIGHTDESK_DB_PATH", raising=False)
    explicit_db = tmp_path / "custom.db"
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(f'data_dir = "{tmp_path}"\ndb_path = "{explicit_db}"\n')
    cfg = load_config(config_path=cfg_file, secrets_path=tmp_path / "nope.env")
    assert cfg.db_path == explicit_db
    assert cfg.transcript_root == tmp_path / "transcripts"


def test_env_overrides_beat_file_values(tmp_path, monkeypatch):
    """NIGHTDESK_BIND_HOST and NIGHTDESK_BIND_PORT env vars beat file values."""
    monkeypatch.setenv("NIGHTDESK_BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("NIGHTDESK_BIND_PORT", "9999")
    monkeypatch.delenv("NIGHTDESK_DATA_DIR", raising=False)
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('bind_host = "127.0.0.1"\nbind_port = 8765\n')
    cfg = load_config(config_path=cfg_file, secrets_path=tmp_path / "nope.env")
    assert cfg.bind_host == "0.0.0.0"
    assert cfg.bind_port == 9999
    assert isinstance(cfg.bind_port, int)


def test_nightdesk_data_dir_env_derives_paths(tmp_path, monkeypatch):
    """NIGHTDESK_DATA_DIR env var derives db_path/transcript_root/log_dir."""
    monkeypatch.setenv("NIGHTDESK_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("NIGHTDESK_DB_PATH", raising=False)
    monkeypatch.delenv("NIGHTDESK_TRANSCRIPT_ROOT", raising=False)
    monkeypatch.delenv("NIGHTDESK_LOG_DIR", raising=False)
    cfg = load_config(config_path=tmp_path / "nope.toml", secrets_path=tmp_path / "nope.env")
    assert cfg.data_dir == tmp_path
    assert cfg.db_path == tmp_path / "nightdesk.db"
    assert cfg.transcript_root == tmp_path / "transcripts"
    assert cfg.log_dir == tmp_path / "logs"


def test_nightdesk_config_env_points_at_alternate_file(tmp_path, monkeypatch):
    """NIGHTDESK_CONFIG env var makes load_config() read an alternate file."""
    alt_cfg = tmp_path / "alt.toml"
    alt_cfg.write_text('bind_port = 7777\n')
    monkeypatch.setenv("NIGHTDESK_CONFIG", str(alt_cfg))
    monkeypatch.delenv("NIGHTDESK_DATA_DIR", raising=False)
    monkeypatch.delenv("NIGHTDESK_BIND_PORT", raising=False)
    cfg = load_config()  # uses env
    assert cfg.bind_port == 7777
