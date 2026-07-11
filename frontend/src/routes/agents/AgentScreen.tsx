import { Suspense, lazy, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "@tanstack/react-router";
import {
  ChevronLeft,
  Mail,
  Pencil,
  Power,
  Square,
  SunMedium,
  TerminalSquare,
  XCircle,
} from "lucide-react";
import { Button } from "@/ui/Button";
import { IconButton } from "@/ui/IconButton";
import { Input } from "@/ui/Input";
import { Dialog } from "@/ui/Dialog";
import { ErrorState } from "@/ui/ErrorState";
import { Spinner } from "@/ui/Spinner";
import { toast, describeError } from "@/ui/Toast";
import { confirm } from "@/ui/confirm";
import { TranscriptScroller } from "@/components/TranscriptScroller";
import { TasksPanel } from "@/components/TasksPanel";
import { SubagentsPanel } from "@/components/SubagentsPanel";
import { buildSubagentList, buildTodoList, useLiveTranscript } from "@/lib/transcript";
import {
  agentTranscriptPath,
  useAgent,
  useAgentTurns,
  useAnswerPending,
  useEndAgent,
  useInterrupt,
  useMarkSeen,
  useMarkUnread,
  usePostMessage,
  useReap,
  useRenameAgent,
  useWake,
} from "@/api/agents";
import { useProfiles } from "@/api/profiles";
import { ApiError } from "@/api/client";
import { cn } from "@/lib/cn";
import { formatTokens, formatUsd } from "@/lib/status";
import { relativeTime } from "@/lib/time";
import { Tooltip } from "@/ui/Tooltip";
import { WakeStatusChip, WakeNotice } from "./WakeStatus";
import { useWakeState } from "./useWakeState";
import { consumeWakeSeed } from "./wakeSeed";
import {
  clearAutoSeenSuppression,
  isAutoSeenSuppressed,
  suppressAutoSeen,
} from "./seenSuppress";
import { PendingInputCard } from "./PendingInputCard";
import { AgentQueue, PendingTurnBubble } from "./AgentQueue";
import { AgentEnvPanel } from "./AgentEnvPanel";
import { normalizeCommandList, type ServerCommands } from "./serverInfo";
import type { AgentAnswer, AgentDetailOut } from "@/api/types";

// Code-split the tiptap composer to the agents route (design §8.1, §19.2).
const AgentComposer = lazy(() =>
  import("./AgentComposer").then((m) => ({ default: m.AgentComposer })),
);

export function AgentScreen() {
  const { id } = useParams({ from: "/app/agents/$id" });

  const agentQ = useAgent(id, {
    refetchInterval: (q) => {
      const l = q.state.data?.liveness;
      if (l === "alive" || l === "needs-input") return 2000;
      if (l === "warm") return 5000;
      // Cold / crashed: poll while the screen is focused so a boot — or a crash
      // during a wake — is detected, instead of leaving "Waking" up forever.
      // refetchIntervalInBackground defaults to false, so background tabs don't poll.
      if (l === "cold" || l === "crashed") return 3000;
      return false;
    },
  });
  const agent = agentQ.data;

  const post = usePostMessage(id);
  const interrupt = useInterrupt(id);
  const wake = useWake(id);
  const end = useEndAgent(id);
  const answer = useAnswerPending(id);
  const reap = useReap(id);
  const rename = useRenameAgent(id);

  const [termOpen, setTermOpen] = useState(false);
  // Arriving from the create dialog carries a wake seed (create wakes the agent
  // server-side), so the fresh mount shows "Waking" instead of a false "Cold".
  const [pokedAt, setPokedAt] = useState<number | null>(() => consumeWakeSeed(id));

  const liveness = agent?.liveness;
  const hasActivity =
    (agent?.turns.length ?? 0) > 0 ||
    liveness === "alive" ||
    liveness === "warm" ||
    liveness === "needs-input";
  const tx = useLiveTranscript(agentTranscriptPath(id), hasActivity);

  const todos = useMemo(() => buildTodoList(tx.events), [tx.events]);
  const subagents = useMemo(() => buildSubagentList(tx.events), [tx.events]);

  // Auto-mark-seen: viewing the conversation IS seeing it. Stamp on mount and
  // whenever transcript events land while the tab is visible, trailing-debounced
  // so a streaming turn costs one request after it settles (turn completion ends
  // the event burst, so "seen after the reply finished" falls out naturally).
  // This never fights the unread computation — the stamp only moves forward —
  // and a manual "Mark unread" suppresses it (seenSuppress) until the user
  // leaves the screen and comes back, so re-raised attention sticks.
  const markSeen = useMarkSeen();
  const markSeenRef = useRef(markSeen);
  markSeenRef.current = markSeen;
  useEffect(() => {
    const t = window.setTimeout(() => {
      if (isAutoSeenSuppressed(id)) return;
      if (document.visibilityState !== "visible") return;
      markSeenRef.current.mutate(id);
    }, 800);
    return () => window.clearTimeout(t);
  }, [id, tx.events.length]);
  // Leaving the screen re-arms auto-seen for the next visit (email semantics:
  // mark unread, walk away, the badge stays lit until you actually return).
  useEffect(() => () => clearAutoSeenSuppression(id), [id]);

  const markUnread = useMarkUnread();
  const onMarkUnread = async () => {
    // Mute the auto-stamp FIRST so the debounced effect (or an in-flight timer)
    // cannot immediately re-mark the agent seen while we're still on screen.
    suppressAutoSeen(id);
    try {
      await markUnread.mutateAsync(id);
      toast.success("Marked unread", {
        description: "This agent will show as waiting on you again.",
      });
    } catch (err) {
      clearAutoSeenSuppression(id);
      toast.error("Could not mark unread", { description: describeError(err) });
    }
  };

  // Queued (undelivered) turns render inline at the bottom of the transcript
  // as pending user bubbles. Same query key as AgentQueue below, so the two
  // share one cache entry and one poll.
  const turnsQ = useAgentTurns(id, {
    refetchInterval:
      liveness === "alive" || liveness === "needs-input" ? 2000 : false,
  });
  // Turn ids whose user_message already landed in the transcript. Guards the
  // claim race: the host writes {"type":"user_message","turn_id",...} when it
  // delivers a turn, and that can beat the turns poll noticing the turn left
  // "queued" — without this, the message would double-render for a beat.
  const deliveredTurnIds = useMemo(() => {
    const ids = new Set<string>();
    for (const e of tx.events) {
      if (e.type === "user_message" && typeof e.turn_id === "string") ids.add(e.turn_id);
    }
    return ids;
  }, [tx.events]);
  const pendingTurns = useMemo(
    () =>
      (turnsQ.data ?? []).filter(
        (t) => t.kind === "user" && t.status === "queued" && !deliveredTurnIds.has(t.id),
      ),
    [turnsQ.data, deliveredTurnIds],
  );

  // Latest server_info carries the composer autocomplete seed (slash commands +
  // skills). It's a control event, re-sent on reconnect, so the list self-heals.
  const server: ServerCommands = useMemo(() => {
    for (let i = tx.events.length - 1; i >= 0; i--) {
      const e = tx.events[i];
      if (e.type === "server_info") {
        // Live runners send commands/skills as {name, description} objects;
        // older ones sent plain strings. Normalize both.
        return {
          commands: normalizeCommandList(e.commands),
          skills: normalizeCommandList(e.skills),
        };
      }
    }
    return { commands: [], skills: [] };
  }, [tx.events]);

  // Approximate current context: the newest turn-scope stats event carries the
  // last API call's input + cache_read + cache_write tokens — together, what
  // the model actually held in context on that call. A context-clearing
  // restart that lands after the newest stats event resets the estimate.
  const context = useMemo<ContextEstimate | null>(() => {
    for (let i = tx.events.length - 1; i >= 0; i--) {
      const e = tx.events[i];
      if (e.type === "runtime_restarted" && e.context_cleared === true) {
        return { tokens: 0, model: null, cleared: true };
      }
      if (e.type === "stats" && e.scope === "turn") {
        const n = (v: unknown) => (typeof v === "number" ? v : 0);
        return {
          tokens:
            n(e.input_tokens) + n(e.cache_read_tokens) + n(e.cache_write_tokens),
          model: typeof e.model === "string" ? e.model : null,
          cleared: false,
        };
      }
    }
    return null;
  }, [tx.events]);

  // Track liveness transitions to clear the "waking" affordance honestly:
  //  - once the agent is actually up (alive / warm / needs-input), or
  //  - when a wake attempt ends in a crash (a cold→crashed transition) so the
  //    crash is surfaced as "Crashed" instead of hiding behind a ticking "Waking"
  //    timer. Waking an already-crashed agent is not a transition into crashed,
  //    so it correctly keeps showing "Waking" (recovery in flight).
  const prevLiveness = useRef(liveness);
  useEffect(() => {
    const prev = prevLiveness.current;
    if (liveness === "alive" || liveness === "warm" || liveness === "needs-input") {
      setPokedAt(null);
    } else if (liveness === "crashed" && prev !== "crashed") {
      setPokedAt(null);
    }
    prevLiveness.current = liveness;
  }, [liveness]);

  // Truthful wake state: splits "cold" into parked vs. waking, layers in the
  // worker heartbeat, and times how long a wake has been outstanding. Drives the
  // header chip and the transcript banner so a stuck wake is diagnosed, not spun.
  const wakeState = useWakeState(agent?.liveness ?? "cold", pokedAt);

  if (agentQ.isLoading) {
    return <div className="p-8 text-sm text-moon-600">Loading agent…</div>;
  }
  if (agentQ.isError || !agent) {
    return (
      <div className="grid h-full place-items-center px-4">
        <ErrorState
          title="Agent not found"
          action={
            <Button asChild variant="ghost">
              <Link to="/agents">Back to agents</Link>
            </Button>
          }
        />
      </div>
    );
  }

  const streaming = liveness === "alive";
  const needsInput = liveness === "needs-input";
  const cold = liveness === "cold" || liveness === "crashed";
  const ended = liveness === "ended";
  const pending = agent.pending_input;
  const waking = pokedAt != null && cold;

  const send = async (text: string) => {
    if (cold) setPokedAt(Date.now());
    try {
      await post.mutateAsync({ message: text });
    } catch (err) {
      setPokedAt(null);
      toast.error("Could not send message", { description: describeError(err) });
      throw err;
    }
  };

  const onInterrupt = async () => {
    try {
      await interrupt.mutateAsync(undefined);
    } catch (err) {
      toast.error("Could not interrupt", { description: describeError(err) });
    }
  };

  const onWake = async () => {
    setPokedAt(Date.now());
    try {
      await wake.mutateAsync(undefined);
    } catch (err) {
      setPokedAt(null);
      toast.error("Could not wake agent", { description: describeError(err) });
    }
  };

  const onEnd = async () => {
    if (
      !(await confirm({
        title: "End this agent?",
        body: "Tearing down is permanent — the agent can't be resumed after it ends.",
        confirmLabel: "End agent",
        danger: true,
      }))
    )
      return;
    try {
      await end.mutateAsync(undefined);
      toast.success("Agent ended");
    } catch (err) {
      toast.error("Could not end agent", { description: describeError(err) });
    }
  };

  const onAnswer = async (body: AgentAnswer) => {
    if (!pending) return;
    try {
      await answer.mutateAsync({ requestId: pending.request_id, body });
    } catch (err) {
      toast.error("Could not send your answer", { description: describeError(err) });
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header */}
      <header className="sticky top-0 z-10 flex flex-wrap items-center gap-2 border-b border-ink-700 bg-ink-950/80 px-4 py-3 backdrop-blur sm:px-6">
        <Button asChild variant="ghost" size="sm">
          <Link to="/agents" aria-label="Back to agents">
            <ChevronLeft size={16} />
          </Link>
        </Button>
        <EditableTitle
          title={agent.title}
          disabled={ended}
          saving={rename.isPending}
          onRename={async (title) => {
            try {
              await rename.mutateAsync({ title });
            } catch (err) {
              toast.error("Could not rename agent", { description: describeError(err) });
            }
          }}
        />
        <WakeStatusChip liveness={agent.liveness} wake={wakeState} />
        {/* Cost lives in the Usage rail card (one source of truth). */}

        {(streaming || needsInput) && (
          <Button
            size="sm"
            variant="danger"
            leadingIcon={needsInput ? <XCircle size={14} /> : <Square size={14} />}
            loading={interrupt.isPending}
            onClick={onInterrupt}
          >
            {needsInput ? "Cancel request" : "Interrupt"}
          </Button>
        )}
        {cold && (
          <Button size="sm" variant="ghost" leadingIcon={<SunMedium size={14} />} loading={wake.isPending} onClick={onWake}>
            Wake
          </Button>
        )}
        {!ended && (
          <Tooltip content="Re-raise attention: this agent shows as waiting on you again">
            <Button
              size="sm"
              variant="ghost"
              leadingIcon={<Mail size={14} />}
              loading={markUnread.isPending}
              onClick={onMarkUnread}
            >
              <span className="hidden sm:inline">Mark unread</span>
            </Button>
          </Tooltip>
        )}
        {!ended && (
          <Button
            size="sm"
            variant="ghost"
            leadingIcon={<TerminalSquare size={14} />}
            disabled={streaming || needsInput}
            onClick={() => setTermOpen(true)}
          >
            <span className="hidden sm:inline">Open in terminal</span>
          </Button>
        )}
        {!ended && (
          <Button size="sm" variant="ghost" leadingIcon={<Power size={14} />} onClick={onEnd}>
            <span className="hidden sm:inline">End</span>
          </Button>
        )}
      </header>

      {/* Body: transcript stage + right rail */}
      <div className="grid min-h-0 flex-1 grid-cols-1 overflow-y-auto lg:grid-cols-[minmax(0,1fr)_320px] lg:overflow-hidden">
        <section className="flex min-h-0 flex-col lg:overflow-hidden">
          <WakeNotice wake={wakeState} />
          {/* The scroller owns the scrolling (its inner div is the overflow
              container) so its pin-to-bottom logic actually engages; a scrolling
              wrapper here would grow the scroller to content height and leave
              the pin logic watching a div that never overflows. */}
          <div className="flex min-h-0 flex-1 flex-col px-4 py-4 sm:px-6">
            {hasActivity ? (
              <TranscriptScroller
                events={tx.events}
                status={tx.status}
                running={streaming}
                footer={
                  pendingTurns.length > 0 ? (
                    // Matches TranscriptView's list wrapper so pending bubbles
                    // read as a continuation of the event list.
                    <div className="mt-2 space-y-2 font-mono text-[12px] leading-relaxed">
                      {pendingTurns.map((t) => (
                        <PendingTurnBubble key={t.id} agentId={id} turn={t} />
                      ))}
                    </div>
                  ) : undefined
                }
                className="min-h-0 flex-1"
              />
            ) : (
              <div className="flex min-h-[40vh] items-center justify-center rounded-card border border-dashed border-ink-700 bg-ink-900/40 text-center text-sm text-moon-500">
                Send the first message to start the agent.
              </div>
            )}
          </div>

          {/* Pending ask pinned above the composer. */}
          {!ended && (
            <div className="space-y-2 border-t border-ink-700 px-4 py-3 sm:px-6">
              {pending && <PendingInputCard pending={pending} onAnswer={onAnswer} busy={answer.isPending} />}
              <AgentQueue agentId={id} refetchInterval={streaming || needsInput ? 2000 : false} />
              <Suspense
                fallback={
                  <div className="flex items-center gap-2 rounded-card border border-ink-700 bg-ink-950 px-3 py-3 text-sm text-moon-500">
                    <Spinner /> Loading composer…
                  </div>
                }
              >
                <AgentComposer
                  onSend={send}
                  server={server}
                  sourcePath={agent.source_path}
                  streaming={streaming}
                  waking={waking}
                  wakesOnSend={cold}
                  sending={post.isPending}
                />
              </Suspense>
            </div>
          )}
          {ended && (
            <div className="border-t border-ink-700 px-4 py-4 text-sm text-moon-500 sm:px-6">
              This agent has ended. Its transcript is read-only.
            </div>
          )}
        </section>

        {/* Right rail: progress + runtime + usage + env. Folds under the
            transcript below lg. */}
        <aside className="space-y-3 border-t border-ink-700 px-4 py-4 lg:border-l lg:border-t-0 lg:overflow-y-auto">
          <TasksPanel todos={todos} />
          <SubagentsPanel subagents={subagents} />
          <RuntimeCard agent={agent} />
          <UsageCard agent={agent} context={context} />
          <RailInfo sourcePath={agent.source_path} updatedAt={agent.updated_at} />
          <CollapsibleRail title="Environment">
            <AgentEnvPanel agentId={id} env={agent.env} liveness={agent.liveness} />
          </CollapsibleRail>
        </aside>
      </div>

      <TerminalHandoffDialog
        open={termOpen}
        onOpenChange={setTermOpen}
        sourcePath={agent.source_path}
        claudeSessionId={agent.claude_session_id}
        liveness={agent.liveness}
        onReap={() => reap.mutateAsync(undefined)}
        reaping={reap.isPending}
      />
    </div>
  );
}

/** Frontend-side context estimate derived from the live transcript. */
interface ContextEstimate {
  tokens: number;
  /** Model the newest stats event reported (more truthful than the row's
   *  configured model, which may be null = harness default). */
  model: string | null;
  /** A context-clearing restart landed after the newest stats event. */
  cleared: boolean;
}

/** Context windows for model ids we actually know. Prefix-matched; unknown
 *  models get no bar (a number without a made-up denominator). */
const CONTEXT_WINDOWS: [prefix: string, window: number][] = [
  ["claude-opus-4", 200_000],
  ["claude-sonnet-4", 200_000],
  ["claude-haiku-4", 200_000],
  ["claude-3-", 200_000],
];

function contextWindowFor(model: string | null): number | null {
  if (!model) return null;
  if (model.includes("[1m]")) return 1_000_000;
  for (const [prefix, window] of CONTEXT_WINDOWS) {
    if (model.startsWith(prefix)) return window;
  }
  return null;
}

/** Inline-editable agent title: pencil swaps the h1 for an input. Enter or
 *  blur commits (trimmed, non-empty, only if changed); Escape cancels. */
function EditableTitle({
  title,
  disabled,
  saving,
  onRename,
}: {
  title: string;
  disabled?: boolean;
  saving?: boolean;
  onRename: (title: string) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(title);
  // Escape must skip the input's blur-commit that fires as the input unmounts.
  const cancelled = useRef(false);

  const start = () => {
    setDraft(title);
    cancelled.current = false;
    setEditing(true);
  };
  const commit = () => {
    if (cancelled.current) return;
    setEditing(false);
    const next = draft.trim();
    if (!next || next === title) return;
    void onRename(next);
  };

  if (!editing) {
    return (
      <div className="group flex min-w-0 flex-1 items-center gap-1.5">
        <h1 className="min-w-0 truncate font-display text-lg font-semibold text-moon-100">
          {title}
        </h1>
        {!disabled && (
          <IconButton
            label="Rename agent"
            size="sm"
            icon={<Pencil size={13} />}
            disabled={saving}
            onClick={start}
            className="opacity-0 transition-opacity duration-100 focus-visible:opacity-100 group-hover:opacity-100 pointer-coarse:opacity-100"
          />
        )}
      </div>
    );
  }
  return (
    <div className="min-w-0 flex-1">
      <Input
        autoFocus
        value={draft}
        maxLength={200}
        aria-label="Agent name"
        onChange={(e) => setDraft(e.target.value)}
        onFocus={(e) => e.target.select()}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            commit();
          } else if (e.key === "Escape") {
            cancelled.current = true;
            setEditing(false);
          }
        }}
        className="h-8 max-w-md font-display text-lg font-semibold"
      />
    </div>
  );
}

function RailRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="shrink-0 text-moon-600">{label}</span>
      {children}
    </div>
  );
}

/** Read-only runtime facts: harness, model, profile, posture, access. */
function RuntimeCard({ agent }: { agent: AgentDetailOut }) {
  const profiles = useProfiles();
  const profile = profiles.data?.find((p) => p.id === agent.profile_id);
  // Row model null = harness default; the profile's default_model is what the
  // spawn actually resolves to when set.
  const model = agent.model ?? profile?.default_model ?? null;
  return (
    <div className="space-y-1 rounded-card border border-ink-700 bg-ink-900 px-3 py-2.5 text-[11px] text-moon-500">
      <div className="pb-0.5 text-[11px] font-semibold uppercase tracking-wide text-moon-400">
        Runtime
      </div>
      <RailRow label="Harness">
        <span className="font-mono text-moon-300">{agent.backend}</span>
      </RailRow>
      <RailRow label="Model">
        <span className="min-w-0 truncate font-mono text-moon-300">
          {model ?? "harness default"}
          {agent.model == null && model != null && (
            <span className="text-moon-600"> (profile)</span>
          )}
        </span>
      </RailRow>
      <RailRow label="Profile">
        <span className="min-w-0 truncate font-mono text-moon-300">
          {profile?.name ?? (agent.profile_id ? `${agent.profile_id.slice(0, 8)}…` : "—")}
        </span>
      </RailRow>
      <RailRow label="Posture">
        <span className="font-mono text-moon-300">{agent.posture}</span>
      </RailRow>
      <RailRow label="Access">
        <span className="font-mono text-moon-300">
          {agent.workspace_access.replace(/_/g, " ")}
        </span>
      </RailRow>
    </div>
  );
}

/** Cumulative usage + the live context estimate. Cost's single home (the
 *  header chip was retired in favor of this card). */
function UsageCard({
  agent,
  context,
}: {
  agent: AgentDetailOut;
  context: ContextEstimate | null;
}) {
  const window = context && !context.cleared ? contextWindowFor(context.model) : null;
  const pct = window && context ? Math.min(100, (context.tokens / window) * 100) : null;
  return (
    <div className="space-y-1 rounded-card border border-ink-700 bg-ink-900 px-3 py-2.5 text-[11px] text-moon-500">
      <div className="pb-0.5 text-[11px] font-semibold uppercase tracking-wide text-moon-400">
        Usage
      </div>
      <RailRow label="Tokens in">
        <span className="font-mono tabular-nums text-moon-300">
          {formatTokens(agent.input_tokens + agent.cache_read_tokens + agent.cache_write_tokens)}
        </span>
      </RailRow>
      <RailRow label="Tokens out">
        <span className="font-mono tabular-nums text-moon-300">
          {formatTokens(agent.output_tokens)}
        </span>
      </RailRow>
      <RailRow label="Cost">
        <span className="font-mono tabular-nums text-lamp">{formatUsd(agent.cost_usd)}</span>
      </RailRow>
      {context && (
        <div className="pt-1">
          <RailRow label="Context">
            <Tooltip content="Approximate: the last turn's input + cache tokens">
              <span className="cursor-help font-mono tabular-nums text-moon-300">
                {context.cleared
                  ? "cleared"
                  : `~${formatTokens(context.tokens)} tokens`}
                {window != null && (
                  <span className="text-moon-600"> / {formatTokens(window)}</span>
                )}
              </span>
            </Tooltip>
          </RailRow>
          {pct != null && (
            <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-ink-700">
              <div
                className={cn("h-full rounded-full", pct > 85 ? "bg-failed" : "bg-lamp")}
                style={{ width: `${Math.max(2, pct)}%` }}
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function RailInfo({ sourcePath, updatedAt }: { sourcePath: string; updatedAt: string }) {
  return (
    <div className="rounded-card border border-ink-700 bg-ink-900 px-3 py-2.5 text-[11px] text-moon-500">
      <div className="mb-1 flex items-baseline justify-between gap-2">
        <span className="text-moon-600">Working dir</span>
        <Tooltip content={sourcePath} mono>
          <span className="min-w-0 cursor-help truncate font-mono text-moon-300">
            {sourcePath}
          </span>
        </Tooltip>
      </div>
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-moon-600">Last activity</span>
        <span className="font-mono text-moon-300">{relativeTime(updatedAt)}</span>
      </div>
    </div>
  );
}

function CollapsibleRail({ title, children }: { title: string; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-card border border-ink-700 bg-ink-900">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
        aria-expanded={open}
      >
        <span className="text-[11px] font-semibold uppercase tracking-wide text-moon-400">{title}</span>
        <span className="ml-auto text-[11px] text-moon-600">{open ? "Hide" : "Edit"}</span>
      </button>
      {open && <div className="border-t border-ink-700/60 px-3 py-3">{children}</div>}
    </div>
  );
}

function CommandBlock({ cmd }: { cmd: string }) {
  return (
    <div className="space-y-2">
      <pre className="overflow-x-auto rounded-control border border-ink-700 bg-ink-950 px-3 py-2.5 font-mono text-[12px] text-moon-100">
        {cmd}
      </pre>
      <Button
        size="sm"
        variant="ghost"
        onClick={() => {
          void navigator.clipboard?.writeText(cmd);
          toast.success("Command copied");
        }}
      >
        Copy command
      </Button>
    </div>
  );
}

function TerminalHandoffDialog({
  open,
  onOpenChange,
  sourcePath,
  claudeSessionId,
  liveness,
  onReap,
  reaping,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  sourcePath: string;
  claudeSessionId: string | null;
  liveness: "alive" | "needs-input" | "warm" | "cold" | "ended" | "crashed";
  onReap: () => Promise<unknown>;
  reaping: boolean;
}) {
  const [handedOff, setHandedOff] = useState(false);
  const warm = liveness === "warm";
  const exactCmd = claudeSessionId
    ? `cd ${sourcePath} && claude --resume ${claudeSessionId}`
    : null;

  const handOff = async () => {
    try {
      await onReap();
      setHandedOff(true);
      toast.success("Handed off — the desk released its connection");
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        toast.error("Can't hand off yet", {
          description: "A turn is streaming or an input is pending. Let it settle, then try again.",
        });
        return;
      }
      toast.error("Could not hand off", { description: describeError(err) });
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (!o) setHandedOff(false);
        onOpenChange(o);
      }}
      title="Open in terminal"
      description="Continue this agent's session from your own terminal."
      footer={
        <Button variant="ghost" onClick={() => onOpenChange(false)}>
          Close
        </Button>
      }
    >
      <div className="space-y-3">
        {exactCmd == null ? (
          // Never run: no session id yet. Generic guidance.
          <>
            <p className="text-[12px] text-moon-500">
              This agent hasn&apos;t run yet, so there&apos;s no resumable session to point at. Once it
              has taken a turn, this dialog shows the exact <span className="font-mono">--resume</span>{" "}
              command. For now:
            </p>
            <CommandBlock cmd={`cd ${sourcePath} && claude --resume`} />
            <p className="text-[12px] text-moon-500">
              Claude lists the resumable sessions in this directory to pick from.
            </p>
          </>
        ) : warm && !handedOff ? (
          // Warm here: reap first so there's a single writer, then reveal the command.
          <>
            <p className="text-[12px] text-moon-500">
              This agent is warm on the desk. Hand it off to release the desk&apos;s connection so the
              two don&apos;t write the same session at once — then resume it in your terminal.
            </p>
            <Button variant="primary" leadingIcon={<TerminalSquare size={14} />} loading={reaping} onClick={handOff}>
              Hand off to terminal
            </Button>
          </>
        ) : (
          // Cold / crashed, or just handed off: the exact command is safe to run.
          <>
            {handedOff && (
              <p className="text-[12px] text-success">Released. Resume it from your terminal:</p>
            )}
            <CommandBlock cmd={exactCmd} />
            <p className="text-[12px] text-moon-500">
              Runs the same on-disk session. Keep a single writer — don&apos;t send from the desk while
              you drive it from the terminal.
            </p>
          </>
        )}
      </div>
    </Dialog>
  );
}
