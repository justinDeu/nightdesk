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
The endpoint may return any of several JSON shapes; :func:`normalize_endpoint_json`
accepts them all so the URL can point at any compatible source:

* **canonical** (our cache shape)::

      {"source": "...", "fetched_at": "...",
       "prices": {"claude-opus-4": {"input": 5.0, "output": 25.0,
                                    "cache_read": 0.5, "cache_write": 6.25}}}

* **registry aggregate** (e.g. models.dev ``models.json``, keyed by
  ``provider/model`` with a ``cost`` block)::

      {"anthropic/claude-opus-4-4-7": {"cost": {"input": 5.0, "output": 25.0,
                                              "cache_read": 0.5, "cache_write": 6.25}, ...}}

* **flat** (prefix → prices directly)::

      {"claude-opus-4": {"input": 5.0, "output": 25.0, ...}}

* **list under ``data``** (e.g. OpenRouter ``/api/v1/models``, each entry has
  an ``id`` plus a ``pricing`` sub-block)::

      {"data": [{"id": "anthropic/claude-opus-4", "pricing": {
          "prompt": 1.5e-05, "completion": 7.5e-05,
          "input_cache_read": 1.5e-06, "input_cache_write": 1.875e-05}}]}

All prices are normalized to **USD per 1M tokens** before they leave the
parser, matching :func:`nightdesk.domain.cost.compute_cost`. Sources that
publish per-token prices (LiteLLM ``*_cost_per_token`` / OpenRouter
``pricing.prompt`` etc.) are scaled by 1e6 inside :func:`_coerce_block`; the
canonical/registry/flat shapes already publish per-1M values and pass through
unchanged.
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
    _model_candidates,
    apply_prices,
    bundled_prices_all,
    bundled_prices_by_vendor,
    price_table,
)

log = logging.getLogger(__name__)

# Public model-pricing registry. Override via config (``pricing_url``) or the
# ``NIGHTDESK_PRICING_URL`` env var to point at any compatible JSON source.
#
# LiteLLM's community price file is the default because it (a) is actively
# maintained, (b) publishes per-token USD pricing INCLUDING prompt-cache
# read/write fields, and (c) carries first-party Anthropic entries keyed by
# the SDK's hyphenated model ids (``claude-opus-4-5`` etc.), which line up with
# the longest-prefix matching used here and with the bundled fallback table.
DEFAULT_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

# On-disk cache filename (lives under the nightdesk data dir).
PRICE_CACHE_FILENAME = "model_prices.json"

# How long the cache is trusted without re-fetching.
DEFAULT_TTL = timedelta(hours=24)

# Live-fetch budget. Short on purpose: a slow/unreachable upstream must never
# hang the analytics page — we fail fast and fall through to the cache/table.
DEFAULT_TIMEOUT_SECONDS = 3.0

# Per-token field names (USD per single token). When a block carries one of
# these, its value is scaled by 1e6 to reach the per-1M unit the rest of the
# system expects. Checked BEFORE the per-1M aliases so a mixed block resolves
# to a single consistent unit.
_INPUT_PER_TOKEN_ALIASES = ("input_cost_per_token", "prompt")
_OUTPUT_PER_TOKEN_ALIASES = ("output_cost_per_token", "completion")
_CACHE_READ_PER_TOKEN_ALIASES = ("cache_read_input_token_cost", "input_cache_read")
_CACHE_WRITE_PER_TOKEN_ALIASES = (
    "cache_creation_input_token_cost",
    "input_cache_write",
    "cache_creation_input_token_cost_per_token",
)

# Per-1M field names (USD per 1M tokens) — pass through unscaled.
_INPUT_ALIASES = ("input", "input_price")
_OUTPUT_ALIASES = ("output", "output_price")
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
# Endpoint normalization (canonical / registry-aggregate / flat / list-data).
# --------------------------------------------------------------------------
def _finite_float(block: Mapping[str, object], aliases: tuple[str, ...]) -> Optional[float]:
    """First finite float in ``block`` under any of ``aliases``, else None."""
    for name in aliases:
        if name in block:
            try:
                val = float(block[name])  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if val == val and val not in (float("inf"), float("-inf")):  # finite
                return val
    return None


def _coerce_block(block: Mapping[str, object]) -> Optional[dict[str, float]]:
    """Pull the four per-1M price components out of one model's block, or None.

    Per-token sources (LiteLLM ``*_cost_per_token``, OpenRouter
    ``pricing.prompt`` etc.) are detected by field name and scaled by 1e6 so
    the result is always USD per 1M tokens. When a block mixes per-token and
    per-1M fields, the per-token fields win (they are checked first) so a
    block resolves to one consistent unit.
    """
    if not isinstance(block, Mapping):
        return None

    def component(per_token: tuple[str, ...], per_1m: tuple[str, ...]) -> Optional[float]:
        val = _finite_float(block, per_token)
        if val is not None:
            return val * 1_000_000.0
        return _finite_float(block, per_1m)

    out = {
        "input": component(_INPUT_PER_TOKEN_ALIASES, _INPUT_ALIASES),
        "output": component(_OUTPUT_PER_TOKEN_ALIASES, _OUTPUT_ALIASES),
        "cache_read": component(_CACHE_READ_PER_TOKEN_ALIASES, _CACHE_READ_ALIASES),
        "cache_write": component(_CACHE_WRITE_PER_TOKEN_ALIASES, _CACHE_WRITE_ALIASES),
    }
    if any(v is None for v in out.values()):
        return None
    return out  # type: ignore[return-value]


# Providers (under ``litellm_provider`` or a similar discriminator) whose
# entries we treat as first-party Anthropic prices when present. Reseller /
# cloud copies (bedrock, vertex_ai, azure_ai, openrouter, ...) are skipped so
# they cannot shadow or duplicate the canonical model ids. Empty means "accept
# all" (preserves the flat/registry shapes that carry no provider tag).
_ANTHROPIC_PROVIDERS = frozenset({"anthropic"})


def _is_anthropic(key: str, entry: Mapping[str, object]) -> bool:
    """True if ``entry`` / ``key`` looks like a first-party Anthropic model.

    Honors an explicit ``litellm_provider`` / ``provider`` field when present
    (LiteLLM), else falls back to the ``provider/`` prefix on the key
    (OpenRouter-style ``anthropic/claude-opus-4``). Entries with no
    discriminator at all (flat/canonical shapes) are accepted as-is.
    """
    if not _ANTHROPIC_PROVIDERS:
        return True
    prov = entry.get("litellm_provider") or entry.get("provider")
    if prov is not None:
        return str(prov).lower() in _ANTHROPIC_PROVIDERS
    # No provider field: trust the "anthropic/" id prefix if one is present.
    if "/" in key:
        return key.split("/", 1)[0].lower() in _ANTHROPIC_PROVIDERS
    return True  # no discriminator → assume first-party (flat/canonical)


def _price_block_from_entry(val: Mapping[str, object]) -> Optional[Mapping[str, object]]:
    """The price sub-block of a model entry, or the entry itself if flat."""
    for sub in ("cost", "pricing", "price"):
        cand = val.get(sub)
        if isinstance(cand, Mapping):
            return cand
    return val


def _normalize_keyed(src: Mapping[str, object]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for key, val in src.items():
        if not isinstance(key, str) or not isinstance(val, Mapping):
            continue
        if not _is_anthropic(key, val):
            continue
        block = _price_block_from_entry(val)
        prices = _coerce_block(block)
        if prices is None:
            continue
        prefix = key.split("/", 1)[-1]  # strip "provider/" if present
        if prefix:
            out[prefix] = prices
    return out


def normalize_endpoint_json(data: object) -> dict[str, dict[str, float]]:
    """Normalize any supported endpoint shape into prefix → per-1M price dict.

    Supported shapes:

    * **canonical** ``{"prices": {prefix: {...}}}``
    * **registry aggregate** keyed by ``provider/model`` with a
      ``cost``/``pricing``/``price`` sub-block (e.g. models.dev, LiteLLM)
    * **flat** ``{prefix: {...}}``
    * **list under ``data``** (e.g. OpenRouter ``/api/v1/models``): each entry
      has an ``id`` (a ``provider/`` prefix is stripped) and a price block

    A ``litellm_provider`` / ``provider`` field is honored when present: only
    first-party Anthropic entries are kept, so cloud-reseller copies (bedrock,
    vertex_ai, ...) do not shadow the canonical model ids.

    All returned prices are USD per 1M tokens regardless of the source's unit.
    """
    if not isinstance(data, Mapping):
        return {}

    # Canonical shape wraps the map under "prices".
    if isinstance(data.get("prices"), Mapping):
        return _normalize_keyed(data["prices"])  # type: ignore[arg-type]

    # List under "data" (OpenRouter). Key each entry by its id.
    data_list = data.get("data")
    if isinstance(data_list, list):
        remapped: dict[str, Mapping[str, object]] = {}
        for entry in data_list:
            if isinstance(entry, Mapping):
                eid = entry.get("id")
                if isinstance(eid, str) and eid:
                    remapped[eid] = entry
        return _normalize_keyed(remapped)

    # Dict-keyed (registry aggregate or flat).
    return _normalize_keyed(data)


# --------------------------------------------------------------------------
# Vendor-aware resolution and run pricing snapshots.
# --------------------------------------------------------------------------
#
# The chain above (``normalize_endpoint_json`` / ``resolve_prices`` /
# ``PriceInfo``) predates the provider model and stays exactly as it is:
# Anthropic-only, prefix-matched, feeding the existing analytics repricing
# path. Vendor-aware resolution is an additive layer on top, built from a
# *vendor-preserving* parse of the same upstream payloads
# (``normalize_endpoint_json_all``) so GLM and other non-Anthropic rows are
# no longer discarded before anyone gets a chance to price them.
#
# Design choice: rather than mutate ``_is_anthropic`` in place (which would
# change ``normalize_endpoint_json``'s output shape for every existing
# caller -- the cache round-trip, the default-URL smoke test, the
# reseller-filtering regression tests), vendor resolution reads from this
# parallel, vendor-tagged parse. The Anthropic-only chain and the
# vendor-aware chain currently overlap in what they can price (both can
# resolve Anthropic models); they are expected to converge once run-time
# pricing snapshots replace analytics' live repricing in a later change.

# Vendors this module knows how to resolve explicitly. Anything else falls
# through to the legacy vendor-agnostic best-effort path.
KNOWN_VENDORS = frozenset({"anthropic", "zai", "openrouter", "openai", "ollama"})

_PRICE_COMPONENTS = ("input", "output", "cache_read", "cache_write")


def _entry_vendor_hint(key: str, entry: Mapping[str, object]) -> Optional[str]:
    """Best-effort vendor tag for one raw price entry, or None with no signal.

    Honors an explicit ``litellm_provider`` / ``provider`` field when
    present, else the ``provider/`` prefix on the key. Flat/canonical
    entries carrying no discriminator get no vendor hint at all -- callers
    treat that as "unattributed", never as a guess.
    """
    prov = entry.get("litellm_provider") or entry.get("provider")
    if prov:
        return str(prov).lower()
    if "/" in key:
        return key.split("/", 1)[0].lower()
    return None


def _normalize_keyed_all(
    src: Mapping[str, object],
) -> dict[str, tuple[Optional[str], dict[str, float]]]:
    """Vendor-preserving counterpart of ``_normalize_keyed``: every parseable
    entry survives (no Anthropic-only filtering), tagged with its inferred
    vendor hint."""
    out: dict[str, tuple[Optional[str], dict[str, float]]] = {}
    for key, val in src.items():
        if not isinstance(key, str) or not isinstance(val, Mapping):
            continue
        block = _price_block_from_entry(val)
        prices = _coerce_block(block)
        if prices is None:
            continue
        prefix = key.split("/", 1)[-1]  # strip one leading "provider/" segment
        if prefix:
            out[prefix] = (_entry_vendor_hint(key, val), prices)
    return out


def normalize_endpoint_json_all(
    data: object,
) -> dict[str, tuple[Optional[str], dict[str, float]]]:
    """Like :func:`normalize_endpoint_json`, but keeps every vendor's rows.

    Returns ``{prefix: (vendor_hint, prices)}``. ``vendor_hint`` is the raw
    inferred tag (e.g. ``"anthropic"``, ``"cloudflare"``, ``"fireworks_ai"``)
    or ``None`` when the source shape carries no discriminator at all.

    The one special case: the list-under-``data`` shape (OpenRouter's own
    ``/api/v1/models``) tags every entry ``"openrouter"`` regardless of the
    ``provider/model`` id prefix -- that prefix names the underlying model,
    not the seller, and OpenRouter's price is its own markup over that model.
    """
    if not isinstance(data, Mapping):
        return {}

    if isinstance(data.get("prices"), Mapping):
        return _normalize_keyed_all(data["prices"])  # type: ignore[arg-type]

    data_list = data.get("data")
    if isinstance(data_list, list):
        remapped: dict[str, Mapping[str, object]] = {}
        for entry in data_list:
            if isinstance(entry, Mapping):
                eid = entry.get("id")
                if isinstance(eid, str) and eid:
                    remapped[eid] = entry
        parsed = _normalize_keyed_all(remapped)
        return {prefix: ("openrouter", prices) for prefix, (_v, prices) in parsed.items()}

    return _normalize_keyed_all(data)


def _prefix_lookup(
    model: str, table: Mapping[str, Mapping[str, float]]
) -> Optional[dict[str, float]]:
    """Longest-prefix match against ``table``, trying normalized candidates
    in order (exact id first, then suffix-stripped, then vendor-prefix-
    stripped) so a variant with its own row (``"glm-5.2[1m]"``) is preferred
    over its stripped form when both exist."""
    for candidate in _model_candidates(model):
        best: Optional[str] = None
        for prefix in table:
            if candidate.startswith(prefix) and (best is None or len(prefix) > len(best)):
                best = prefix
        if best is not None:
            return dict(table[best])
    return None


def _vendor_table(
    live_all: Mapping[str, tuple[Optional[str], dict[str, float]]], vendor: str
) -> dict[str, dict[str, float]]:
    return {prefix: prices for prefix, (v, prices) in live_all.items() if v == vendor}


def _zai_reseller_matches(
    model: str, live_all: Mapping[str, tuple[Optional[str], dict[str, float]]]
) -> list[dict[str, float]]:
    """Reseller rows (any vendor hint) whose deepest path segment names this
    GLM model, e.g. ``cloudflare/@cf/zai-org/glm-5.2`` or
    ``fireworks_ai/glm-5.2`` both match ``model="glm-5.2"``."""
    candidates = set(_model_candidates(model))
    matches = []
    for key, (_vendor, prices) in live_all.items():
        deep = key.rsplit("/", 1)[-1]
        if deep in candidates:
            matches.append(prices)
    return matches


def _median_prices(entries: list[dict[str, float]]) -> dict[str, float]:
    """Component-wise median across reseller price rows. Never the cheapest:
    the min systematically underestimates what a user actually pays when
    resellers vary."""
    def median(component: str) -> float:
        values = sorted(e[component] for e in entries)
        n = len(values)
        mid = n // 2
        if n % 2:
            return values[mid]
        return (values[mid - 1] + values[mid]) / 2.0

    return {component: median(component) for component in _PRICE_COMPONENTS}


_ZERO_COST_PRICES: dict[str, float] = {c: 0.0 for c in _PRICE_COMPONENTS}


def resolve_vendor_price(
    vendor: str,
    model: str,
    *,
    live_all: Optional[Mapping[str, tuple[Optional[str], dict[str, float]]]] = None,
    live_source: str = "live",
) -> tuple[Optional[dict[str, float]], str]:
    """Resolve per-1M-token prices for ``(vendor, model)``.

    ``live_all`` is a vendor-preserving parse of a live/cached pricing
    payload (:func:`normalize_endpoint_json_all`); omit it to resolve
    purely from the bundled table. ``live_source`` labels the tier
    ``live_all`` came from (``"live"`` or ``"cache"``) and is only used when
    a hit actually comes from it.

    Returns ``(prices, source)`` where ``source`` is ``"live"``/``"cache"``
    (per ``live_source``, when resolved from ``live_all``), ``"bundled"``, or
    ``"none"`` when nothing resolves (``prices`` is ``None`` in that case).

    Vendor rules (see docs/design/providers-and-endpoints.md, "Pricing
    integration"):

    * ``ollama`` -- always free.
    * ``zai`` -- curated bundled rows first; live reseller rows as fallback,
      taking the median across resellers when more than one prices a model.
    * ``anthropic`` / ``openai`` -- live rows tagged for that vendor, then
      the bundled table.
    * ``openrouter`` -- live rows tagged ``"openrouter"`` only (no bundled
      fallback: OpenRouter's markup isn't something to guess offline).
    * anything else -- legacy vendor-agnostic best-effort prefix match
      across whatever live data and bundled rows exist, so unknown vendors
      degrade gracefully instead of pricing as zero.
    """
    live_all = live_all or {}

    if vendor == "ollama":
        return dict(_ZERO_COST_PRICES), "bundled"

    if vendor == "zai":
        hit = _prefix_lookup(model, bundled_prices_by_vendor("zai"))
        if hit is not None:
            return hit, "bundled"
        matches = _zai_reseller_matches(model, live_all)
        if matches:
            return _median_prices(matches), live_source
        return None, "none"

    if vendor in ("anthropic", "openai"):
        hit = _prefix_lookup(model, _vendor_table(live_all, vendor))
        if hit is not None:
            return hit, live_source
        hit = _prefix_lookup(model, bundled_prices_by_vendor(vendor))
        if hit is not None:
            return hit, "bundled"
        return None, "none"

    if vendor == "openrouter":
        hit = _prefix_lookup(model, _vendor_table(live_all, "openrouter"))
        if hit is not None:
            return hit, live_source
        return None, "none"

    # Unknown vendor: degrade to the old vendor-agnostic best-effort prefix
    # match, first against any live/cache data, then the full bundled table.
    agnostic_live = {prefix: prices for prefix, (_v, prices) in live_all.items()}
    hit = _prefix_lookup(model, agnostic_live)
    if hit is not None:
        return hit, live_source
    hit = _prefix_lookup(model, bundled_prices_all())
    if hit is not None:
        return hit, "bundled"
    return None, "none"


def build_pricing_snapshot(
    models: Mapping[str, str],
    *,
    live_all: Optional[Mapping[str, tuple[Optional[str], dict[str, float]]]] = None,
    live_source: str = "live",
    as_of: Optional[str] = None,
) -> dict[str, dict[str, object]]:
    """Build a run pricing snapshot: model id -> resolved rates + provenance.

    ``models`` maps model id -> vendor tag (the caller knows which endpoint
    served each model; this function is pure and touches no DB). ``live_all``
    is a vendor-preserving parse of a live/cached payload
    (:func:`normalize_endpoint_json_all`); omit it to resolve purely from the
    bundled table. ``as_of`` stamps every entry (default:
    :data:`nightdesk.domain.cost.PRICES_AS_OF`).

    Output shape, per the design doc::

        {model_id: {"vendor", "input", "output", "cache_read", "cache_write",
                    "source", "as_of"}}

    A model that resolves to nothing still gets an entry, with every rate
    field ``None`` and ``source="none"`` -- so a consumer can tell "priced at
    zero" (ollama) apart from "pricing was attempted and failed" (nulls).
    """
    as_of = as_of or PRICES_AS_OF
    snapshot: dict[str, dict[str, object]] = {}
    for model, vendor in models.items():
        prices, source = resolve_vendor_price(
            vendor, model, live_all=live_all, live_source=live_source
        )
        entry: dict[str, object] = {"vendor": vendor, "source": source, "as_of": as_of}
        if prices is None:
            entry.update({c: None for c in _PRICE_COMPONENTS})
        else:
            entry.update(prices)
        snapshot[model] = entry
    return snapshot


# --------------------------------------------------------------------------
# Vendor-preserving live fetch + cache chain, for run-time pricing snapshots.
#
# The chain above (``fetch_live_prices`` / ``resolve_prices``) feeds the
# Anthropic-only analytics repricing path and its on-disk cache shape has
# already discarded vendor tags by the time it hits disk. Run pricing
# snapshots need vendor-tagged rows (GLM included), so this is a parallel,
# smaller chain over the same upstream payload: live fetch -> its own cache
# file -> nothing (falls through to the bundled table via
# ``resolve_vendor_price``, which every caller already handles).
# --------------------------------------------------------------------------
PRICE_CACHE_ALL_FILENAME = "model_prices_all.json"


def cache_path_all(data_dir: Optional[Path]) -> Optional[Path]:
    """Where the vendor-tagged price cache lives, or None if disabled."""
    return Path(data_dir) / PRICE_CACHE_ALL_FILENAME if data_dir else None


def fetch_live_prices_all(
    url: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> Optional[dict[str, tuple[Optional[str], dict[str, float]]]]:
    """Like :func:`fetch_live_prices`, but vendor-preserving.

    Never raises: failures log at debug and yield None so callers fall
    through to the bundled table.
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
        log.debug("vendor-tagged price fetch from %s failed: %s", url, exc)
        return None
    parsed = normalize_endpoint_json_all(payload)
    return parsed or None


def _write_cache_all(
    path: Path,
    entries: Mapping[str, tuple[Optional[str], dict[str, float]]],
    *,
    source: str,
    fetched_at: str,
) -> None:
    prices = {
        key: {"vendor": vendor, **rates} for key, (vendor, rates) in entries.items()
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"source": source, "fetched_at": fetched_at, "prices": prices}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(path)


def _read_cache_all(
    path: Optional[Path],
) -> Optional[tuple[dict[str, tuple[Optional[str], dict[str, float]]], str]]:
    rec = _read_raw(path)
    if not rec:
        return None
    raw = rec.get("prices")
    if not isinstance(raw, Mapping):
        return None
    entries: dict[str, tuple[Optional[str], dict[str, float]]] = {}
    for key, block in raw.items():
        if not isinstance(key, str) or not isinstance(block, Mapping):
            continue
        rates = {c: block.get(c) for c in _PRICE_COMPONENTS}
        if any(not isinstance(v, (int, float)) for v in rates.values()):
            continue
        entries[key] = (block.get("vendor"), {c: float(v) for c, v in rates.items()})
    if not entries:
        return None
    fetched_at = str(rec.get("fetched_at") or "")
    return entries, fetched_at


def resolve_live_all(
    data_dir: Optional[Path] = None,
    *,
    url: Optional[str] = None,
    now: Optional[datetime] = None,
    ttl: timedelta = DEFAULT_TTL,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    fetcher: Optional[Callable[[str], Optional[dict]]] = None,
) -> tuple[dict[str, tuple[Optional[str], dict[str, float]]], str, str]:
    """Resolve vendor-tagged live prices via live -> cache -> nothing.

    Returns ``(live_all, source, as_of)`` where ``source`` is ``"live"``,
    ``"cache"``, or ``"bundled"`` (meaning neither live nor cache produced
    anything — callers pass ``live_all={}`` on to
    :func:`resolve_vendor_price`, which falls through to the bundled table on
    its own), and ``as_of`` is the human date (``YYYY-MM-DD``) the resolved
    prices are current as of: the fetch date for live, the cache's
    ``fetched_at`` date for cache, and :data:`nightdesk.domain.cost.PRICES_AS_OF`
    for the bundled fallback. Never raises.
    """
    now = now or datetime.now(timezone.utc)
    path = cache_path_all(data_dir)
    fetch = fetcher or (lambda u: fetch_live_prices_all(u, timeout_seconds=timeout_seconds))

    if cache_fresh(path, now=now, ttl=ttl):
        cached = _read_cache_all(path)
        if cached is not None:
            entries, fetched_at = cached
            return entries, "cache", _as_date(fetched_at)

    if url:
        live = fetch(url)
        if live:
            fetched_at = now.astimezone(timezone.utc).isoformat()
            if path is not None:
                try:
                    _write_cache_all(path, live, source=url, fetched_at=fetched_at)
                except OSError as exc:
                    log.debug("vendor-tagged price cache write to %s failed: %s", path, exc)
            return live, "live", _as_date(fetched_at)

    cached = _read_cache_all(path)
    if cached is not None:
        entries, fetched_at = cached
        return entries, "cache", _as_date(fetched_at)

    return {}, "bundled", PRICES_AS_OF


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
