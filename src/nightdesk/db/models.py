from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
import uuid

from sqlalchemy import (
    Column, Index, String, Integer, Boolean, DateTime, ForeignKey, JSON, Text, Time,
    Table, UniqueConstraint,
)
from sqlalchemy import Float as sa_Float
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Many-to-many: tickets <-> labels
# ---------------------------------------------------------------------------
ticket_labels = Table(
    "ticket_labels",
    Base.metadata,
    Column("ticket_id", ForeignKey("tickets.id", ondelete="CASCADE"), primary_key=True),
    Column("label_id", ForeignKey("labels.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_ticket_labels_label_id", "label_id"),
)


class Provider(Base):
    """A vendor identity: the pricing anchor plus the set of endpoints it owns.

    Slim by design — see ``docs/design/providers-and-endpoints.md``. Credentials,
    protocol, and model menus live on :class:`ProviderEndpoint`, not here, because
    one vendor's surfaces can authenticate differently (Anthropic: API key vs
    subscription file; OpenAI: API key vs Codex OAuth).
    """

    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    # Canonical pricing key: zai, openrouter, anthropic, openai, ollama, custom.
    # Distinct from `name` (the user's label) and from any endpoint's protocol.
    vendor: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    endpoints: Mapped[list["ProviderEndpoint"]] = relationship(
        back_populates="provider", cascade="all, delete-orphan",
    )


class ProviderEndpoint(Base):
    """One API surface a :class:`Provider` exposes.

    Deliberately no ``UNIQUE(provider_id, protocol_kind)`` — real vendors expose
    two surfaces on one protocol (ZAI's coding-plan vs pay-as-you-go base URLs).
    ``credential`` and ``extra`` are Fernet-encrypted (same scheme as
    ``Profile.claude_credentials``); ``extra`` may carry secret routing headers.
    """

    __tablename__ = "provider_endpoints"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("providers.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    label: Mapped[str] = mapped_column(String, default="", nullable=False)
    # anthropic | anthropic_compat | openai | openai_compat | openai_codex |
    # openrouter | ollama
    protocol_kind: Mapped[str] = mapped_column(String, nullable=False)
    # Null where the protocol implies it (openai_codex).
    base_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # api_key | oauth_file | subscription_file | env_var | none
    credential_source: Mapped[str] = mapped_column(String, default="api_key", nullable=False)
    # Encrypted: the secret itself (api_key), or a reference to it (an env var
    # name for env_var, a filesystem path for oauth_file/subscription_file).
    credential: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Harness code exclusively allowed to use this endpoint (e.g. a Claude
    # subscription endpoint locks to "claude_sdk" per Anthropic's terms), or None.
    harness_lock: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    default_model: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    models: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    models_pulled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Encrypted JSON: vendor-quirk overrides (headers, env, options), interpreted
    # per protocol by the renderer. May contain secrets (routing tokens).
    extra: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    provider: Mapped["Provider"] = relationship(back_populates="endpoints")


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, unique=True)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    fs_read: Mapped[list] = mapped_column(JSON, default=list)
    fs_write: Mapped[list] = mapped_column(JSON, default=list)
    allowed_tools: Mapped[list] = mapped_column(JSON, default=list)
    denied_tools: Mapped[list] = mapped_column(JSON, default=list)
    # v1 narrows network_mode to "off" | "on". Legacy values are migrated by 0010.
    network_mode: Mapped[str] = mapped_column(String, default="off")
    network_allowlist: Mapped[list] = mapped_column(JSON, default=list)
    secret_keys: Mapped[list] = mapped_column(JSON, default=list)
    default_model: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    backend: Mapped[str] = mapped_column(String, default="claude_sdk", nullable=False)
    # Where runs of this profile execute: "local" (on-host bwrap sandbox,
    # default) or "k8s" (per-run pod). The executor is orthogonal to backend;
    # see docs/design/session-suite/k8s-executor.md.
    execution_target: Mapped[str] = mapped_column(
        String, default="local", nullable=False,
    )
    # Encrypted JSON blob: {"source": "inherit"|"api_key"|"auth_token", "value": "..."}.
    claude_credentials: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    claude_binary_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Encrypted JSON kv map of custom env vars to inject into the sandbox.
    env: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    system_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    permission_mode: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # CC settings.json subset (theme, cleanupPeriodDays, additionalDirectories, etc.).
    cc_settings_passthrough: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # Run-token scopes this profile grants beyond the self-scope defaults.
    run_token_scopes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # Optional primary endpoint reference. When set the worker uses the
    # resolved endpoint in preference to the legacy claude_credentials blob.
    endpoint_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("provider_endpoints.id"), nullable=True,
    )
    # Backend-specific extra configuration forwarded to the launch context.
    backend_config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Label(Base):
    """A named, colored tag that can be attached to tickets."""
    __tablename__ = "labels"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    color: Mapped[str] = mapped_column(String, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    tickets: Mapped[list["Ticket"]] = relationship(
        secondary=ticket_labels, back_populates="labels",
    )


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    source_path: Mapped[str] = mapped_column(String, nullable=False)
    default_workspace_mode: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    default_worktree_name_template: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    default_base_ref: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    default_linked_workspaces: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    default_toolchains: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    default_tool_paths: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    color: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    archived_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    tickets: Mapped[list["Ticket"]] = relationship(back_populates="project")




class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String)
    prompt: Mapped[str] = mapped_column(Text, default="")
    # Human-facing what/why for scanning the board/review/ack digest. Distinct
    # from ``prompt`` (the agent instructions that actually run); ``description``
    # is metadata only and is NEVER injected into the agent's context.
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, default="draft", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0, index=True)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    project_id: Mapped[Optional[str]] = mapped_column(ForeignKey("projects.id"), nullable=True, index=True)
    profile_id: Mapped[Optional[str]] = mapped_column(ForeignKey("profiles.id"), nullable=True)
    permission_overrides: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    toolchain_overrides: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    additional_dirs: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    # 'ticket' (board work) | 'session' (ad-hoc chat, hidden from board/inbox/
    # analytics/search). Sessions reuse the entire run pipeline; only the ticket
    # list surfaces filter them out.
    kind: Mapped[str] = mapped_column(String, default="ticket", nullable=False, index=True)
    run_now: Mapped[bool] = mapped_column(Boolean, default=False)
    # Opt-in: on a successful run, commit this ticket's working-tree changes
    # onto its git_worktree branch so dependent (stacked) tickets whose base_ref
    # points at that branch actually receive this ticket's work. Without this,
    # runs leave work uncommitted and the branch ref never advances, so a
    # dependent provisions from the stale base commit. See the run-completion
    # path in worker/run_one.py and the stacking docs in nightdesk-ticket-ops.
    commit_on_finish: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    scheduled_after: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    current_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("runs.id"), nullable=True)
    # The active conversation (1 ticket : N conversations, ordered). Exactly one
    # is active at a time; selecting/continuing an older conversation re-points
    # this at it. Nullable for tickets that have never run.
    current_conversation_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("conversations.id", use_alter=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    # Post-review acknowledgement (docs/design/post-review-acknowledge-flow.md).
    # "A human has seen this ticket's current outcome." Set implicitly by
    # human-initiated transitions and the explicit ack endpoints; never by
    # run/agent transitions; cleared when the ticket re-enters an active state.
    # NULL on an archived/review ticket == unacknowledged == shows in the digest.
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    next_run_context: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    next_run_context_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped[Optional["Project"]] = relationship(back_populates="tickets")
    profile: Mapped[Optional["Profile"]] = relationship(lazy="joined")
    runs: Mapped[list["Run"]] = relationship(back_populates="ticket", foreign_keys="Run.ticket_id")
    conversations: Mapped[list["Conversation"]] = relationship(
        back_populates="ticket",
        foreign_keys="Conversation.ticket_id",
        cascade="all, delete-orphan",
        order_by="Conversation.position",
    )
    current_conversation: Mapped[Optional["Conversation"]] = relationship(
        foreign_keys=[current_conversation_id], post_update=True,
    )
    workspaces: Mapped[list["TicketWorkspace"]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="TicketWorkspace.position",
    )
    # Dependencies: tickets this ticket must wait for.
    dependencies: Mapped[list["TicketDependency"]] = relationship(
        foreign_keys="TicketDependency.ticket_id",
        back_populates="ticket",
        cascade="all, delete-orphan",
    )
    # Dependents: tickets that wait for this one.
    dependents: Mapped[list["TicketDependency"]] = relationship(
        foreign_keys="TicketDependency.depends_on_id",
        back_populates="depends_on",
        cascade="all, delete-orphan",
    )
    labels: Mapped[list["Label"]] = relationship(
        secondary=ticket_labels, back_populates="tickets", lazy="selectin",
    )


class TicketEvent(Base):
    """Append-only record of one ticket status transition with its actor.

    Layer 0 of the post-review acknowledgement flow
    (``docs/design/post-review-acknowledge-flow.md``). Written at the single
    choke point every status change funnels through
    (``domain.tickets.transition_with_position``), so it is a complete transition
    audit trail — including the worker's own queued->running and running->review
    moves.

    ``actor_kind`` generalizes ``DiffComment.author_kind``:

    - ``admin`` — the human (admin bearer/cookie), or any internal default caller.
    - ``run`` — a ticket run / the worker acting on the run's behalf
      (``run_id`` set). These NEVER acknowledge.
    - ``token`` — reserved for the sibling scoped-agent-token design; not written
      on this branch. ``domain.events.actor_from_principal`` is the seam that
      maps a future ``TokenPrincipal`` onto this kind.

    Rows are never mutated; ``ticket_id`` cascades on ticket delete.
    """

    __tablename__ = "ticket_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    # NULL only in the theoretical create-as-terminal case; transitions carry it.
    from_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    to_status: Mapped[str] = mapped_column(String, nullable=False)
    # admin | run | token
    actor_kind: Mapped[str] = mapped_column(String, nullable=False)
    run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("runs.id"), nullable=True)
    session_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, nullable=False,
    )

    __table_args__ = (
        Index(
            "ix_ticket_events_ticket_to_status",
            "ticket_id", "to_status", "created_at",
        ),
    )


class TicketDependency(Base):
    """A directed edge: the ticket owning this row depends on (must wait for)
    ``depends_on_id``.  A dependency is satisfied when the upstream ticket's
    most-recent run has ``exit_status='success'`` AND the upstream is in
    ``review`` or ``archived``."""

    __tablename__ = "ticket_dependencies"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    depends_on_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    ticket: Mapped["Ticket"] = relationship(
        foreign_keys=[ticket_id],
        back_populates="dependencies",
    )
    depends_on: Mapped["Ticket"] = relationship(
        foreign_keys=[depends_on_id],
        back_populates="dependents",
    )


class Conversation(Base):
    """A conversation: the primary unit of work.

    A Ticket owns many Conversations (1:N, ordered by ``position``); exactly
    one is active (``ticket.current_conversation_id``). A Conversation owns
    many Turns (the existing ``Run`` rows, kept in place) and is bound to one
    runtime via ``profile_id`` plus a ``backend`` string snapshot that survives
    profile renames and acts as the runtime lock.

    The authoritative resume handle (``session_id``) lives HERE, lifted off the
    per-turn Run. A Run keeps the ``session_id`` it observed for traceability.
    Cost/token totals are CUMULATIVE and equal the LAST turn's reported totals
    (the SDK reports cumulative-since-process-start tokens, which after a resume
    include the replayed prefix — summing per-turn would double-count cache-read).
    One transcript file per conversation (``transcript_path``) with a single
    monotonic seq space, appended per turn.
    """

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    profile_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("profiles.id"), nullable=True,
    )
    # Backend string snapshot frozen at creation (e.g. claude_sdk, opencode).
    # Survives profile renames and is the runtime lock: the active
    # conversation's runtime cannot be changed by reassigning the ticket's
    # profile (that only affects the NEXT new conversation).
    backend: Mapped[str] = mapped_column(String, default="claude_sdk", nullable=False)
    # The authoritative resume handle, persisted eagerly on the first
    # session-bearing event. Null means the conversation is not resumable.
    session_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="active", nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # One transcript file per conversation (shared by every turn in it).
    transcript_path: Mapped[str] = mapped_column(String, nullable=False)
    # Cumulative totals == the LAST turn's reported totals (NOT a sum).
    cost_usd: Mapped[Optional[float]] = mapped_column(sa_Float, nullable=True)
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    model_used: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    ticket: Mapped["Ticket"] = relationship(
        back_populates="conversations", foreign_keys=[ticket_id],
    )
    profile: Mapped[Optional["Profile"]] = relationship(lazy="joined")
    runs: Mapped[list["Run"]] = relationship(
        back_populates="conversation",
        foreign_keys="Run.conversation_id",
        order_by="Run.position",
    )
    workspaces: Mapped[list["TicketWorkspace"]] = relationship(back_populates="conversation")


class SteerMessage(Base):
    """A follow-up the user queues while a run is live (mid-run steering).

    Belongs to a Conversation (the live unit of work), NOT a specific Run: a
    message authored during run N is delivered into run N (inject-capable
    backends) or run N+1 (queue-only backends, via the run-completion drain +
    auto-continue). Conversation is the same anchor the transcript file,
    session_id, and ``latest_turn()`` already use, so a message survives run N
    ending and can drive run N+1.

    State machine: ``pending -> delivering -> delivered`` (happy path) and
    ``pending -> cancelled`` (deleted, or folded into next_run_context at the
    run-completion drain). ``delivering`` is a short-lived claim state; a
    ``delivering`` row on a finished/orphaned run is reset to ``pending`` by
    orphan recovery.
    """

    __tablename__ = "steer_messages"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # pending -> delivering -> delivered ; or pending -> cancelled
    state: Mapped[str] = mapped_column(String, default="pending", nullable=False, index=True)
    # "at_turn" (default) or "inject"; inject downgrades to at_turn without STEER_INJECT.
    delivery_mode: Mapped[str] = mapped_column(String, default="at_turn", nullable=False)
    delivered_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("runs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    conversation: Mapped["Conversation"] = relationship()


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"))
    # The conversation this turn belongs to. Nullable only for rows created
    # before this column existed; backfilled by the 0019 migration. Every run
    # created through ``start_run`` gets one.
    conversation_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("conversations.id"), nullable=True, index=True,
    )
    # Order within the conversation (0-based).
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    error_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    worktree_path: Mapped[str] = mapped_column(String)
    transcript_path: Mapped[str] = mapped_column(String)
    pid: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    host: Mapped[str] = mapped_column(String)
    started_as_run_now: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    intent: Mapped[str] = mapped_column(String, default="first_run", nullable=False)
    parent_run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("runs.id"), nullable=True)
    headless_policy_version: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    restart_workspace_policy: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    failure_kind: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cache_read_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cache_write_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[Optional[float]] = mapped_column(sa_Float, nullable=True)
    model_used: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # The base prompt (ticket.prompt at launch time), stored per-run so the
    # transcript panel can show what each run actually used. NULL for runs
    # created before this column existed; display falls back to ticket.prompt.
    prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Claude session id reported by the SDK at run completion. Enables resuming
    # the conversation (`claude --resume <session_id>` / SDK resume=).
    session_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sandbox_tool_paths: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # Which backend produced this run, and against which endpoint. NULL on
    # rows created before multi-backend support; display falls back to the
    # claude_sdk defaults.
    backend: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    endpoint_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("provider_endpoints.id"), nullable=True,
    )
    # Backend-shaped resume handle (e.g. {"session_id": ...} for claude,
    # {"session_id": ..., "data_dir": ...} for opencode). Opaque to the worker.
    session_ref: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # Prices in effect when this run launched, keyed by model id. Computed
    # once at run finish; never re-derived from current prices. See
    # docs/design/providers-and-endpoints.md#pricing-integration.
    pricing_snapshot: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    ticket: Mapped["Ticket"] = relationship(back_populates="runs", foreign_keys=[ticket_id])
    conversation: Mapped[Optional["Conversation"]] = relationship(
        back_populates="runs", foreign_keys=[conversation_id],
    )


class RunLatency(Base):
    """Cached per-run latency summary derived from the transcript.

    Populated once when a run finishes (see
    ``domain.latency.populate_run_latency``) and never rescanned — transcripts
    are terminal, so a cached row is final. The analytics dashboard aggregates
    these rows instead of reading transcript files on every load.

    ``turn_latencies`` keeps the raw per-turn seconds (JSON array) so
    percentiles/medians can be merged across runs in Python without rescanning.
    ``total_model_seconds`` / ``total_tool_seconds`` are the per-run sums
    (model inference time vs tool execution time); ``ttft_seconds`` is the
    run's first-token latency. See ``domain.latency`` for the accuracy caveat:
    these are ingest-receipt deltas, trend-useful but not ms-precise API TTFT.
    """

    __tablename__ = "run_latency"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True,
    )
    model: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    total_model_seconds: Mapped[float] = mapped_column(sa_Float, nullable=False, default=0.0)
    total_tool_seconds: Mapped[float] = mapped_column(sa_Float, nullable=False, default=0.0)
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ttft_seconds: Mapped[Optional[float]] = mapped_column(sa_Float, nullable=True)
    turn_latencies: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    run: Mapped["Run"] = relationship(foreign_keys=[run_id])


class DiffComment(Base):
    """A review comment anchored to a line of a run's diff, or a reply to one.

    One table, one-level threading via a nullable self-FK ``parent_id``. A
    *root* (``parent_id IS NULL``) carries the anchor (``file_path``/``side``/
    ``line``/``anchor_head_sha``/``anchor_text``) plus resolution and delivery
    state; a *reply* carries only ``body`` + author and points at its root.

    Anchors are NOT a pinned snapshot. The run-diff endpoint recomputes from
    git live (``run_start_sha..HEAD``) on every request, so a later run on the
    same worktree shifts line numbers. Each root stores ``anchor_head_sha`` (the
    diff's ``head_sha`` when the comment was filed) and ``anchor_text`` (the
    line's text); a root is *outdated* when the live diff's head differs — it
    renders against ``anchor_text`` instead of being silently mis-placed.
    """

    __tablename__ = "diff_comments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    # Denormalized for cheap ticket-rail listing + next_run_context targeting,
    # mirroring runs/workspaces carrying ticket_id/conversation_id.
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"), index=True)
    conversation_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("conversations.id"), nullable=True, index=True,
    )
    # Threading: NULL = root (carries the anchor); else points at the root.
    parent_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("diff_comments.id", ondelete="CASCADE"), nullable=True, index=True,
    )
    # Anchor (root only; NULL on replies).
    file_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    side: Mapped[Optional[str]] = mapped_column(String, nullable=True)   # 'old' | 'new'
    line: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 1-based, on `side`
    anchor_head_sha: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    anchor_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Content.
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Acting principal (an agent may file via API). 'admin' | 'agent'.
    author_kind: Mapped[str] = mapped_column(String, default="admin", nullable=False)
    author_run_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("runs.id"), nullable=True,
    )  # the run token's run, when agent-authored
    # Resolution + delivery (root only).
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=True,
    )

    run: Mapped["Run"] = relationship(foreign_keys=[run_id])
    replies: Mapped[list["DiffComment"]] = relationship(
        back_populates="parent",
        cascade="all, delete-orphan",
        foreign_keys=[parent_id],
        order_by="DiffComment.created_at",
    )
    parent: Mapped[Optional["DiffComment"]] = relationship(
        back_populates="replies", remote_side=[id], foreign_keys=[parent_id],
    )


class TicketWorkspace(Base):
    __tablename__ = "ticket_workspaces"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"), index=True)
    run_id: Mapped[Optional[str]] = mapped_column(ForeignKey("runs.id"), nullable=True, index=True)
    # A Conversation owns its workspaces (1:N), not the ticket. Required so
    # continuing an OLD conversation restores the tree it ran against, not one
    # a newer conversation mutated.
    conversation_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("conversations.id"), nullable=True, index=True,
    )
    role: Mapped[str] = mapped_column(String, default="linked", nullable=False)
    label: Mapped[str] = mapped_column(String, default="", nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    access: Mapped[str] = mapped_column(String, default="read_write", nullable=False)
    source_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    resolved_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    repo_root: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    git_common_dir: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    relative_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    worktree_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    worktree_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    branch: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    base_ref: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    base_sha: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    head_sha: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Worktree HEAD captured at the moment this run started. The run diff is
    # computed from here to the current end state so it reflects only what the
    # run changed, not the whole branch versus its target.
    run_start_sha: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    retention: Mapped[str] = mapped_column(String, default="preserve", nullable=False)
    state: Mapped[str] = mapped_column(String, default="pending", nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    ticket: Mapped["Ticket"] = relationship(back_populates="workspaces")
    conversation: Mapped[Optional["Conversation"]] = relationship(back_populates="workspaces")

class ConfigRow(Base):
    __tablename__ = "config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    window_start: Mapped[str] = mapped_column(String, default="22:00")
    window_end: Mapped[str] = mapped_column(String, default="07:00")
    max_parallel: Mapped[int] = mapped_column(Integer, default=2)
    worktree_root: Mapped[str] = mapped_column(String)
    transcript_root: Mapped[str] = mapped_column(String)
    # Global default base ref for git_worktree tickets. When set, new tickets
    # created without an explicit base_ref branch from this ref instead of HEAD.
    worktree_base_ref: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    polling_interval_seconds: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    max_run_duration_seconds: Mapped[int] = mapped_column(Integer, default=86400, nullable=False)
    run_token_grace_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    claude_binary_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Empty/NULL means "auto-discover" (PATH, then ~/.opencode/bin).
    opencode_binary_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    session_cookie_ttl_seconds: Mapped[int] = mapped_column(
        Integer, default=60 * 60 * 24 * 30, nullable=False,
    )
    cc_minimum_version: Mapped[str] = mapped_column(String, default="2.1.80", nullable=False)
    # Webhook URL for run-completion notifications (Slack/Discord/ntfy etc.).
    # Best-effort POST on run -> review transition; empty/NULL means disabled.
    notify_webhook_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Global IANA timezone for evaluating ScheduleWindow rows. Window start/end
    # are wall-clock times in this zone and day_mask bits are local weekdays;
    # the scheduler converts ``now`` into this zone before matching. Default
    # "UTC" reproduces the pre-timezone-fix behavior.
    schedule_timezone: Mapped[str] = mapped_column(String, default="UTC", nullable=False)
    toolchain_presets: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # --- Cloud sandbox (Kubernetes executor) ---------------------------------
    # All optional; k8s runs are only possible once a runner image + a
    # cluster-routable API address are configured (see domain/k8s_config.py).
    # kubeconfig path for out-of-cluster access; empty when running in-cluster.
    k8s_kubeconfig_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    k8s_in_cluster: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    k8s_namespace: Mapped[str] = mapped_column(String, default="nightdesk", nullable=False)
    k8s_runner_image: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    k8s_cpu_request: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    k8s_cpu_limit: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    k8s_mem_request: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    k8s_mem_limit: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    k8s_node_selector: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    k8s_runtime_class: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Name of a pre-existing cluster Secret providing clone/push git auth
    # (HTTPS token or deploy key), mounted into the runner pod.
    k8s_git_credentials_secret: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class ScheduleWindow(Base):
    """A named time window with its own parallelism cap.

    Replaces the single ``window_start``/``window_end``/``max_parallel`` triple
    on ConfigRow with a list of windows. When several windows match the current
    time/day the scheduler uses the cap of the highest-precedence one — the
    first in ``position`` order (lowest ``position`` = dragged to the top in the
    Settings editor). ``position`` is therefore the overlap tie-breaker.

    ``day_mask`` is a bitmask: Mon=1 Tue=2 Wed=4 Thu=8 Fri=16 Sat=32 Sun=64.
    Bit ``i`` corresponds to ``1 << datetime.weekday()`` (Mon=0..Sun=6). The
    default 127 (0b1111111) is every day.

    ``start``/``end`` are HH:MM strings with the same wraparound semantics as
    ``scheduler.in_window`` (equal start/end means always on; start > end wraps
    past midnight).
    """

    __tablename__ = "schedule_windows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String, default="", nullable=False)
    day_mask: Mapped[int] = mapped_column(Integer, default=127, nullable=False)
    start: Mapped[str] = mapped_column(String, default="00:00", nullable=False)
    end: Mapped[str] = mapped_column(String, default="00:00", nullable=False)
    max_parallel: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class RunToken(Base):
    __tablename__ = "run_tokens"

    token_hash: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False, index=True)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("tickets.id"), nullable=False, index=True)
    scopes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    scope_data: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class DaemonStatus(Base):
    __tablename__ = "daemon_status"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cc_binary_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    cc_version: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    cc_check_status: Mapped[str] = mapped_column(String, default="unknown", nullable=False)
    cc_check_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host: Mapped[str] = mapped_column(String)
    pid: Mapped[int] = mapped_column(Integer)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CronJob(Base):
    """A recurring ticket template plus a schedule.

    When the schedule fires the worker materializes one ordinary
    ``status="queued"`` ticket from this template; that ticket then flows
    through the normal scheduler (active-hours window and ``max_parallel``
    capacity). Set ``force_run=True`` to instead materialize ``run_now=True``
    tickets, which the scheduler dispatches unconditionally — past a full queue
    and outside the active-hours window (overflow above ``max_parallel``).

    Backend neutrality: the agent backend is chosen by the referenced
    ``Profile.backend``, never by the cron job. This template stores only
    ``profile_id`` and no engine field — a profile pointed at a non-Claude
    backend produces tickets that run on that backend with zero cron changes.

    Workspace is directory-only in v1: ``workspace_mode`` is constrained to
    ``directory``/``in_place`` and generated tickets run directly in the primary
    worktree.
    """

    __tablename__ = "cron_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    # --- ticket template ---
    title: Mapped[str] = mapped_column(String, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    profile_id: Mapped[str] = mapped_column(ForeignKey("profiles.id"), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_path: Mapped[str] = mapped_column(String, nullable=False)
    # Constrained to 'directory' | 'in_place' on the API surface (no worktrees).
    workspace_mode: Mapped[str] = mapped_column(String, default="directory", nullable=False)
    additional_dirs: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    permission_overrides: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # When true, generated tickets are created with run_now=True so the
    # scheduler dispatches them unconditionally (ignores active-hours window
    # and max_parallel capacity). Pairs with overlap_policy to avoid stacking.
    force_run: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # --- schedule ---
    # Standard 5-field cron expression: "minute hour dom month dow".
    schedule: Mapped[str] = mapped_column(String, nullable=False)
    # IANA timezone name (zoneinfo). Next-fire is computed in this tz; all DB
    # datetimes are stored in UTC.
    timezone: Mapped[str] = mapped_column(String, default="UTC", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # 'coalesce' (default): one ticket for the oldest due fire, then advance past
    # now. No backfill in v1.
    misfire_policy: Mapped[str] = mapped_column(String, default="coalesce", nullable=False)
    # 'skip_if_active' (default): skip a new ticket if the previous generated
    # ticket is draft/queued/running. 'review' does not block.
    overlap_policy: Mapped[str] = mapped_column(String, default="skip_if_active", nullable=False)
    # Nullable while disabled (stale past value is harmless; the materialization
    # query filters enabled). UTC.
    next_fire_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_fire_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ticket_id: Mapped[Optional[str]] = mapped_column(ForeignKey("tickets.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    fires: Mapped[list["CronJobFire"]] = relationship(
        back_populates="cron_job", cascade="all, delete-orphan",
    )


class CronJobFire(Base):
    """Idempotency + audit row for a single (cron_job, fire_at) materialization.

    The unique constraint on ``(cron_job_id, fire_at)`` makes materialization
    idempotent: a second tick that tries to claim the same fire hits the
    constraint and skips. ``ticket_id`` is NULL when the fire was skipped
    (e.g. overlap), with the reason recorded in ``skipped_reason`` so a
    suppressed run is auditable rather than a silent gap.
    """

    __tablename__ = "cron_job_fires"
    __table_args__ = (
        UniqueConstraint("cron_job_id", "fire_at", name="uq_cron_job_fires_job_fire"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    cron_job_id: Mapped[str] = mapped_column(ForeignKey("cron_jobs.id"), nullable=False, index=True)
    # The scheduled fire instant (UTC). Minute-granular for scheduled fires,
    # sub-minute for manual fire-now.
    fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ticket_id: Mapped[Optional[str]] = mapped_column(ForeignKey("tickets.id"), nullable=True)
    # NULL when a ticket was generated; set (e.g. "overlap") when the fire was
    # recorded but suppressed.
    skipped_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    cron_job: Mapped["CronJob"] = relationship(back_populates="fires")


class SavedView(Base):
    """A named, bookmarkable slice of the board or list surface.

    Stores the URL params (q, group, order) for a surface so the user can
    navigate back to a recurring query with one click.  Applying a view is
    pure client-side navigation to the composed URL — no server-side state.
    """

    __tablename__ = "saved_views"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    # "board" | "list"
    surface: Mapped[str] = mapped_column(String, nullable=False)
    # Dict of URL params: q, group, (order for list).  All values are strings.
    params: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
