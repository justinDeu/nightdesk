"""Scope taxonomy, bundles, and human-only policy for API tokens.

This module is the single source of truth for the permission vocabulary shared
by durable ``ndk_`` agent tokens (``domain/api_tokens.py``) and ephemeral
``ndr_`` run tokens (``domain/run_tokens.py``). Enforcement lives in
``api/auth.py`` (``make_scoped``); this module only defines the words.

Design: fine-grained ``resource.action`` scopes are the enforcement primitive;
server-defined *bundles* are the minting UX. A bundle expands to a scope
snapshot at mint time (stored on the token). The snapshot is intentionally not
a live reference — editing a preset must never silently re-grant every holder.

See ``docs/design/agent-token-permissions.md`` §4.
"""
from __future__ import annotations

from typing import Iterable


# ---------------------------------------------------------------------------
# The scope vocabulary. Grouped by router surface (§4.2).
# ---------------------------------------------------------------------------

# tickets + everything that reads/writes through the ticket surface.
TICKETS_READ = "tickets.read"
TICKETS_CREATE = "tickets.create"
TICKETS_UPDATE = "tickets.update"
TICKETS_TRANSITION = "tickets.transition"
TICKETS_RUN = "tickets.run"
TICKETS_ARCHIVE = "tickets.archive"
TICKETS_DELETE = "tickets.delete"

RUNS_READ = "runs.read"

COMMENTS_READ = "comments.read"
COMMENTS_WRITE = "comments.write"

PROJECTS_READ = "projects.read"
PROJECTS_WRITE = "projects.write"

LABELS_WRITE = "labels.write"

PROFILES_READ = "profiles.read"
PROFILES_WRITE = "profiles.write"

PROVIDERS_READ = "providers.read"
PROVIDERS_WRITE = "providers.write"

CONFIG_READ = "config.read"
CONFIG_WRITE = "config.write"

CRON_READ = "cron.read"
CRON_WRITE = "cron.write"

AGENTS_READ = "agents.read"
AGENTS_MESSAGE = "agents.message"
AGENTS_ADMIN = "agents.admin"

ANALYTICS_READ = "analytics.read"

# External integrations (GitLab v1). ``read`` covers issue/MR browse + a ticket's
# external-links; ``link`` covers linking/unlinking + import-as-draft (run tokens
# carry the ``.self`` variant, enforced by ``enforce_self_ticket``); ``write`` is
# connection/repo-link CRUD — human-only, mirroring providers.write, since a
# connection holds a forge credential and an endpoint URL.
INTEGRATIONS_READ = "integrations.read"
INTEGRATIONS_LINK = "integrations.link"
INTEGRATIONS_WRITE = "integrations.write"

FS_READ = "fs.read"

# Never mintable; the mint/revoke/list routes gate on the root bearer directly,
# never on a scope. Present in the vocabulary only so it can be named as
# human-only and rendered (disabled) in the UI.
TOKENS_ADMIN = "tokens.admin"


# Every scope an ``ndk_`` token could conceivably hold (mintable or not). The
# order here drives the grouped checklist in the mint dialog.
ALL_SCOPES: tuple[str, ...] = (
    TICKETS_READ,
    TICKETS_CREATE,
    TICKETS_UPDATE,
    TICKETS_TRANSITION,
    TICKETS_RUN,
    TICKETS_ARCHIVE,
    TICKETS_DELETE,
    RUNS_READ,
    COMMENTS_READ,
    COMMENTS_WRITE,
    PROJECTS_READ,
    PROJECTS_WRITE,
    LABELS_WRITE,
    PROFILES_READ,
    PROFILES_WRITE,
    PROVIDERS_READ,
    PROVIDERS_WRITE,
    CONFIG_READ,
    CONFIG_WRITE,
    CRON_READ,
    CRON_WRITE,
    AGENTS_READ,
    AGENTS_MESSAGE,
    AGENTS_ADMIN,
    ANALYTICS_READ,
    INTEGRATIONS_READ,
    INTEGRATIONS_LINK,
    INTEGRATIONS_WRITE,
    FS_READ,
    TOKENS_ADMIN,
)

_ALL_SCOPES_SET = frozenset(ALL_SCOPES)


# ---------------------------------------------------------------------------
# Human-only scopes (§4.3). The mint endpoint rejects any of these outright, so
# no token can ever hold one — only the admin bearer/cookie passes a route that
# requires one. Each entry closes a concrete escalation path (see the doc).
# ---------------------------------------------------------------------------
HUMAN_ONLY_SCOPES: frozenset[str] = frozenset({
    PROFILES_WRITE,     # profile edit escalates every future run on it
    CRON_WRITE,         # authoring a cron job is persistent arbitrary execution
    CONFIG_WRITE,       # schedule windows / worker knobs / global settings
    PROVIDERS_WRITE,    # credential replacement / endpoint-URL redirection
    AGENTS_MESSAGE,     # prompt injection into a full-power resident agent
    AGENTS_ADMIN,       # editing a resident agent's env / restarting its runtime
    TICKETS_DELETE,     # destructive; archive covers every agent workflow
    INTEGRATIONS_WRITE, # a connection holds a forge credential + endpoint URL
    TOKENS_ADMIN,       # a token that mints tokens is admin
})


# ---------------------------------------------------------------------------
# Self-scoped run-token vocabulary and aliasing (§4.2, §7).
#
# In-flight run tokens were minted with the legacy ``run_tokens`` scope strings.
# Rather than migrate rows, we alias those strings to the new taxonomy at
# resolution time so a run token that was granted "ticket.create" now passes a
# route guarded by "tickets.create". The self-constrained scopes keep their own
# names (they are enforced by ``enforce_self_ticket``, not by ``scoped``).
# ---------------------------------------------------------------------------
RUNS_WRITE_SELF = "runs.write.self"
TICKETS_READ_SELF = "tickets.read.self"
TICKETS_NEXT_RUN_CONTEXT_SELF = "tickets.update.next_run_context.self"

# Legacy run-token scope string -> new taxonomy name. Applied to every run-token
# scope at resolution. Legacy self strings map to their new self names; the
# transcript/diff/result write-back trio all collapse onto RUNS_WRITE_SELF but
# the runs.py write-back still checks the legacy names, so we keep BOTH the old
# and new strings on the resolved principal (union) to avoid breaking it.
_RUN_SCOPE_ALIASES: dict[str, str] = {
    "ticket.create": TICKETS_CREATE,
    "ticket.read.self": TICKETS_READ_SELF,
    "ticket.update.next_run_context": TICKETS_NEXT_RUN_CONTEXT_SELF,
    "run.append_transcript.self": RUNS_WRITE_SELF,
    "run.mark_done.self": RUNS_WRITE_SELF,
    # GitLab v1 run-token grants. ``integrations.read`` is already canonical;
    # the self-constrained link grant aliases onto the durable ``integrations.link``
    # name so a future ``scoped("integrations.link")`` route would resolve it too.
    "integrations.link.self": INTEGRATIONS_LINK,
}


def expand_run_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
    """Return a run token's scopes plus their new-taxonomy aliases.

    Union of the original strings and their aliases so both the legacy
    ``require_scopes(["run.append_transcript.self"])`` write-back path and any
    new ``scoped("tickets.create")`` route resolve the same token.
    """
    out: list[str] = []
    for s in scopes:
        if s not in out:
            out.append(s)
        alias = _RUN_SCOPE_ALIASES.get(s)
        if alias and alias not in out:
            out.append(alias)
    return tuple(out)


# ---------------------------------------------------------------------------
# Bundles (§4.5). Presets are starting points for the mint dialog, expanded to a
# snapshot at mint. ``bundle`` is kept on the token for display only.
# ---------------------------------------------------------------------------

_OBSERVER = (
    TICKETS_READ,
    RUNS_READ,
    COMMENTS_READ,
    PROJECTS_READ,
    PROVIDERS_READ,
    CONFIG_READ,
    CRON_READ,
    AGENTS_READ,
    ANALYTICS_READ,
)

_REVIEWER = _OBSERVER + (COMMENTS_WRITE,)

_PM_AGENT = (
    TICKETS_READ,
    TICKETS_CREATE,
    TICKETS_UPDATE,
    TICKETS_TRANSITION,
    TICKETS_ARCHIVE,
    RUNS_READ,
    COMMENTS_READ,
    COMMENTS_WRITE,
    PROJECTS_READ,
    LABELS_WRITE,
    ANALYTICS_READ,
)

_OPERATOR = _PM_AGENT + (TICKETS_RUN, CRON_READ)


def _dedupe(scopes: Iterable[str]) -> list[str]:
    out: list[str] = []
    for s in scopes:
        if s not in out:
            out.append(s)
    return out


# Public preset -> ordered scope list. Snapshot at mint; never referenced live.
BUNDLES: dict[str, list[str]] = {
    "observer": _dedupe(_OBSERVER),
    "reviewer": _dedupe(_REVIEWER),
    "pm-agent": _dedupe(_PM_AGENT),
    "operator": _dedupe(_OPERATOR),
}


def bundle_scopes(name: str) -> list[str]:
    """Expand a bundle name to its scope snapshot. KeyError if unknown."""
    return list(BUNDLES[name])


def is_known_scope(scope: str) -> bool:
    return scope in _ALL_SCOPES_SET


def unknown_scopes(scopes: Iterable[str]) -> list[str]:
    return [s for s in scopes if s not in _ALL_SCOPES_SET]


def human_only(scopes: Iterable[str]) -> list[str]:
    """Return the subset of ``scopes`` that may never be minted."""
    return [s for s in scopes if s in HUMAN_ONLY_SCOPES]
