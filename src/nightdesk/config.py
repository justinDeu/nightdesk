from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import os
import tomli


DEFAULT_DATA_DIR = Path(os.path.expanduser("~/.local/share/nightdesk"))
DEFAULT_CONFIG_PATH = Path(os.path.expanduser("~/.config/nightdesk/config.toml"))
DEFAULT_SECRETS_PATH = Path(os.path.expanduser("~/.config/nightdesk/secrets.env"))


@dataclass
class NightdeskConfig:
    bearer_token: str = ""
    db_path: Path = field(default_factory=lambda: DEFAULT_DATA_DIR / "nightdesk.db")
    transcript_root: Path = field(default_factory=lambda: DEFAULT_DATA_DIR / "transcripts")
    # Worktrees live OUTSIDE the data dir on purpose: the sandbox refuses to
    # bind-mount anything under ~/.local/share/nightdesk (db, creds, transcripts),
    # so git_worktree-mode tickets must resolve to a sibling the sandbox can mount.
    worktree_root: Path = field(
        default_factory=lambda: Path(os.path.expanduser("~/.local/share/nightdesk-worktrees"))
    )
    bind_host: str = "127.0.0.1"
    bind_port: int = 8765
    # Optional global default base ref for git_worktree tickets. Seeds the
    # mutable ConfigRow.worktree_base_ref at setup; new tickets without an
    # explicit base_ref branch from this ref instead of HEAD.
    worktree_base_ref: Optional[str] = None
    secrets: dict[str, str] = field(default_factory=dict)


def _parse_secrets(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def load_config(
    config_path: Path = DEFAULT_CONFIG_PATH,
    secrets_path: Path = DEFAULT_SECRETS_PATH,
) -> NightdeskConfig:
    cfg = NightdeskConfig()
    if config_path.exists():
        data = tomli.loads(config_path.read_text())
        for key in ("bearer_token", "bind_host", "bind_port", "worktree_base_ref"):
            if key in data:
                setattr(cfg, key, data[key])
        for key in ("db_path", "transcript_root", "worktree_root"):
            if key in data:
                setattr(cfg, key, Path(os.path.expanduser(data[key])))
    if secrets_path.exists():
        cfg.secrets = _parse_secrets(secrets_path.read_text())
    return cfg
