#!/usr/bin/env python
"""One-off backfill: populate ``run_latency`` rows for historical runs.

Latency caching landed with the run-completion hook (``populate_run_latency``
in ``domain.latency``), so runs finished before that ship left no cached
summary and the analytics latency charts only see recent data. Latency is
derived purely from transcript ``ts`` deltas, so every historical run whose
transcript file still exists can be summarized after the fact.

Idempotent: ``populate_run_latency`` leaves existing rows untouched, and this
script only visits finished runs with no ``run_latency`` row. Safe to re-run.

    python scripts/backfill_run_latency.py            # apply
    python scripts/backfill_run_latency.py --dry-run   # report only
"""
from __future__ import annotations

import argparse
import os

from sqlalchemy import select

from nightdesk.cli import _init
from nightdesk.db.models import Run, RunLatency
from nightdesk.db.session import make_engine, session_factory
from nightdesk.domain.latency import populate_run_latency


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    args = ap.parse_args()

    cfg = _init()
    Session = session_factory(make_engine(cfg.db_path))

    filled = 0
    empty = 0
    missing = 0
    with Session() as session:
        cached = select(RunLatency.run_id)
        runs = list(session.scalars(
            select(Run)
            .where(Run.finished_at.is_not(None), Run.id.not_in(cached))
            .order_by(Run.started_at)
        ))
        print(f"{len(runs)} finished run(s) without a latency row")
        for run in runs:
            if not run.transcript_path or not os.path.exists(run.transcript_path):
                missing += 1
                continue
            if args.dry_run:
                filled += 1
                continue
            try:
                row = populate_run_latency(session, run)
            except Exception as exc:  # best-effort, like the run-end hook
                session.rollback()
                print(f"  {run.id}  FAILED: {exc}")
                continue
            if row is not None and row.turn_count == 0 and row.ttft_seconds is None:
                empty += 1
            filled += 1

    verb = "would fill" if args.dry_run else "filled"
    print(
        f"{verb} {filled} latency row(s); {missing} skipped (transcript gone); "
        f"{empty} summarized with no samples"
    )


if __name__ == "__main__":
    main()
