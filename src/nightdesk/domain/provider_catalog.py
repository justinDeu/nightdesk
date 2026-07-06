"""The known-vendor catalog: seed data for provider registration.

Pure data, no DB/network access. See
``docs/design/providers-and-endpoints.md`` ("Known vendor catalog") for the
source table. The providers UI offers these as pre-filled templates; users
can also register a fully custom vendor/endpoint by hand.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class EndpointTemplate:
    """A suggested endpoint to seed when registering a catalog vendor."""

    label: str
    protocol_kind: str
    base_url: Optional[str] = None
    credential_source: str = "api_key"
    harness_lock: Optional[str] = None
    default_model: Optional[str] = None


@dataclass(frozen=True)
class VendorTemplate:
    vendor: str
    label: str
    endpoints: tuple[EndpointTemplate, ...] = field(default_factory=tuple)


_CATALOG: tuple[VendorTemplate, ...] = (
    VendorTemplate(
        vendor="zai",
        label="ZAI",
        endpoints=(
            EndpointTemplate(
                label="Anthropic-compatible",
                protocol_kind="anthropic_compat",
                base_url="https://api.z.ai/api/anthropic",
                credential_source="api_key",
            ),
            EndpointTemplate(
                label="OpenAI-compatible",
                protocol_kind="openai_compat",
                base_url="https://api.z.ai/api/paas/v4",
                credential_source="api_key",
            ),
        ),
    ),
    VendorTemplate(
        vendor="anthropic",
        label="Anthropic",
        endpoints=(
            EndpointTemplate(
                label="API key",
                protocol_kind="anthropic",
                credential_source="api_key",
            ),
            EndpointTemplate(
                label="Claude subscription",
                protocol_kind="anthropic",
                credential_source="subscription_file",
                harness_lock="claude_sdk",
            ),
        ),
    ),
    VendorTemplate(
        vendor="openai",
        label="OpenAI",
        endpoints=(
            EndpointTemplate(
                label="API key",
                protocol_kind="openai",
                credential_source="api_key",
            ),
            EndpointTemplate(
                label="ChatGPT / Codex",
                protocol_kind="openai_codex",
                credential_source="oauth_file",
                default_model=None,
            ),
        ),
    ),
    VendorTemplate(
        vendor="openrouter",
        label="OpenRouter",
        endpoints=(
            EndpointTemplate(
                label="API key",
                protocol_kind="openrouter",
                base_url="https://openrouter.ai/api/v1",
                credential_source="api_key",
            ),
        ),
    ),
    VendorTemplate(
        vendor="ollama",
        label="Ollama",
        endpoints=(
            EndpointTemplate(
                label="Local",
                protocol_kind="ollama",
                base_url="http://localhost:11434/v1",
                credential_source="none",
            ),
        ),
    ),
)

# Default credential path for the Codex OAuth endpoint template. Stored
# separately from EndpointTemplate.base_url since it's a credential
# *reference* (a file path), not the endpoint's base_url.
CODEX_OAUTH_DEFAULT_PATH = "~/.codex/auth.json"


def catalog() -> tuple[VendorTemplate, ...]:
    """Every known vendor template, in display order."""
    return _CATALOG


def get_vendor_template(vendor: str) -> Optional[VendorTemplate]:
    for v in _CATALOG:
        if v.vendor == vendor:
            return v
    return None
