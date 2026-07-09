"""nightdesk configuration loading.

Precedence (lowest → highest):
1. Built-in defaults (NightdeskConfig field defaults)
2. data_dir derivation: when data_dir is provided (file or env), db_path /
   transcript_root / log_dir are derived from it unless those keys are also
   explicitly set.
3. Config-file keys (NIGHTDESK_CONFIG or ~/.config/nightdesk/config.toml)
4. NIGHTDESK_* environment variables (each overrides its file counterpart;
   NIGHTDESK_DATA_DIR triggers the same derivation for keys not explicitly
   set by file or by their own env var)
5. CLI flags — work by exporting NIGHTDESK_* env vars before load_config()
   is called, so child processes (uvicorn reload workers, nightdesk-run-ticket
   subprocesses) inherit them automatically.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import os
import tomli

from nightdesk.domain.pricing import DEFAULT_PRICING_URL


DEFAULT_DATA_DIR = Path(os.path.expanduser("~/.local/share/nightdesk"))
DEFAULT_CONFIG_PATH = Path(os.path.expanduser("~/.config/nightdesk/config.toml"))
DEFAULT_SECRETS_PATH = Path(os.path.expanduser("~/.config/nightdesk/secrets.env"))


def default_config_path() -> Path:
    """Return the config file path, honoring NIGHTDESK_CONFIG env var."""
    return Path(os.environ.get("NIGHTDESK_CONFIG", str(DEFAULT_CONFIG_PATH)))


def default_secrets_path() -> Path:
    """Return the secrets file path, honoring NIGHTDESK_SECRETS env var."""
    return Path(os.environ.get("NIGHTDESK_SECRETS", str(DEFAULT_SECRETS_PATH)))


@dataclass
class NightdeskConfig:
    bearer_token: str = ""
    data_dir: Path = field(default_factory=lambda: DEFAULT_DATA_DIR)
    db_path: Path = field(default_factory=lambda: DEFAULT_DATA_DIR / "nightdesk.db")
    transcript_root: Path = field(default_factory=lambda: DEFAULT_DATA_DIR / "transcripts")
    log_dir: Path = field(default_factory=lambda: DEFAULT_DATA_DIR / "logs")
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
    # Live model-pricing endpoint (JSON). Override to point at any compatible
    # source; see domain/pricing.py for accepted shapes. Defaults to the
    # LiteLLM community price file (per-token USD, normalized to per-1M).
    pricing_url: str = DEFAULT_PRICING_URL
    # Vite build output (frontend/dist) to mount at /app. None means "let
    # api.app.create_app fall back to its own default (<repo>/frontend/dist
    # relative to the installed package)" — unlike the other paths above,
    # there is no sensible default under data_dir since this is a build
    # artifact tied to the source checkout, not user data.
    spa_dist: Optional[Path] = None
    secrets: dict[str, str] = field(default_factory=dict)

    @property
    def session_scratch_root(self) -> Path:
        """Root for per-agent scratch workspaces (resident interactive agents).

        A sibling of ``worktree_root`` — outside ``~/.local/share/nightdesk`` —
        so the deferred sandboxed posture can bind-mount it (the sandbox refuses
        anything under the data dir). Trusted-posture agents just cwd into it.
        """
        return self.worktree_root.parent / "nightdesk-sessions"


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
    config_path: Optional[Path] = None,
    secrets_path: Optional[Path] = None,
) -> NightdeskConfig:
    """Load nightdesk configuration respecting the full precedence chain."""
    if config_path is None:
        config_path = default_config_path()
    if secrets_path is None:
        secrets_path = default_secrets_path()

    cfg = NightdeskConfig()

    # Track which derived-path fields were explicitly set in the file so that
    # data_dir derivation doesn't clobber them.
    explicit_keys: set[str] = set()

    if config_path.exists():
        data = tomli.loads(config_path.read_text())

        # data_dir first — controls derivation of other path fields.
        if "data_dir" in data:
            cfg.data_dir = Path(os.path.expanduser(data["data_dir"]))
            explicit_keys.add("data_dir")

        for key in ("bearer_token", "bind_host", "bind_port", "worktree_base_ref",
                    "pricing_url"):
            if key in data:
                setattr(cfg, key, data[key])

        for key in ("db_path", "transcript_root", "log_dir", "worktree_root", "spa_dist"):
            if key in data:
                setattr(cfg, key, Path(os.path.expanduser(data[key])))
                explicit_keys.add(key)

        # Apply data_dir derivation for path fields not explicitly set in the file.
        if "data_dir" in explicit_keys:
            if "db_path" not in explicit_keys:
                cfg.db_path = cfg.data_dir / "nightdesk.db"
            if "transcript_root" not in explicit_keys:
                cfg.transcript_root = cfg.data_dir / "transcripts"
            if "log_dir" not in explicit_keys:
                cfg.log_dir = cfg.data_dir / "logs"

    # --- Environment variable overrides (highest precedence below CLI flags) ---

    env_data_dir = os.environ.get("NIGHTDESK_DATA_DIR")
    if env_data_dir:
        cfg.data_dir = Path(os.path.expanduser(env_data_dir))
        # Derive dependent paths for keys not set by their own env var or by file.
        if "NIGHTDESK_DB_PATH" not in os.environ and "db_path" not in explicit_keys:
            cfg.db_path = cfg.data_dir / "nightdesk.db"
        if "NIGHTDESK_TRANSCRIPT_ROOT" not in os.environ and "transcript_root" not in explicit_keys:
            cfg.transcript_root = cfg.data_dir / "transcripts"
        if "NIGHTDESK_LOG_DIR" not in os.environ and "log_dir" not in explicit_keys:
            cfg.log_dir = cfg.data_dir / "logs"

    if v := os.environ.get("NIGHTDESK_DB_PATH"):
        cfg.db_path = Path(os.path.expanduser(v))
    if v := os.environ.get("NIGHTDESK_TRANSCRIPT_ROOT"):
        cfg.transcript_root = Path(os.path.expanduser(v))
    if v := os.environ.get("NIGHTDESK_WORKTREE_ROOT"):
        cfg.worktree_root = Path(os.path.expanduser(v))
    if v := os.environ.get("NIGHTDESK_LOG_DIR"):
        cfg.log_dir = Path(os.path.expanduser(v))
    if v := os.environ.get("NIGHTDESK_BIND_HOST"):
        cfg.bind_host = v
    if v := os.environ.get("NIGHTDESK_BIND_PORT"):
        cfg.bind_port = int(v)
    if v := os.environ.get("NIGHTDESK_BEARER_TOKEN"):
        cfg.bearer_token = v
    if v := os.environ.get("NIGHTDESK_WORKTREE_BASE_REF"):
        cfg.worktree_base_ref = v
    if v := os.environ.get("NIGHTDESK_PRICING_URL"):
        cfg.pricing_url = v
    if v := os.environ.get("NIGHTDESK_SPA_DIST"):
        cfg.spa_dist = Path(os.path.expanduser(v))

    if secrets_path.exists():
        cfg.secrets = _parse_secrets(secrets_path.read_text())

    return cfg
