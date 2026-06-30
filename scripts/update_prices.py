#!/usr/bin/env python
"""Refresh nightdesk's model-price cache (run at release time).

Analytics cost estimates resolve prices live → on-disk cache → bundled table
(see ``src/nightdesk/domain/pricing.py``). The cache lives at
``<data_dir>/model_prices.json`` and carries a ``fetched_at`` timestamp with a
24h TTL, so a running server reuses it without re-fetching. This script is what
keeps the *shipped* cache from drifting months behind real pricing: run it
before cutting a release so the cache file committed/installed alongside the app
is recent, and the bundled fallback table stays current.

It fetches the configured pricing endpoint (default: models.dev; override with
``pricing_url`` in config.toml, the ``NIGHTDESK_PRICING_URL`` env var, or
``--url``) and writes the cache. With ``--regenerate`` it additionally rewrites
the in-repo ``_PRICE_TABLE`` / ``PRICES_AS_OF`` in ``domain/cost.py`` from the
fetched prices so the last-resort fallback is also up to date — commit that
change.

The endpoint may return any shape ``pricing.normalize_endpoint_json`` accepts:
our canonical ``{"prices": {prefix: {...}}}``, a registry aggregate keyed by
``provider/model`` with a ``cost`` block (e.g. models.dev), or a flat
``{prefix: {...}}`` map. Prices are USD per 1M tokens.

Usage::

    python scripts/update_prices.py              # fetch -> write cache
    python scripts/update_prices.py --regenerate # ...and rewrite cost.py table
    python scripts/update_prices.py --url URL    # override the endpoint once
    python scripts/update_prices.py --dry-run    # report only, write nothing

Note: the default public endpoint may not yet carry pricing in its aggregate
JSON; if the fetch comes back empty, point ``--url`` (or ``pricing_url``) at a
compatible source that does. The script never clobbers the cache or the table
with an empty price set.
"""
from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path

from nightdesk.cli import _init
from nightdesk.domain import pricing


# Sentinels wrapping _PRICE_TABLE in domain/cost.py (keep them intact when
# hand-editing — this script replaces the block between them).
_BEGIN = "# @update-prices begin"
_END = "# @update-prices end"
_BLOCK_RE = re.compile(
    re.escape(_BEGIN) + r"\n.*?\n" + re.escape(_END), re.DOTALL
)
_AS_OF_RE = re.compile(r'PRICES_AS_OF = "[^"]*"')

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COST_PATH = _REPO_ROOT / "src" / "nightdesk" / "domain" / "cost.py"


def _format_table(prices: dict[str, dict[str, float]]) -> str:
    """Render a _PRICE_TABLE literal (Anthropic claude-* prefixes, sorted)."""
    prefixes = sorted(p for p in prices if p.startswith("claude"))
    lines = ["_PRICE_TABLE: list[tuple[str, dict[str, float]]] = ["]
    for prefix in prefixes:
        p = prices[prefix]
        lines.append(f"    ({prefix!r}, {{")
        lines.append(f'        "input": {p["input"]!r}, "output": {p["output"]!r},')
        lines.append(
            f'        "cache_write": {p["cache_write"]!r}, '
            f'"cache_read": {p["cache_read"]!r},'
        )
        lines.append("    }),")
    lines.append("]")
    return "\n".join(lines)


def _regenerate_table(
    prices: dict[str, dict[str, float]], as_of: str, *, dry_run: bool
) -> bool:
    """Rewrite _PRICE_TABLE + PRICES_AS_OF in domain/cost.py. Returns success."""
    claude = {k: v for k, v in prices.items() if k.startswith("claude")}
    if not claude:
        print("  --regenerate: no 'claude-*' prefixes in fetched prices; "
              "leaving cost.py untouched.")
        return False

    text = _COST_PATH.read_text()
    if not _BLOCK_RE.search(text):
        print(f"  --regenerate: sentinels not found in {_COST_PATH}; "
              f"leaving cost.py untouched.")
        return False
    if not _AS_OF_RE.search(text):
        print(f"  --regenerate: PRICES_AS_OF line not found in {_COST_PATH}; "
              f"leaving cost.py untouched.")
        return False

    new_block = f"{_BEGIN}\n{_format_table(claude)}\n{_END}"
    new_text = _BLOCK_RE.sub(new_block, text)
    new_text = _AS_OF_RE.sub(f'PRICES_AS_OF = "{as_of}"', new_text)

    if dry_run:
        print(f"  [dry-run] would rewrite {_COST_PATH} "
              f"({len(claude)} claude prefixes, as_of={as_of})")
    else:
        _COST_PATH.write_text(new_text)
        print(f"  rewrote {_COST_PATH} ({len(claude)} claude prefixes, "
              f"PRICES_AS_OF={as_of})")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Refresh nightdesk's model-price cache (release-time).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--url", default=None,
        help="Pricing endpoint URL (overrides config/env for this run).",
    )
    ap.add_argument(
        "--regenerate", action="store_true",
        help="Also rewrite the in-repo _PRICE_TABLE / PRICES_AS_OF in cost.py.",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Report what would happen; write nothing.",
    )
    args = ap.parse_args()

    cfg = _init()
    url = args.url or cfg.pricing_url
    now = datetime.datetime.now(datetime.timezone.utc)

    print(f"Fetching prices from {url} ...")
    prices = pricing.fetch_live_prices(url)
    if not prices:
        print("No usable prices returned (endpoint unreachable, or reachable "
              "but carrying no pricing). Cache and table left unchanged.\n"
              "Point pricing_url / --url at a compatible JSON source.")
        return 1

    as_of = now.strftime("%Y-%m-%d")
    print(f"Fetched {len(prices)} model prices (as_of {as_of}). Sample:")
    for prefix in sorted(prices)[:8]:
        print(f"  {prefix}: {prices[prefix]}")

    path = pricing.cache_path(cfg.data_dir)
    if not args.dry_run:
        if path is None:
            print("No data_dir configured; cannot write cache. "
                  "Set data_dir / NIGHTDESK_DATA_DIR.")
            return 1
        pricing.write_cache(path, prices, source=url, fetched_at=now.isoformat())
        print(f"Wrote cache: {path}")
    else:
        print(f"[dry-run] would write cache: {path}")

    info = pricing.PriceInfo("live", as_of, prices)
    print(f"Resolved: {info.label}")

    if args.regenerate:
        _regenerate_table(prices, as_of, dry_run=args.dry_run)

    return 0


if __name__ == "__main__":
    sys.exit(main())
