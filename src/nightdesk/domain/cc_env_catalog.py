"""Catalog of well-known Claude Code environment variables.

Surfaced by the profile editor as an "Add variable" dropdown so users
don't have to guess at exact names or look up docs. Categories group
related variables together; the editor renders each one with its
human-readable description as inline help text.

Authoritative source: https://code.claude.com/docs/en/env-vars and the
related setup / proxy / Bedrock / Vertex / model-config pages on the
same site. Keep this list pruned to variables a normal user might
actually toggle; deeply internal/diagnostic env vars stay out so the
dropdown doesn't sprawl.

Each entry:
    name        Exact env var name as Claude Code reads it.
    category    Section label in the dropdown.
    description One-line explanation shown next to the value input.
    secret      True iff the value should render as a password input
                and never appear in plaintext in HTML responses.
    example     Optional placeholder shown in the value input.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CcEnvVar:
    name: str
    category: str
    description: str
    secret: bool = False
    example: str | None = None


# Ordered so the dropdown reads top-to-bottom in a logical sequence:
# auth first, then routing, then models, behavior, network, then the
# platform integrations (Bedrock, Vertex). The Models / Routing /
# Behavior buckets are also promoted into dedicated fieldsets in the
# editor; the catalog still carries the metadata so descriptions and
# placeholders stay in one place.
CC_ENV_CATALOG: tuple[CcEnvVar, ...] = (
    # ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL are owned
    # by the Authentication section on the profile editor and intentionally
    # absent from this catalog — they cannot be set via the generic env editor.
    # --- Authentication ---------------------------------------------------
    CcEnvVar(
        name="ANTHROPIC_CUSTOM_HEADERS",
        category="Authentication",
        description=(
            "Extra HTTP headers added to every Anthropic API request. "
            "Newline-separated 'Name: value' pairs."
        ),
        secret=True,
        example="X-Org-Id: abc123",
    ),
    CcEnvVar(
        name="CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
        category="Authentication",
        description=(
            "Cache lifetime for credentials emitted by an apiKeyHelper "
            "script, in milliseconds."
        ),
        example="3600000",
    ),
    # --- Routing ----------------------------------------------------------
    CcEnvVar(
        name="ANTHROPIC_BETAS",
        category="Routing",
        description=(
            "Comma-separated 'anthropic-beta' header values to opt into. "
            "Works with all auth methods, unlike the --betas CLI flag."
        ),
        example="prompt-caching-2024-07-31",
    ),
    CcEnvVar(
        name="CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
        category="Routing",
        description=(
            "Strip 'anthropic-beta' headers and beta tool-schema fields. "
            "Set to '1' when a gateway rejects unknown beta values."
        ),
        example="1",
    ),
    # --- Models -----------------------------------------------------------
    CcEnvVar(
        name="ANTHROPIC_MODEL",
        category="Models",
        description="Primary model used for the main turn.",
        example="claude-sonnet-4-5",
    ),
    CcEnvVar(
        name="ANTHROPIC_DEFAULT_OPUS_MODEL",
        category="Models",
        description=(
            "Model that the 'opus' alias (and 'opusplan' in Plan Mode) "
            "resolves to."
        ),
        example="claude-opus-4-7",
    ),
    CcEnvVar(
        name="ANTHROPIC_DEFAULT_SONNET_MODEL",
        category="Models",
        description=(
            "Model that the 'sonnet' alias (and 'opusplan' outside Plan "
            "Mode) resolves to."
        ),
        example="claude-sonnet-4-5",
    ),
    CcEnvVar(
        name="ANTHROPIC_DEFAULT_HAIKU_MODEL",
        category="Models",
        description=(
            "Model that the 'haiku' alias and background tasks resolve "
            "to. Replaces the deprecated ANTHROPIC_SMALL_FAST_MODEL."
        ),
        example="claude-haiku-4-5",
    ),
    CcEnvVar(
        name="ANTHROPIC_SMALL_FAST_MODEL",
        category="Models",
        description=(
            "Legacy override for the background / small-task model. "
            "Deprecated; prefer ANTHROPIC_DEFAULT_HAIKU_MODEL."
        ),
        example="claude-haiku-4-5",
    ),
    CcEnvVar(
        name="CLAUDE_CODE_SUBAGENT_MODEL",
        category="Models",
        description="Model used for subagent invocations.",
        example="claude-sonnet-4-5",
    ),
    # --- Network / proxy --------------------------------------------------
    CcEnvVar(
        name="HTTP_PROXY",
        category="Network",
        description=(
            "HTTP proxy for non-TLS traffic. Use the HTTPS variant for "
            "api.anthropic.com."
        ),
        example="http://proxy.example.com:8080",
    ),
    CcEnvVar(
        name="HTTPS_PROXY",
        category="Network",
        description=(
            "HTTPS proxy for all Anthropic API traffic. CONNECT-tunnel "
            "only."
        ),
        example="http://proxy.example.com:8080",
    ),
    CcEnvVar(
        name="NO_PROXY",
        category="Network",
        description=(
            "Hosts that bypass the proxy. Space- or comma-separated; "
            "'*' disables proxying entirely."
        ),
        example="localhost,127.0.0.1,.internal",
    ),
    # --- Behavior ---------------------------------------------------------
    CcEnvVar(
        name="API_TIMEOUT_MS",
        category="Behavior",
        description="Per-request API timeout, in milliseconds.",
        example="120000",
    ),
    CcEnvVar(
        name="CLAUDE_CODE_MAX_RETRIES",
        category="Behavior",
        description="Maximum retry attempts for a failed API call.",
        example="2",
    ),
    CcEnvVar(
        name="CLAUDE_CODE_MAX_CONTEXT_TOKENS",
        category="Behavior",
        description=(
            "Override the assumed context window. Honored when "
            "DISABLE_COMPACT is also set; useful when ANTHROPIC_BASE_URL "
            "routes to a model with a different context size."
        ),
        example="200000",
    ),
    CcEnvVar(
        name="CLAUDE_CODE_MAX_OUTPUT_TOKENS",
        category="Behavior",
        description="Cap on output tokens per response.",
        example="8192",
    ),
    CcEnvVar(
        name="MAX_THINKING_TOKENS",
        category="Behavior",
        description=(
            "Fixed thinking-token budget. Set to 0 to disable extended "
            "thinking entirely."
        ),
        example="16000",
    ),
    CcEnvVar(
        name="CLAUDE_CODE_MAX_THINKING_TOKENS",
        category="Behavior",
        description=(
            "Alias for MAX_THINKING_TOKENS honored by some CC builds."
        ),
        example="16000",
    ),
    CcEnvVar(
        name="DISABLE_COMPACT",
        category="Behavior",
        description=(
            "Disable automatic conversation compaction. Set to '1' to "
            "enable."
        ),
        example="1",
    ),
    CcEnvVar(
        name="CLAUDE_AUTOCOMPACT_PCT_OVERRIDE",
        category="Behavior",
        description=(
            "Override the context-fill percentage that triggers auto "
            "compaction."
        ),
        example="50",
    ),
    CcEnvVar(
        name="DISABLE_TELEMETRY",
        category="Behavior",
        description=(
            "Opt out of Claude Code's anonymous telemetry. Set to '1' "
            "to disable."
        ),
        example="1",
    ),
    CcEnvVar(
        name="DISABLE_ERROR_REPORTING",
        category="Behavior",
        description=(
            "Skip Sentry-style error reports back to Anthropic. Set to "
            "'1' to disable."
        ),
        example="1",
    ),
    CcEnvVar(
        name="DISABLE_BUG_COMMAND",
        category="Behavior",
        description=(
            "Disable the /bug (and /feedback) slash commands. Set to "
            "'1' to disable."
        ),
        example="1",
    ),
    CcEnvVar(
        name="DISABLE_COST_WARNINGS",
        category="Behavior",
        description="Suppress cost-warning messages. Set to '1'.",
        example="1",
    ),
    CcEnvVar(
        name="DISABLE_AUTOUPDATER",
        category="Behavior",
        description=(
            "Skip the background auto-updater. Set to '1' to disable."
        ),
        example="1",
    ),
    CcEnvVar(
        name="FORCE_AUTOUPDATE_PLUGINS",
        category="Behavior",
        description=(
            "When combined with DISABLE_AUTOUPDATER=1, keeps plugin "
            "auto-updates running. Set to '1'."
        ),
        example="1",
    ),
    CcEnvVar(
        name="CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        category="Behavior",
        description=(
            "Suppress non-essential outbound traffic (telemetry, update "
            "checks, version pings). Set to '1'."
        ),
        example="1",
    ),
    CcEnvVar(
        name="CLAUDE_CODE_DISABLE_BACKGROUND_TASKS",
        category="Behavior",
        description="Disable background task execution. Set to '1'.",
        example="1",
    ),
    CcEnvVar(
        name="CLAUDE_CODE_SKIP_PROMPT_HISTORY",
        category="Behavior",
        description=(
            "Do not record prompt history to disk. Set to '1' to enable."
        ),
        example="1",
    ),
    CcEnvVar(
        name="CLAUDE_CODE_IDE_SKIP_AUTO_INSTALL",
        category="Behavior",
        description=(
            "Skip automatic install of IDE extensions. Set to 'true'."
        ),
        example="true",
    ),
    CcEnvVar(
        name="CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL",
        category="Behavior",
        description=(
            "Skip auto-adding the official plugin marketplace on first "
            "run. Set to '1'."
        ),
        example="1",
    ),
    # --- Bash tool --------------------------------------------------------
    CcEnvVar(
        name="BASH_DEFAULT_TIMEOUT_MS",
        category="Bash tool",
        description=(
            "Default timeout for long-running Bash tool commands, in "
            "milliseconds. Default 120000 (2 minutes)."
        ),
        example="120000",
    ),
    CcEnvVar(
        name="BASH_MAX_TIMEOUT_MS",
        category="Bash tool",
        description=(
            "Maximum timeout the model may set on a Bash command, in "
            "milliseconds. Default 600000 (10 minutes)."
        ),
        example="600000",
    ),
    CcEnvVar(
        name="BASH_MAX_OUTPUT_LENGTH",
        category="Bash tool",
        description=(
            "Maximum characters of Bash output returned to the model "
            "before truncation."
        ),
        example="30000",
    ),
    CcEnvVar(
        name="CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR",
        category="Bash tool",
        description=(
            "Reset the Bash working directory to the project root for "
            "every command. Set to '1'."
        ),
        example="1",
    ),
    # --- MCP --------------------------------------------------------------
    CcEnvVar(
        name="MCP_TIMEOUT",
        category="MCP",
        description="Connect timeout for MCP servers, in milliseconds.",
        example="10000",
    ),
    CcEnvVar(
        name="MCP_TOOL_TIMEOUT",
        category="MCP",
        description="Per-tool-call timeout for MCP, in milliseconds.",
        example="30000",
    ),
    CcEnvVar(
        name="MAX_MCP_OUTPUT_TOKENS",
        category="MCP",
        description=(
            "Maximum tokens of MCP tool output returned to the model. "
            "Default 25000."
        ),
        example="50000",
    ),
    # --- Prompt cache -----------------------------------------------------
    CcEnvVar(
        name="DISABLE_PROMPT_CACHING",
        category="Prompt cache",
        description="Disable prompt caching. Set to '1' to disable.",
        example="1",
    ),
    CcEnvVar(
        name="ENABLE_PROMPT_CACHING_1H",
        category="Prompt cache",
        description=(
            "Request a 1-hour cache TTL instead of the 5-minute "
            "default. Set to '1'."
        ),
        example="1",
    ),
    # --- Debug / logging --------------------------------------------------
    CcEnvVar(
        name="ANTHROPIC_LOG",
        category="Debug",
        description=(
            "Log level for the Anthropic SDK transport. Common values: "
            "'debug', 'info', 'warn', 'error'."
        ),
        example="debug",
    ),
    CcEnvVar(
        name="CLAUDE_CODE_DEBUG",
        category="Debug",
        description="Enable Claude Code debug logging. Set to '1'.",
        example="1",
    ),
    CcEnvVar(
        name="CLAUDE_CODE_DEBUG_LOG_LEVEL",
        category="Debug",
        description=(
            "Debug log verbosity. Common values: 'verbose', 'debug', "
            "'info'."
        ),
        example="verbose",
    ),
    # --- Amazon Bedrock ---------------------------------------------------
    CcEnvVar(
        name="CLAUDE_CODE_USE_BEDROCK",
        category="Amazon Bedrock",
        description=(
            "Route through Amazon Bedrock instead of Anthropic. Set "
            "to '1'."
        ),
        example="1",
    ),
    CcEnvVar(
        name="AWS_REGION",
        category="Amazon Bedrock",
        description=(
            "AWS region for Bedrock. Required when CLAUDE_CODE_USE_"
            "BEDROCK=1; not read from ~/.aws/config."
        ),
        example="us-east-1",
    ),
    CcEnvVar(
        name="ANTHROPIC_BEDROCK_BASE_URL",
        category="Amazon Bedrock",
        description="Custom Bedrock endpoint URL.",
        example="https://bedrock-runtime.us-east-1.amazonaws.com",
    ),
    CcEnvVar(
        name="ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION",
        category="Amazon Bedrock",
        description="Override the AWS region used for the small/fast model.",
        example="us-west-2",
    ),
    CcEnvVar(
        name="CLAUDE_CODE_SKIP_BEDROCK_AUTH",
        category="Amazon Bedrock",
        description=(
            "Skip AWS SigV4 signing when a gateway handles auth. Set "
            "to '1'."
        ),
        example="1",
    ),
    # --- Google Vertex AI -------------------------------------------------
    CcEnvVar(
        name="CLAUDE_CODE_USE_VERTEX",
        category="Google Vertex AI",
        description=(
            "Route through Google Vertex AI instead of Anthropic. Set "
            "to '1'."
        ),
        example="1",
    ),
    CcEnvVar(
        name="ANTHROPIC_VERTEX_PROJECT_ID",
        category="Google Vertex AI",
        description=(
            "GCP project ID for Vertex (when CLAUDE_CODE_USE_VERTEX=1)."
        ),
        example="my-gcp-project",
    ),
    CcEnvVar(
        name="CLOUD_ML_REGION",
        category="Google Vertex AI",
        description="Vertex region. Use 'global' for global endpoints.",
        example="global",
    ),
    CcEnvVar(
        name="ANTHROPIC_VERTEX_BASE_URL",
        category="Google Vertex AI",
        description="Custom Vertex endpoint URL.",
        example="https://aiplatform.googleapis.com",
    ),
    CcEnvVar(
        name="CLAUDE_CODE_SKIP_VERTEX_AUTH",
        category="Google Vertex AI",
        description=(
            "Skip GCP auth when a gateway handles it. Set to '1'."
        ),
        example="1",
    ),
)


_BY_NAME: dict[str, CcEnvVar] = {v.name: v for v in CC_ENV_CATALOG}


def lookup(name: str) -> CcEnvVar | None:
    """Return the catalog entry for ``name`` if known, else None."""
    return _BY_NAME.get(name)


def categories() -> list[tuple[str, list[CcEnvVar]]]:
    """Return [(category, [vars])] in catalog order. Used by the editor."""
    out: list[tuple[str, list[CcEnvVar]]] = []
    seen: dict[str, list[CcEnvVar]] = {}
    for v in CC_ENV_CATALOG:
        if v.category not in seen:
            seen[v.category] = []
            out.append((v.category, seen[v.category]))
        seen[v.category].append(v)
    return out
