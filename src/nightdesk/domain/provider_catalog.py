"""The known-offering catalog: seed data for provider registration.

Pure data, no DB/network access. See
``docs/design/providers-and-endpoints.md`` ("Known vendor catalog") for the
source table. The providers UI offers these as pre-filled templates; users
can also register a fully custom vendor/endpoint by hand.

Each catalog entry is an *offering* — a distinct way of paying for/
authenticating against a vendor — not a vendor. Vendors that support more
than one credential arrangement (OpenAI's pay-as-you-go API vs its ChatGPT/
Codex subscription; Anthropic's API vs a Claude subscription; ZAI's regular
API vs its flat-rate Coding Plan) show up as *separate* offerings, each
locked to exactly one ``credential_source``. This keeps "which credential do
I need" a single, unambiguous choice per offering instead of a vendor-level
menu that mixes secret and file-path credentials together.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class EndpointTemplate:
    """A suggested endpoint to seed when registering a catalog offering.

    Credential handling lives on the owning ``OfferingTemplate`` (every
    endpoint under an offering shares its single ``credential_source``), not
    here.
    """

    label: str
    protocol_kind: str
    base_url: Optional[str] = None
    harness_lock: Optional[str] = None
    default_model: Optional[str] = None
    # Seeded into the endpoint's model menu at create time. Mainly for
    # protocols with no list operation (see
    # ``nightdesk.domain.providers.PROTOCOLS_WITHOUT_MODEL_LIST``) — the user
    # can't pull a menu, so the catalog offers sensible starting ids instead
    # of an empty list and a blank text box.
    models: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class OfferingTemplate:
    """One vendor offering: a fixed credential mode plus the endpoint(s) it seeds."""

    key: str
    label: str
    vendor: str
    credential_source: str
    # Default path/hint shown next to a file-based credential input, e.g. the
    # Codex OAuth cache or the Claude Code credentials file. None for
    # secret-based (api_key/env_var) or credential-less (none) offerings.
    credential_hint: Optional[str] = None
    description: str = ""
    # Prefilled Provider.name so two offerings on the same vendor (e.g. the
    # two OpenAI offerings) don't collide on the unique-name constraint.
    suggested_name: str = ""
    endpoints: tuple[EndpointTemplate, ...] = field(default_factory=tuple)


# Default credential paths, seeded as hints for file-based credential
# sources. Stored separately from EndpointTemplate.base_url since they're
# credential *references* (host file paths), not endpoints' base_urls.
CODEX_OAUTH_DEFAULT_PATH = "~/.codex/auth.json"
CLAUDE_CREDENTIALS_DEFAULT_PATH = "~/.claude/.credentials.json"

_CATALOG: tuple[OfferingTemplate, ...] = (
    OfferingTemplate(
        key="openai_api",
        label="OpenAI API",
        vendor="openai",
        credential_source="api_key",
        description="Pay-as-you-go API key.",
        suggested_name="OpenAI (API key)",
        endpoints=(
            EndpointTemplate(label="API", protocol_kind="openai"),
        ),
    ),
    OfferingTemplate(
        key="openai_codex",
        label="OpenAI — ChatGPT subscription (Codex)",
        vendor="openai",
        credential_source="oauth_file",
        credential_hint=CODEX_OAUTH_DEFAULT_PATH,
        description="ChatGPT subscription via Codex OAuth.",
        suggested_name="OpenAI (Codex)",
        endpoints=(
            EndpointTemplate(
                label="Codex", protocol_kind="openai_codex",
                default_model="gpt-5.6-sol",
                # Codex does support a model list (pulled from
                # https://chatgpt.com/backend-api/codex/models — see
                # nightdesk.api.routes.providers), but a fresh install has
                # nothing pulled yet, so seed the menu with the current
                # top models instead of leaving it empty.
                models=(
                    "gpt-5.6-sol",
                    "gpt-5.6-terra",
                    "gpt-5.6-luna",
                    "gpt-5.5",
                    "gpt-5.4",
                    "gpt-5.4-mini",
                    "gpt-5.3-codex-spark",
                ),
            ),
        ),
    ),
    OfferingTemplate(
        key="anthropic_api",
        label="Anthropic API",
        vendor="anthropic",
        credential_source="api_key",
        description="Pay-as-you-go API key.",
        suggested_name="Anthropic (API key)",
        endpoints=(
            EndpointTemplate(label="API", protocol_kind="anthropic"),
        ),
    ),
    OfferingTemplate(
        key="claude_subscription",
        label="Claude subscription",
        vendor="anthropic",
        credential_source="subscription_file",
        credential_hint=CLAUDE_CREDENTIALS_DEFAULT_PATH,
        description="Claude Pro/Max subscription via Claude Code's stored credentials.",
        suggested_name="Claude subscription",
        endpoints=(
            EndpointTemplate(
                label="Claude Code", protocol_kind="anthropic", harness_lock="claude_sdk",
            ),
        ),
    ),
    OfferingTemplate(
        key="zai",
        label="ZAI",
        vendor="zai",
        credential_source="api_key",
        description="Pay-as-you-go API key — one key covers both endpoints below.",
        suggested_name="ZAI",
        endpoints=(
            EndpointTemplate(
                label="Anthropic-compatible",
                protocol_kind="anthropic_compat",
                base_url="https://api.z.ai/api/anthropic",
            ),
            EndpointTemplate(
                label="OpenAI-compatible",
                protocol_kind="openai_compat",
                base_url="https://api.z.ai/api/paas/v4",
            ),
        ),
    ),
    OfferingTemplate(
        key="zai_coding",
        label="ZAI coding plan",
        vendor="zai",
        credential_source="api_key",
        description=(
            "Flat-rate GLM Coding Plan subscription key. Z.AI serves the "
            "coding plan on the same Anthropic-compatible endpoint as the "
            "regular API (https://api.z.ai/api/anthropic) — the plan is "
            "tied to the key/account, not a different base URL."
        ),
        suggested_name="ZAI (Coding Plan)",
        endpoints=(
            EndpointTemplate(
                label="Anthropic-compatible (coding plan)",
                protocol_kind="anthropic_compat",
                base_url="https://api.z.ai/api/anthropic",
                default_model="glm-4.7",
            ),
        ),
    ),
    OfferingTemplate(
        key="openrouter",
        label="OpenRouter",
        vendor="openrouter",
        credential_source="api_key",
        description="Pay-as-you-go API key across many hosted models.",
        suggested_name="OpenRouter",
        endpoints=(
            EndpointTemplate(
                label="API", protocol_kind="openrouter", base_url="https://openrouter.ai/api/v1",
            ),
        ),
    ),
    OfferingTemplate(
        key="ollama",
        label="Ollama",
        vendor="ollama",
        credential_source="none",
        description="Local models, no credential required.",
        suggested_name="Ollama (local)",
        endpoints=(
            EndpointTemplate(
                label="Local", protocol_kind="ollama", base_url="http://localhost:11434/v1",
            ),
        ),
    ),
)


def catalog() -> tuple[OfferingTemplate, ...]:
    """Every known offering, in display order."""
    return _CATALOG


def get_offering(key: str) -> Optional[OfferingTemplate]:
    for o in _CATALOG:
        if o.key == key:
            return o
    return None
