import { Suspense, lazy, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "@tanstack/react-router";
import {
  ChevronLeft,
  Power,
  Square,
  SunMedium,
  TerminalSquare,
  XCircle,
} from "lucide-react";
import { Button } from "@/ui/Button";
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
  useAnswerPending,
  useEndAgent,
  useInterrupt,
  usePostMessage,
  useWake,
} from "@/api/agents";
import { formatUsd } from "@/lib/status";
import { relativeTime } from "@/lib/time";
import { AgentStatePill } from "./AgentStatePill";
import { PendingInputCard } from "./PendingInputCard";
import { AgentQueue } from "./AgentQueue";
import { AgentEnvPanel } from "./AgentEnvPanel";
import type { ServerCommands } from "./AgentComposer";
import type { AgentAnswer } from "@/api/types";

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
      return false;
    },
  });
  const agent = agentQ.data;

  const post = usePostMessage(id);
  const interrupt = useInterrupt(id);
  const wake = useWake(id);
  const end = useEndAgent(id);
  const answer = useAnswerPending(id);

  const [termOpen, setTermOpen] = useState(false);
  const [pokedAt, setPokedAt] = useState<number | null>(null);

  const liveness = agent?.liveness;
  const hasActivity =
    (agent?.turns.length ?? 0) > 0 ||
    liveness === "alive" ||
    liveness === "warm" ||
    liveness === "needs-input";
  const tx = useLiveTranscript(agentTranscriptPath(id), hasActivity);

  const todos = useMemo(() => buildTodoList(tx.events), [tx.events]);
  const subagents = useMemo(() => buildSubagentList(tx.events), [tx.events]);

  // Latest server_info carries the composer autocomplete seed (slash commands +
  // skills). It's a control event, re-sent on reconnect, so the list self-heals.
  const server: ServerCommands = useMemo(() => {
    for (let i = tx.events.length - 1; i >= 0; i--) {
      const e = tx.events[i];
      if (e.type === "server_info") {
        return {
          commands: Array.isArray(e.commands) ? (e.commands as string[]) : [],
          skills: Array.isArray(e.skills) ? (e.skills as string[]) : [],
        };
      }
    }
    return { commands: [], skills: [] };
  }, [tx.events]);

  // Clear the "waking" affordance once the agent is actually up.
  useEffect(() => {
    if (liveness === "alive" || liveness === "warm" || liveness === "needs-input") {
      setPokedAt(null);
    }
  }, [liveness]);

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
        <h1 className="min-w-0 flex-1 truncate font-display text-lg font-semibold text-moon-100">
          {agent.title}
        </h1>
        <AgentStatePill liveness={agent.liveness} />
        {agent.cost_usd > 0 && (
          <span className="font-mono text-[11px] tabular-nums text-lamp">{formatUsd(agent.cost_usd)}</span>
        )}

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
          <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-6">
            {hasActivity ? (
              <TranscriptScroller
                events={tx.events}
                status={tx.status}
                running={streaming}
                className="min-h-0"
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
              <AgentQueue turns={agent.turns} />
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

        {/* Right rail: progress + env. Folds under the transcript below lg. */}
        <aside className="space-y-3 border-t border-ink-700 px-4 py-4 lg:border-l lg:border-t-0 lg:overflow-y-auto">
          <TasksPanel todos={todos} />
          <SubagentsPanel subagents={subagents} />
          <RailInfo
            sourcePath={agent.source_path}
            model={agent.model}
            updatedAt={agent.updated_at}
          />
          <CollapsibleRail title="Environment">
            <AgentEnvPanel agentId={id} env={agent.env} liveness={agent.liveness} />
          </CollapsibleRail>
        </aside>
      </div>

      <TerminalHandoffDialog
        open={termOpen}
        onOpenChange={setTermOpen}
        sourcePath={agent.source_path}
        cold={cold}
      />
    </div>
  );
}

function RailInfo({
  sourcePath,
  model,
  updatedAt,
}: {
  sourcePath: string;
  model: string | null;
  updatedAt: string;
}) {
  return (
    <div className="rounded-card border border-ink-700 bg-ink-900 px-3 py-2.5 text-[11px] text-moon-500">
      <div className="mb-1 flex items-baseline justify-between gap-2">
        <span className="text-moon-600">Working dir</span>
        <span className="min-w-0 truncate font-mono text-moon-300" title={sourcePath}>
          {sourcePath}
        </span>
      </div>
      {model && (
        <div className="mb-1 flex items-baseline justify-between gap-2">
          <span className="text-moon-600">Model</span>
          <span className="font-mono text-moon-300">{model}</span>
        </div>
      )}
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

function TerminalHandoffDialog({
  open,
  onOpenChange,
  sourcePath,
  cold,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  sourcePath: string;
  cold: boolean;
}) {
  const cmd = `cd ${sourcePath} && claude --resume`;
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Open in terminal"
      description="Continue this agent's session from your own terminal — Claude lists the resumable sessions in this directory to pick from."
      footer={
        <Button variant="ghost" onClick={() => onOpenChange(false)}>
          Close
        </Button>
      }
    >
      <div className="space-y-3">
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
        {!cold && (
          <p className="text-[12px] text-moon-500">
            This agent is still warm here. To avoid two writers on the same session, let it go cold
            (or end it) before you drive it from a terminal — otherwise the desk keeps its own live
            connection.
          </p>
        )}
      </div>
    </Dialog>
  );
}
