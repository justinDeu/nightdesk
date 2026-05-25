#!/usr/bin/env python
"""One-off backfill: recover Run.model_used from transcripts.

Older runs left ``model_used`` NULL because the worker only read the model off
the final SDK result event (which doesn't carry it). The model does appear in
each transcript's per-turn ``stats`` events, so this script scans the
transcript of every NULL-model run, picks the most common model seen, writes it
back, and recomputes ``cost_usd`` when it was NULL and tokens are present.

Idempotent: only touches rows where ``model_used IS NULL``. Safe to re-run.

    python scripts/backfill_run_model.py            # apply
    python scripts/backfill_run_model.py --dry-run   # report only
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from typing import Optional

from nightdesk.cli import _init
from nightdesk.db.models import Run
from nightdesk.db.session import make_engine, session_factory
from nightdesk.domain.cost import compute_cost


def model_from_transcript(path: str) -> Optional[str]:
    """Most frequent non-empty model across the transcript's stats events."""
    if not path or not os.path.exists(path):
        return None
    counts: Counter[str] = Counter()
    with open(path, errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(evt, dict) and evt.get("type") == "stats":
                model = evt.get("model")
                if model:
                    counts[str(model)] += 1
    if not counts:
        return None
    return counts.most_common(1)[0][0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    args = ap.parse_args()

    cfg = _init()
    Session = session_factory(make_engine(cfg.db_path))

    filled = 0
    recosted = 0
    skipped = 0
    with Session() as session:
        runs = session.query(Run).filter(Run.model_used.is_(None)).all()
        print(f"{len(runs)} runs with NULL model_used")
        for run in runs:
            model = model_from_transcript(run.transcript_path)
            if not model:
                skipped += 1
                continue
            print(f"  {run.id}  ->  {model}", end="")
            if not args.dry_run:
                run.model_used = model
            filled += 1
            # Recompute cost only when it was never computed (model was unknown).
            if run.cost_usd is None:
                cost = compute_cost(
                    model=model,
                    input_tokens=run.input_tokens or 0,
                    output_tokens=run.output_tokens or 0,
                    cache_read_tokens=run.cache_read_tokens or 0,
                    cache_write_tokens=run.cache_write_tokens or 0,
                )
                if cost is not None:
                    if not args.dry_run:
                        run.cost_usd = cost
                    recosted += 1
                    print(f"  (cost ${cost:.4f})", end="")
            print()
        if not args.dry_run:
            session.commit()

    verb = "would fill" if args.dry_run else "filled"
    print(f"\n{verb} {filled} model_used ({recosted} also re-costed); "
          f"{skipped} unrecoverable (no model in transcript)")


if __name__ == "__main__":
    main()
