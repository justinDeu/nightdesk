import { useState } from "react";
import { Link, Outlet, useMatch, useNavigate } from "@tanstack/react-router";
import { useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { Bot, Mail, MailOpen, Plus, Trash2 } from "lucide-react";
import { ExperimentalBanner } from "@/components/ExperimentalBanner";
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
  agentsApi,
  useAgents,
  useCreateAgent,
  useDeleteAgent,
  useMarkSeen,
  useMarkUnread,
} from "@/api/agents";
import { qk } from "@/api/keys";
import { cn } from "@/lib/cn";
import { AgentStatePill } from "./AgentStatePill";
import { seedWake } from "./wakeSeed";
import type { AgentLiveness, AgentOut } from "@/api/types";

/** Resident interactive agents, as a list + detail split view. This component
 *  is the layout for BOTH /agents and /agents/$id (the screen route is nested
 *  under it), so it stays mounted across switches — clicking a row only swaps
 *  the detail pane (the <Outlet/>), never reloading the list. The URL still
 *  carries the agent id, so deep links and middle-click keep working. */
export function AgentsPage() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  // The matched child route carries the selected agent id (undefined at /agents).
  const agentMatch = useMatch({ from: "/app/agents/$id", shouldThrow: false });
  const selectedId = agentMatch?.params.id ?? null;

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

  // Prefetch detail + queue turns on hover/focus so a click swaps the pane
  // from cache (one hit, not a route load) — the list never unmounts, so the
  // switch is as cheap as the tickets side-peek.
  const prefetch = (id: string) => {
    qc.prefetchQuery({ queryKey: qk.agents.detail(id), queryFn: () => agentsApi.get(id) });
    qc.prefetchQuery({ queryKey: qk.agents.turns(id), queryFn: () => agentsApi.listTurns(id) });
  };

  const count = agentsQ.data?.length ?? null;

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Experimental notice lives once at the top of the agents surface
          (was duplicated across the old list + screen pages). */}
      <div className="shrink-0 border-b border-ink-700 px-4 py-2 sm:px-5">
        <ExperimentalBanner feature="Agent sessions" storageKey="agents" />
      </div>

      <div className="flex min-h-0 flex-1">
        {/* Rail: agent list. Full-width on mobile when nothing is selected;
            a fixed 320px column on desktop. */}
        <nav
          aria-label="Agents"
          className={cn(
            "min-h-0 w-full shrink-0 flex-col border-r border-ink-700 bg-ink-950 lg:flex lg:w-[320px]",
            selectedId ? "hidden lg:flex" : "flex",
          )}
        >
          <header className="flex h-11 shrink-0 items-center gap-2 border-b border-ink-700 px-3">
            <h1 className="font-display text-sm font-semibold tracking-tight text-moon-100">
              Agents
            </h1>
            {count != null && count > 0 && (
              <span className="rounded-full bg-ink-800 px-1.5 font-mono text-[10px] text-moon-600">
                {count}
              </span>
            )}
            <Button
              size="sm"
              variant="primary"
              leadingIcon={<Plus size={14} />}
              onClick={() => setDialogOpen(true)}
              className="ml-auto"
            >
              New
            </Button>
          </header>

          <div className="min-h-0 flex-1 overflow-y-auto p-2">
            <RailList
              agentsQ={agentsQ}
              selectedId={selectedId}
              onDelete={onDelete}
              onPrefetch={prefetch}
            />
          </div>
        </nav>

        {/* Detail pane: the agent screen, or a prompt when nothing is open.
            Hidden on mobile until an agent is selected (list -> detail nav). */}
        <section
          className={cn(
            "min-h-0 min-w-0 flex-1 flex-col bg-ink-950 lg:flex",
            selectedId ? "flex" : "hidden",
          )}
        >
          {selectedId ? (
            <Outlet />
          ) : (
            <DetailEmpty
              loading={agentsQ.isLoading}
              error={agentsQ.isError}
              hasAgents={(agentsQ.data?.length ?? 0) > 0}
              onRetry={() => agentsQ.refetch()}
              onCreate={() => setDialogOpen(true)}
            />
          )}
        </section>
      </div>

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
    </div>
  );
}

/** Left accent strip keyed to liveness: the dawn-edge gradient marks a live
 *  agent (sibling of the running-ticket treatment), the accent marks
 *  needs-input, ember marks crashed. */
const ROW_ACCENT: Record<AgentLiveness, string> = {
  alive: "dawn-edge",
  "needs-input": "bg-lamp",
  warm: "bg-dawn",
  cold: "bg-ink-700",
  ended: "bg-ink-700",
  crashed: "bg-failed",
};

function RailList({
  agentsQ,
  selectedId,
  onDelete,
  onPrefetch,
}: {
  agentsQ: UseQueryResult<AgentOut[]>;
  selectedId: string | null;
  onDelete: (a: AgentOut) => void;
  onPrefetch: (id: string) => void;
}) {
  if (agentsQ.isError) {
    return (
      <div className="px-1 py-2">
        <ErrorState
          title="Could not load agents"
          action={
            <Button variant="ghost" size="sm" onClick={() => agentsQ.refetch()}>
              Retry
            </Button>
          }
        />
      </div>
    );
  }
  if (agentsQ.isLoading) {
    return <div className="px-2 py-3 text-sm text-moon-600">Loading agents…</div>;
  }
  if ((agentsQ.data?.length ?? 0) === 0) {
    return (
      <div className="flex flex-col items-center gap-2 px-2 py-6 text-center">
        <Bot size={18} className="text-moon-600" />
        <p className="text-xs text-moon-600">
          No agents yet. Click <span className="text-moon-400">New</span> to start one.
        </p>
      </div>
    );
  }
  return (
    <ul className="flex flex-col gap-0.5">
      {agentsQ.data!.map((a) => (
        <AgentRailRow
          key={a.id}
          agent={a}
          active={a.id === selectedId}
          onDelete={() => onDelete(a)}
          onPrefetch={() => onPrefetch(a.id)}
        />
      ))}
    </ul>
  );
}

/** Compact rail row: accent strip (liveness/wake), unread dot, name, working
 *  dir, and the state pill. Real link so middle-click opens /agents/$id in a
 *  new tab. Hover reveals mark-read / delete in the pill's slot. */
function AgentRailRow({
  agent,
  active,
  onDelete,
  onPrefetch,
}: {
  agent: AgentOut;
  active: boolean;
  onDelete: () => void;
  onPrefetch: () => void;
}) {
  const live =
    agent.liveness === "alive" ||
    agent.liveness === "needs-input" ||
    agent.liveness === "warm";
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
    <li className="group relative flex items-center">
      <Link
        to="/agents/$id"
        params={{ id: agent.id }}
        onMouseEnter={onPrefetch}
        onFocus={onPrefetch}
        aria-current={active ? "page" : undefined}
        className={cn(
          "relative flex min-w-0 flex-1 items-center gap-2.5 rounded-control px-3 py-2 text-left transition-colors",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-lamp",
          active
            ? "wash-selected bg-ink-800/70 text-moon-100"
            : "text-moon-300 hover:bg-ink-800/50 hover:text-moon-100",
        )}
      >
        <span
          aria-hidden
          className={cn(
            "absolute inset-y-2 left-0 w-[2px] rounded-full",
            ROW_ACCENT[agent.liveness],
          )}
        />
        {agent.unread ? (
          <Tooltip content="Replied — waiting on you">
            <span
              aria-label="Unread reply"
              className="h-1.5 w-1.5 shrink-0 rounded-full bg-lamp"
            />
          </Tooltip>
        ) : (
          <span className="h-1.5 w-1.5 shrink-0" aria-hidden />
        )}
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-1.5">
            <span className="min-w-0 truncate text-[13px] font-medium leading-tight">
              {agent.title}
            </span>
            {agent.has_pending && agent.liveness !== "needs-input" && (
              <span className="shrink-0 rounded-full bg-lamp/15 px-1.5 py-0.5 font-mono text-[9px] font-semibold uppercase text-lamp">
                queued
              </span>
            )}
          </span>
          <span className="mt-0.5 block truncate font-mono text-[11px] text-moon-600">
            {agent.source_path || "(scratch dir)"}
          </span>
        </span>
        <AgentStatePill
          liveness={agent.liveness}
          className="shrink-0 transition-opacity duration-100 group-hover:opacity-0"
        />
      </Link>

      {/* Hover actions sit in the pill's slot — siblings of the link, not
          nested, so clicking them never navigates. pointer-events-none until
          hover so the invisible buttons don't swallow clicks meant for the link. */}
      <div className="pointer-events-none absolute right-1.5 top-1/2 flex -translate-y-1/2 items-center gap-0.5 opacity-0 transition-opacity duration-100 group-hover:pointer-events-auto group-hover:opacity-100 group-focus-within:pointer-events-auto group-focus-within:opacity-100">
        <IconButton
          label={agent.unread ? "Mark read" : "Mark unread"}
          size="sm"
          icon={agent.unread ? <MailOpen size={14} /> : <Mail size={14} />}
          onClick={toggleSeen}
        />
        <IconButton
          label={live ? "End the agent before deleting" : "Delete agent"}
          size="sm"
          icon={<Trash2 size={14} />}
          disabled={live}
          onClick={onDelete}
          className="hover:text-failed"
        />
      </div>
    </li>
  );
}

/** The detail pane when no agent is open: a create CTA when there are no
 *  agents, otherwise a "pick one" prompt. Desktop-only by construction (the
 *  pane is hidden on mobile until a selection exists). */
function DetailEmpty({
  loading,
  error,
  hasAgents,
  onRetry,
  onCreate,
}: {
  loading: boolean;
  error: boolean;
  hasAgents: boolean;
  onRetry: () => void;
  onCreate: () => void;
}) {
  if (error) {
    return (
      <div className="grid h-full place-items-center px-4">
        <ErrorState
          title="Could not load agents"
          action={<Button variant="ghost" onClick={onRetry}>Retry</Button>}
        />
      </div>
    );
  }
  if (!hasAgents && !loading) {
    return (
      <div className="grid h-full place-items-center px-4">
        <EmptyState
          icon={<Bot size={20} />}
          title="No agents yet"
          description="Start a resident agent against a profile and a working directory. It stays warm between turns and asks you when it needs a decision."
          action={
            <Button variant="primary" leadingIcon={<Plus size={15} />} onClick={onCreate}>
              New agent
            </Button>
          }
        />
      </div>
    );
  }
  return (
    <div className="grid h-full place-items-center px-4 text-center">
      <div className="max-w-xs">
        <Bot size={22} className="mx-auto mb-2 text-moon-600" />
        <p className="text-sm text-moon-400">Select an agent</p>
        <p className="mt-1 text-xs text-moon-600">
          Pick one from the list to open its conversation. Middle-click opens it in a new tab.
        </p>
      </div>
    </div>
  );
}

// Resident agent sessions have a runtime for every backend
// worker/resident_backends.py ships a ResidentBackend for — currently
// claude_sdk (ClaudeResidentBackend) and opencode (OpencodeResidentBackend).
// The API rejects a create against anything else with a 422 (see
// nightdesk.domain.sessions.UnsupportedBackend). Mirror that here so the
// picker prevents the failure instead of just surfacing it.
const RESIDENT_BACKENDS = new Set(["claude_sdk", "opencode"]);

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

  const supportedProfiles = (profiles.data ?? []).filter((p) => RESIDENT_BACKENDS.has(p.backend));
  const effectiveProfile = profileId || supportedProfiles[0]?.id || "";
  const chosenProfile = profiles.data?.find((p) => p.id === effectiveProfile);
  const hasUnsupportedProfiles = (profiles.data ?? []).some(
    (p) => !RESIDENT_BACKENDS.has(p.backend),
  );

  const submit = async () => {
    if (!effectiveProfile) {
      toast.error("Pick a profile first");
      return;
    }
    if (chosenProfile && !RESIDENT_BACKENDS.has(chosenProfile.backend)) {
      toast.error("Agent sessions don't support this profile's backend yet", {
        description: `“${chosenProfile.name}” uses the ${chosenProfile.backend} backend.`,
      });
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
      description={
        chosenProfile?.backend === "opencode"
          ? "A resident agent works against a profile and an optional directory. Trusted posture: it runs opencode directly on the host, dialing the profile's configured provider."
          : "A resident agent works against a profile and an optional directory. Trusted posture: it runs on your real ~/.claude — your skills, hooks, and CLAUDE.md all apply."
      }
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
        <Field
          label="Profile"
          hint={
            hasUnsupportedProfiles
              ? "Sessions support Claude and opencode profiles for now — profiles on another backend are disabled below."
              : undefined
          }
        >
          <Select value={effectiveProfile} onChange={(e) => setProfileId(e.target.value)}>
            {(profiles.data ?? []).map((p) => {
              const supported = RESIDENT_BACKENDS.has(p.backend);
              return (
                <option key={p.id} value={p.id} disabled={!supported}>
                  {p.name} ({p.backend}){supported ? "" : " — unsupported backend"}
                </option>
              );
            })}
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
