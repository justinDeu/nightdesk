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
