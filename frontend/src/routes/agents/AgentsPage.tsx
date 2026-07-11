import { useState } from "react";
import { Link, useNavigate } from "@tanstack/react-router";
import { Bot, Mail, MailOpen, Plus, Trash2 } from "lucide-react";
import { Page } from "@/components/Page";
import { Button } from "@/ui/Button";
import { Dialog } from "@/ui/Dialog";
import { Field, Input } from "@/ui/Input";
import { Select } from "@/ui/Select";
import { EmptyState } from "@/ui/EmptyState";
import { ErrorState } from "@/ui/ErrorState";
import { IconButton } from "@/ui/IconButton";
import { Tooltip } from "@/ui/Tooltip";
import { PathInput } from "@/components/PathInput";
import { toast, describeError } from "@/ui/Toast";
import { confirm } from "@/ui/confirm";
import { useProfiles } from "@/api/profiles";
import {
  useAgents,
  useCreateAgent,
  useDeleteAgent,
  useMarkSeen,
  useMarkUnread,
} from "@/api/agents";
import { relativeTime } from "@/lib/time";
import { formatUsd, formatTokens } from "@/lib/status";
import { cn } from "@/lib/cn";
import { AgentStatePill } from "./AgentStatePill";
import { seedWake } from "./wakeSeed";
import type { AgentLiveness, AgentOut } from "@/api/types";

/** Resident interactive agents: long-lived chats an agent drives against a
 *  profile and a working tree, with a first-class needs-input loop. Each row
 *  opens the agent screen. */
export function AgentsPage() {
  const navigate = useNavigate();
  const agentsQ = useAgents({
    // Poll while any agent is doing something (alive / needs-input) or booting.
    refetchInterval: (q) =>
      (q.state.data ?? []).some(
        (a) => a.liveness === "alive" || a.liveness === "needs-input",
      )
        ? 3000
        : false,
  });
  const [dialogOpen, setDialogOpen] = useState(false);
  const del = useDeleteAgent();

  const onDelete = async (a: AgentOut) => {
    if (
      !(await confirm({
        title: `Delete “${a.title}”?`,
        body: "This removes the agent and its turn history. This cannot be undone.",
        confirmLabel: "Delete",
        danger: true,
      }))
    )
      return;
    try {
      await del.mutateAsync(a.id);
    } catch (err) {
      toast.error("Could not delete agent", { description: describeError(err) });
    }
  };

  return (
    <Page
      title="Agents"
      subtitle="Long-lived interactive agents you steer — they ask when they need you."
      width="wide"
      actions={
        <Button variant="primary" leadingIcon={<Plus size={15} />} onClick={() => setDialogOpen(true)}>
          New agent
        </Button>
      }
    >
      {agentsQ.isError ? (
        <ErrorState
          title="Could not load agents"
          action={<Button variant="ghost" onClick={() => agentsQ.refetch()}>Retry</Button>}
        />
      ) : agentsQ.isLoading ? (
        <div className="p-8 text-sm text-moon-600">Loading agents…</div>
      ) : (agentsQ.data?.length ?? 0) === 0 ? (
        <EmptyState
          icon={<Bot size={20} />}
          title="No agents yet"
          description="Start a resident agent against a profile and a working directory. It stays warm between turns and asks you when it needs a decision."
          action={
            <Button variant="primary" leadingIcon={<Plus size={15} />} onClick={() => setDialogOpen(true)}>
              New agent
            </Button>
          }
        />
      ) : (
        <ul className="flex flex-col gap-2">
          {agentsQ.data!.map((a) => (
            <AgentRow key={a.id} agent={a} onDelete={() => onDelete(a)} />
          ))}
        </ul>
      )}

      <NewAgentDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        onCreated={(id) => {
          setDialogOpen(false);
          // Create wakes the agent server-side; seed the wake timestamp so the
          // detail screen mounts showing "Waking", not a false "Cold".
          seedWake(id);
          navigate({ to: "/agents/$id", params: { id } });
        }}
      />
    </Page>
  );
}

/** Left accent strip keyed to liveness: the dawn-edge gradient marks a live
 *  agent (sibling of the running-ticket treatment), the accent marks needs-input,
 *  ember marks crashed. */
const ROW_ACCENT: Record<AgentLiveness, string> = {
  alive: "dawn-edge",
  "needs-input": "bg-lamp",
  warm: "bg-dawn",
  cold: "bg-ink-700",
  ended: "bg-ink-700",
  crashed: "bg-failed",
};

/** Shorten a model id for a tight card row; the full id is in its tooltip. */
function shortModel(model: string): string {
  const tail = model.split("/").pop() ?? model;
  return tail.length > 22 ? `${tail.slice(0, 21)}…` : tail;
}

function AgentRow({
  agent,
  onDelete,
}: {
  agent: AgentOut;
  onDelete: () => void;
}) {
  const live =
    agent.liveness === "alive" || agent.liveness === "needs-input" || agent.liveness === "warm";
  const tokens = agent.input_tokens + agent.output_tokens;
  const markSeen = useMarkSeen();
  const markUnread = useMarkUnread();
  const toggleSeen = async () => {
    try {
      await (agent.unread ? markSeen : markUnread).mutateAsync(agent.id);
    } catch (err) {
      toast.error(
        agent.unread ? "Could not mark read" : "Could not mark unread",
        { description: describeError(err) },
      );
    }
  };
  return (
    <li className="group relative overflow-hidden rounded-card border border-ink-700 bg-ink-900/60 transition-colors hover:border-ink-600 hover:bg-ink-800/60">
      <span aria-hidden className={cn("absolute inset-y-0 left-0 w-[2px]", ROW_ACCENT[agent.liveness])} />
      <div className="flex items-center gap-3 py-2.5 pl-4 pr-2">
        <Link
          to="/agents/$id"
          params={{ id: agent.id }}
          className="flex min-w-0 flex-1 flex-col items-start gap-1 rounded-[4px] text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lamp"
        >
          <span className="flex w-full items-center gap-2">
            <Bot size={14} className="shrink-0 text-moon-500" />
            {agent.unread && (
              <Tooltip content="Replied — waiting on you">
                <span
                  aria-label="Unread reply"
                  className="h-1.5 w-1.5 shrink-0 rounded-full bg-lamp"
                />
              </Tooltip>
            )}
            <span className="min-w-0 flex-1 truncate text-sm font-medium text-moon-100 group-hover:text-lamp">
              {agent.title}
            </span>
            {agent.has_pending && agent.liveness !== "needs-input" && (
              <span className="shrink-0 rounded-full bg-lamp/15 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-lamp">
                queued
              </span>
            )}
          </span>
          <span className="flex w-full items-center gap-1.5 pl-[22px] font-mono text-[11px] text-moon-600">
            <span className="min-w-0 flex-1 truncate">{agent.source_path || "(scratch dir)"}</span>
            {agent.model && (
              <Tooltip content={agent.model} mono>
                <span className="hidden shrink-0 text-moon-500 hover:text-moon-300 sm:inline">
                  · {shortModel(agent.model)}
                </span>
              </Tooltip>
            )}
          </span>
        </Link>

        {/* At-a-glance metrics, right-aligned. Cost + tokens fold away on narrow
            screens; the state pill and delete stay reachable. */}
        <div className="flex shrink-0 items-center gap-2.5 pl-1">
          {tokens > 0 && (
            <Tooltip
              mono
              content={
                <span>
                  in {formatTokens(agent.input_tokens)} · out {formatTokens(agent.output_tokens)} ·
                  cache {formatTokens(agent.cache_read_tokens + agent.cache_write_tokens)} tokens
                </span>
              }
            >
              <span className="hidden font-mono text-[11px] tabular-nums text-moon-500 sm:inline">
                {formatTokens(tokens)}
                <span className="ml-0.5 text-moon-600">tok</span>
              </span>
            </Tooltip>
          )}
          {agent.cost_usd > 0 && (
            <Tooltip content="Total spend" mono>
              <span className="hidden font-mono text-[11px] tabular-nums text-moon-400 sm:inline">
                {formatUsd(agent.cost_usd)}
              </span>
            </Tooltip>
          )}
          <AgentStatePill liveness={agent.liveness} />
          <span className="hidden w-14 shrink-0 text-right font-mono text-[11px] text-moon-600 md:inline">
            {relativeTime(agent.updated_at)}
          </span>
          <IconButton
            label={agent.unread ? "Mark read" : "Mark unread"}
            size="sm"
            icon={agent.unread ? <MailOpen size={14} /> : <Mail size={14} />}
            onClick={toggleSeen}
            className="opacity-0 transition-opacity group-hover:opacity-100"
          />
          <IconButton
            label={live ? "End the agent before deleting" : "Delete agent"}
            size="sm"
            icon={<Trash2 size={14} />}
            disabled={live}
            onClick={onDelete}
            className="opacity-0 transition-opacity group-hover:opacity-100 hover:text-failed"
          />
        </div>
      </div>
    </li>
  );
}

function NewAgentDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onCreated: (id: string) => void;
}) {
  const profiles = useProfiles();
  const create = useCreateAgent();
  const [title, setTitle] = useState("");
  const [profileId, setProfileId] = useState("");
  const [sourcePath, setSourcePath] = useState("");
  const [idleTimeout, setIdleTimeout] = useState("");

  const effectiveProfile = profileId || profiles.data?.[0]?.id || "";

  const submit = async () => {
    if (!effectiveProfile) {
      toast.error("Pick a profile first");
      return;
    }
    const idle = idleTimeout.trim() ? Number(idleTimeout.trim()) : undefined;
    if (idle !== undefined && (!Number.isFinite(idle) || idle <= 0)) {
      toast.error("Idle timeout must be a positive number of seconds");
      return;
    }
    try {
      const a = await create.mutateAsync({
        title: title.trim() || undefined,
        profile_id: effectiveProfile,
        source_path: sourcePath.trim() || undefined,
        idle_timeout_s: idle,
      });
      setTitle("");
      setSourcePath("");
      setIdleTimeout("");
      onCreated(a.id);
    } catch (err) {
      toast.error("Could not start agent", { description: describeError(err) });
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="New agent"
      description="A resident agent works against a profile and an optional directory. Trusted posture: it runs on your real ~/.claude — your skills, hooks, and CLAUDE.md all apply."
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" onClick={submit} loading={create.isPending}>
            Start agent
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <Field label="Title" hint="Optional — defaults to “Agent”.">
          <Input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Agent" />
        </Field>
        <Field label="Profile">
          <Select value={effectiveProfile} onChange={(e) => setProfileId(e.target.value)}>
            {(profiles.data ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} ({p.backend})
              </option>
            ))}
          </Select>
        </Field>
        <Field
          label="Working directory"
          hint="Optional — leave blank for a fresh scratch directory. A path runs the agent in-place on that directory."
        >
          <PathInput value={sourcePath} onChange={setSourcePath} placeholder="/path/to/repo (optional)" />
        </Field>
        <Field
          label="Idle timeout (seconds)"
          hint="Optional — how long the agent stays warm with no activity before going cold. Blank inherits the global default."
        >
          <Input
            type="number"
            min={1}
            value={idleTimeout}
            onChange={(e) => setIdleTimeout(e.target.value)}
            placeholder="inherit global default"
          />
        </Field>
      </div>
    </Dialog>
  );
}
