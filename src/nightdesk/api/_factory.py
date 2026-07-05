from __future__ import annotations

from pathlib import Path

from nightdesk.api.app import create_app
from nightdesk.config import load_config
from nightdesk.db.session import make_engine


def make():
    cfg = load_config()
    engine = make_engine(cfg.db_path)
    return create_app(
        engine=engine,
        bearer_token=cfg.bearer_token,
        static_root=Path(__file__).parent.parent / "static",
        transcript_root=cfg.transcript_root,
        worktree_root=cfg.worktree_root,
        data_dir=cfg.data_dir,
        pricing_url=cfg.pricing_url,
        spa_dist_root=cfg.spa_dist,
    )
