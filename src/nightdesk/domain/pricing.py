"""Live model pricing: fetch → cache → bundled-table fallback chain.

Analytics cost estimates should reflect *current* prices, not a stale in-repo
table. This module resolves a price set with a fail-soft chain:

    1. live fetch from a configurable HTTP endpoint (short timeout, fail fast)
    2. on-disk cache (written under the nightdesk data dir, with a TTL)
    3. the bundled ``_PRICE_TABLE`` in :mod:`nightdesk.domain.cost` (last resort)

Within the TTL window, repeated renders reuse the cache without re-fetching.
Beyond it, we try live first, then a (now stale) cache, then the table. Every
result records which source produced the numbers and an as-of date so the UI
can say "live prices · fetched <date>" / "cached prices · …" / "bundled prices
· as of <date>".

Endpoint format
---------------
The endpoint may return any of three JSON shapes; :func:`normalize_endpoint_json`
accepts them all so the URL can point at any compatible source:

* **canonical** (our cache shape)::

      {"source": "...", "fetched_at": "...",
       "prices": {"claude-opus-4": {"input": 5.0, "output": 25.0,
                                    "cache_read": 0.5, "cache_write": 6.25}}}

* **registry aggregate** (e.g. models.dev ``models.json``, keyed by
  ``provider/model`` with a ``cost`` block)::

      {"anthropic/claude-opus-4-7": {"cost": {"input": 5.0, "output": 25.0,
                                              "cache_read": 0.5, "cache_write": 6.25}, ...}}

* **flat** (prefix → prices directly)::

      {"claude-opus-4": {"input": 5.0, "output": 25.0, ...}}

Prices are USD per 1M tokens, matching :func:`nightdesk.domain.cost.compute_cost`.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional

from nightdesk.domain.cost import (
    PRICES_AS_OF,
    apply_prices,
    price_table,
)

log = logging.getLogger(__name__)

# Public model-pricing registry. Override via config (``pricing_url``) or the
# ``NIGHTDESK_PRICING_URL`` env var to point at any compatible JSON source.
DEFAULT_PRICING_URL = "https://models.dev/models.json"

# On-disk cache filename (lives under the nightdesk data dir).
PRICE_CACHE_FILENAME = "model_prices.json"

# How long the cache is trusted without re-fetching.
DEFAULT_TTL = timedelta(hours=24)

# Live-fetch budget. Short on purpose: a slow/unreachable upstream must never
# hang the analytics page — we fail fast and fall through to the cache/table.
DEFAULT_TIMEOUT_SECONDS = 3.0

# Field-name aliases we accept for each price component across registries.
_INPUT_ALIASES = ("input", "input_price", "prompt")
_OUTPUT_ALIASES = ("output", "output_price", "completion")
_CACHE_READ_ALIASES = ("cache_read", "cached_read", "read_cache", "cache_hit")
_CACHE_WRITE_ALIASES = (
    "cache_write",
    "cached_write",
    "write_cache",
    "cache_creation",
)

PriceMap = Mapping[str, Mapping[str, float]]
PriceFetcher = Callable[[str], Optional[dict[str, dict[str, float]]]]


@dataclass(frozen=True)
class PriceInfo:
    """A resolved price set plus provenance for the UI."""

    source: str  # "live" | "cache" | "table"
    as_of: str  # human date (YYYY-MM-DD)
    prices: dict[str, dict[str, float]]  # prefix -> {input, output, cache_read, cache_write}

    def prices_for(self, model: Optional[str]) -> Optional[dict[str, float]]:
        """Longest-prefix match for ``model`` (e.g. "claude-opus-4-7" → opus)."""
        if not model:
            return None
        best: Optional[str] = None
        for prefix in self.prices:
            if model.startswith(prefix) and (best is None or len(prefix) > len(best)):
                best = prefix
        return dict(self.prices[best]) if best else None

    def cost(
        self,
        model: Optional[str],
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
    ) -> float:
        """USD cost for one bucket of tokens, or 0.0 for an unknown model."""
        prices = self.prices_for(model)
        if not prices:
            return 0.0
        return apply_prices(
            prices,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )

    @property
    def label(self) -> str:
        """Source-aware wording for the analytics header."""
        if self.source == "live":
            return f"live prices · fetched {self.as_of}"
        if self.source == "cache":
            return f"cached prices · fetched {self.as_of}"
        return f"bundled prices · as of {self.as_of}"


# --------------------------------------------------------------------------
# Bundled-table accessor (the last-resort source).
# --------------------------------------------------------------------------
def table_price_info() -> PriceInfo:
    """A PriceInfo backed by the in-repo fallback table."""
    return PriceInfo("table", PRICES_AS_OF, table_prices())


def table_prices() -> dict[str, dict[str, float]]:
    """The bundled table as a canonical prefix → price-dict mapping."""
    return {prefix: dict(prices) for prefix, prices in price_table()}


# --------------------------------------------------------------------------
# Endpoint normalization (canonical / registry-aggregate / flat).
# --------------------------------------------------------------------------
def _coerce_block(block: Mapping[str, object]) -> Optional[dict[str, float]]:
    """Pull the four price components out of one model's block, or None."""
    if not isinstance(block, Mapping):
        return None

    def pick(aliases) -> Optional[float]:
        for name in aliases:
            if name in block:
                try:
                    val = float(block[name])  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    continue
                if val == val and val not in (float("inf"), float("-inf")):  # finite
                    return val
        return None

    out = {
        "input": pick(_INPUT_ALIASES),
        "output": pick(_OUTPUT_ALIASES),
        "cache_read": pick(_CACHE_READ_ALIASES),
        "cache_write": pick(_CACHE_WRITE_ALIASES),
    }
    if any(v is None for v in out.values()):
        return None
    return out  # type: ignore[return-value]


def normalize_endpoint_json(data: object) -> dict[str, dict[str, float]]:
    """Normalize any supported endpoint shape into prefix → price dict.

    Models are keyed by prefix (a ``provider/`` prefix on the key, as in
    models.dev's ``anthropic/claude-opus-4-7``, is stripped). Each model's
    prices may live under a ``cost``/``pricing``/``price`` sub-block (registry
    shape) or directly on the entry (flat shape).
    """
    out: dict[str, dict[str, float]] = {}
    if isinstance(data, Mapping):
        # Canonical shape wraps the map under "prices".
        src = data["prices"] if isinstance(data.get("prices"), Mapping) else data
    else:
        return out

    for key, val in src.items():
        if not isinstance(key, str) or not isinstance(val, Mapping):
            continue
        block = None
        for sub in ("cost", "pricing", "price"):
            cand = val.get(sub)
            if isinstance(cand, Mapping):
                block = cand
                break
        if block is None:
            block = val  # flat shape
        prices = _coerce_block(block)
        if prices is None:
            continue
        prefix = key.split("/", 1)[-1]  # strip "provider/" if present
        if prefix:
            out[prefix] = prices
    return out


# --------------------------------------------------------------------------
# Live fetch (short timeout, never raises).
# --------------------------------------------------------------------------
def fetch_live_prices(
    url: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> Optional[dict[str, dict[str, float]]]:
    """GET ``url``, return normalized prices, or None on any failure/empty.

    Never raises: a slow or unreachable upstream is logged at debug and yields
    None so the caller falls through to the cache/table.
    """
    if not url:
        return None
    try:
        import httpx

        with httpx.Client(timeout=timeout_seconds, follow_redirects=True) as client:
            resp = client.get(url, headers={"Accept": "application/json"})
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001 — fail-soft by design
        log.debug("price fetch from %s failed: %s", url, exc)
        return None
    prices = normalize_endpoint_json(payload)
    return prices or None


# --------------------------------------------------------------------------
# On-disk cache.
# --------------------------------------------------------------------------
def cache_path(data_dir: Optional[Path]) -> Optional[Path]:
    """Where the price cache lives, or None if caching is disabled (no data dir)."""
    return Path(data_dir) / PRICE_CACHE_FILENAME if data_dir else None


def _read_raw(path: Optional[Path]) -> Optional[dict]:
    if not path or not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO timestamp/date to aware UTC, or None."""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Bare date YYYY-MM-DD → midnight UTC.
        try:
            dt = datetime.fromisoformat(s[:10] + "T00:00:00+00:00")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _as_date(value: Optional[str]) -> str:
    dt = _parse_iso(value)
    if dt is not None:
        return dt.strftime("%Y-%m-%d")
    return str(value)[:10] if value else ""


def read_cache(path: Optional[Path]) -> Optional[tuple[dict[str, dict[str, float]], str]]:
    """Return (prices, fetched_at) from the cache, or None if missing/unusable."""
    rec = _read_raw(path)
    if not rec:
        return None
    prices = normalize_endpoint_json(rec.get("prices") or rec)
    if not prices:
        return None
    fetched_at = str(rec.get("fetched_at") or rec.get("as_of") or "")
    return prices, fetched_at


def write_cache(
    path: Path,
    prices: dict[str, dict[str, float]],
    *,
    source: str,
    fetched_at: str,
) -> None:
    """Persist prices in the canonical shape with a fetched_at timestamp."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"source": source, "fetched_at": fetched_at, "prices": prices}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)


def cache_fresh(
    path: Optional[Path], *, now: datetime, ttl: timedelta = DEFAULT_TTL
) -> bool:
    """True if the cache exists and was fetched within ``ttl`` of ``now``."""
    rec = _read_raw(path)
    if not rec:
        return False
    fetched = _parse_iso(rec.get("fetched_at") or rec.get("as_of"))
    if fetched is None:
        return False
    return (now.astimezone(timezone.utc) - fetched) < ttl


# --------------------------------------------------------------------------
# Resolution: the fallback chain.
# --------------------------------------------------------------------------
def resolve_prices(
    data_dir: Optional[Path] = None,
    *,
    url: Optional[str] = None,
    now: Optional[datetime] = None,
    ttl: timedelta = DEFAULT_TTL,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    fetcher: Optional[PriceFetcher] = None,
) -> PriceInfo:
    """Resolve a PriceInfo via live → cache → table, never raising.

    Within ``ttl`` of the cache's ``fetched_at`` the cache is reused without a
    network hit. Past the TTL we try live (short timeout), then a stale cache,
    then the bundled table. With ``url`` unset, live is skipped entirely.
    """
    now = now or datetime.now(timezone.utc)
    path = cache_path(data_dir)
    fetch = fetcher or (lambda u: fetch_live_prices(u, timeout_seconds=timeout_seconds))

    # 1. Fresh cache short-circuits the network entirely (the TTL optimization).
    if cache_fresh(path, now=now, ttl=ttl):
        cached = read_cache(path)
        if cached is not None:
            prices, fetched_at = cached
            return PriceInfo("cache", _as_date(fetched_at), prices)

    # 2. Past TTL (or no cache): try live first, fail fast.
    if url:
        live = fetch(url)
        if live:
            fetched_at = now.astimezone(timezone.utc).isoformat()
            if path is not None:
                try:
                    write_cache(path, live, source=url, fetched_at=fetched_at)
                except OSError as exc:
                    log.debug("price cache write to %s failed: %s", path, exc)
            return PriceInfo("live", _as_date(fetched_at), live)

    # 3. A stale cache still beats the bundled table.
    cached = read_cache(path)
    if cached is not None:
        prices, fetched_at = cached
        return PriceInfo("cache", _as_date(fetched_at), prices)

    # 4. Last resort: the in-repo fallback table.
    return table_price_info()
