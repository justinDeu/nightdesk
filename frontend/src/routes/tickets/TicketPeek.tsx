import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ArrowRight, CalendarClock, Cpu, Maximize2, Zap, X } from "lucide-react";
import type { LabelOut, ProjectOut, RunOut, TicketOut } from "@/api/types";
import { qk } from "@/api";
import { labelsApi } from "@/api/labels";
import { ticketsApi, useUpdateTicket } from "@/api/tickets";
import { useProfiles } from "@/api/profiles";
import { Button } from "@/ui/Button";
import { Dialog } from "@/ui/Dialog";
import { Textarea } from "@/ui/Input";
import { StatusPill } from "@/ui/StatusPill";
import { Tooltip } from "@/ui/Tooltip";
import { toast } from "@/ui/Toast";
import { PriorityPicker, ProfilePicker, ProjectPicker, LabelPicker } from "@/components/PropertyPickers";
import { EffectiveConfigCard } from "@/components/EffectiveConfigCard";
import { WorkspaceList } from "@/components/WorkspaceList";
import { useTicketActions } from "@/lib/ticketActions";
import { cn } from "@/lib/cn";
import { ticketStatusKind, runStatusKind, formatUsd } from "@/lib/status";
import { durationBetween, relativeTime } from "@/lib/time";

/**
 * Right-rail peek: a fast, dismissible summary of the selected ticket that does
 * NOT navigate. Full navigation is an explicit action (the Open button, Enter,
 * or cmd/middle-click on the card). The panel prioritizes the run-relevant
 * configuration (profile, effective runtime, workspaces, scheduling) so the
 * "is this ready to run?" judgment is possible without opening the ticket. The
 * prompt is bounded to a few lines with an expand-to-edit popout.
 */
export function TicketPeek({
  ticket,
  project,
  latestRun,
  projects,
  labels,
  onClose,
  onOpenFull,
}: {
  ticket: TicketOut;
  project?: ProjectOut;
  latestRun?: RunOut;
  projects: ProjectOut[];
  labels: LabelOut[];
  onClose: () => void;
  onOpenFull: () => void;
}) {
  const qc = useQueryClient();
  const actions = useTicketActions();
  const profiles = useProfiles();
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: qk.tickets.all });
    qc.invalidateQueries({ queryKey: qk.tickets.detail(ticket.id) });
  };

  const setLabels = (ids: string[]) =>
    labelsApi
      .setTicketLabels(ticket.id, ids)
      .then(invalidate)
      .catch(() => toast.error("Could not update labels"));

  const needsProfile = !ticket.profile_id;

  return (
    <aside
      className="fade-in fixed bottom-3 right-0 top-14 z-30 flex w-[440px] max-w-[92vw] flex-col overflow-hidden rounded-bl-card border-b border-l border-ink-700 bg-ink-900 shadow-[var(--shadow-pop)]"
      aria-label="Ticket preview"
    >
      {/* Header */}
      <div className="flex items-center gap-2 border-b border-ink-700 px-4 py-3">
        {ticket.status === "running" ? (
          <StatusPill status="running" />
        ) : (
          <StatusPill status={ticketStatusKind(ticket.status)} />
        )}
        <span className="font-mono text-xs text-moon-600">{ticket.id.slice(0, 8)}</span>
        <div className="ml-auto flex items-center gap-1">
          <Tooltip content="Open full ticket">
            <button
              onClick={onOpenFull}
              aria-label="Open full ticket"
              className="rounded-control p-1.5 text-moon-400 hover:bg-ink-800 hover:text-moon-100"
            >
              <ArrowRight size={16} />
            </button>
          </Tooltip>
          <Tooltip content="Close (Esc)">
            <button
              onClick={onClose}
              aria-label="Close preview"
              className="rounded-control p-1.5 text-moon-400 hover:bg-ink-800 hover:text-moon-100"
            >
              <X size={16} />
            </button>
          </Tooltip>
        </div>
      </div>

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-4 py-4">
        <button onClick={onOpenFull} className="block w-full text-left">
          <h2 className="font-display text-base font-semibold leading-snug text-moon-100 hover:text-lamp">
            {ticket.title}
          </h2>
        </button>

        {/* Quick actions */}
        <div className="flex flex-wrap gap-1.5">
          <StatusActions ticket={ticket} actions={actions} />
          <Button size="sm" variant="subtle" trailingIcon={<ArrowRight size={13} />} onClick={onOpenFull}>
            Open
          </Button>
        </div>

        {/* Editable metadata */}
        <div className="grid grid-cols-[76px_1fr] items-center gap-x-3 gap-y-2 text-sm">
          <Label>Priority</Label>
          <div>
            <PriorityPicker value={ticket.priority} onChange={(v) => actions.setPriority(ticket, v)} />
          </div>

          <Label>Profile</Label>
          <div>
            <ProfilePicker
              value={ticket.profile_id}
              profiles={profiles.data ?? []}
              onChange={(pid) =>
                ticketsApi
                  .setProfile(ticket.id, pid)
                  .then(invalidate)
                  .catch(() => toast.error("Could not set profile"))
              }
            />
            {needsProfile && (
              <p className="mt-0.5 text-[10px] text-warn">Required to run</p>
            )}
          </div>

          <Label>Project</Label>
          <div>
            <ProjectPicker
              value={ticket.project_id}
              projects={projects}
              onChange={(id) => actions.setProject(ticket, id)}
            />
          </div>

          <Label>Labels</Label>
          <div>
            <LabelPicker value={ticket.labels.map((l) => l.id)} labels={labels} onChange={setLabels}>
              <div className="flex flex-wrap gap-1">
                {ticket.labels.length === 0 ? (
                  <span className="text-xs text-moon-600">Add labels</span>
                ) : (
                  ticket.labels.map((l) => (
                    <span
                      key={l.id}
                      className="rounded-full px-1.5 py-0.5 text-[10px] font-medium text-ink-950"
                      style={{ backgroundColor: l.color }}
                    >
                      {l.name}
                    </span>
                  ))
                )}
              </div>
            </LabelPicker>
          </div>
        </div>

        {/* Effective runtime — model, permission mode, backend, network, and the
            full resolved config (with per-field origin) one click away. */}
        <EffectiveConfigCard ticketId={ticket.id} />

        {/* Scheduling — only surfaced when it affects the next run. */}
        <Scheduling ticket={ticket} />

        {/* Workspaces — where the run lands. */}
        <section>
          <SectionLabel>Workspaces</SectionLabel>
          <WorkspaceList workspaces={ticket.workspaces} />
        </section>

        {/* Additional directories (read-only here; edit on the full ticket). */}
        {ticket.additional_dirs.length > 0 && (
          <section>
            <SectionLabel>Additional directories</SectionLabel>
            <div className="space-y-1">
              {ticket.additional_dirs.map((d) => (
                <div
                  key={d.path}
                  className="flex items-center gap-2 rounded-control border border-ink-700 bg-ink-900 px-2 py-1.5"
                >
                  <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-moon-100">
                    {d.path}
                  </span>
                  <span className="text-[10px] text-moon-600">{d.mode ?? "rw"}</span>
                </div>
              ))}
            </div>
          </section>
        )}

        {/* Prompt — bounded, with an expand-to-edit popout. */}
        <PromptSection ticket={ticket} />

        {/* Latest run */}
        {latestRun && (
          <section>
            <SectionLabel>Latest run</SectionLabel>
            <div
              className={cn(
                "flex flex-wrap items-center gap-x-3 gap-y-1 rounded-control border p-3 font-mono text-[11px] text-moon-400",
                runStatusKind(latestRun.exit_status) === "failed"
                  ? "border-failed/30 bg-failed/[0.05]"
                  : "border-ink-700 bg-ink-950/40",
              )}
            >
              <StatusPill status={runStatusKind(latestRun.exit_status)} />
              {latestRun.model_used && (
                <span className="inline-flex items-center gap-1">
                  <Cpu size={11} /> {latestRun.model_used}
                </span>
              )}
              <span>{durationBetween(latestRun.started_at, latestRun.finished_at)}</span>
              {latestRun.cost_usd != null && (
                <span className="tabular-nums text-lamp">{formatUsd(latestRun.cost_usd)}</span>
              )}
              <span className="text-moon-600">{relativeTime(latestRun.started_at)}</span>
            </div>
          </section>
        )}

        {/* Footer meta */}
        <div className="border-t border-ink-700/60 pt-3 text-[11px] text-moon-600">
          {project && (
            <div>
              project <span className="text-moon-400">{project.name}</span>
            </div>
          )}
          <div>created {relativeTime(ticket.created_at)}</div>
          <div>updated {relativeTime(ticket.updated_at)}</div>
          <div className="mt-1 font-mono text-moon-600/70">{ticket.id}</div>
        </div>
      </div>
    </aside>
  );
}

function Label({ children }: { children: React.ReactNode }) {
  return <span className="text-xs text-moon-600">{children}</span>;
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-moon-600">
      {children}
    </div>
  );
}

/** run_now / schedule / commit-on-finish, shown only when set so the panel stays
 *  quiet for the common default. */
function Scheduling({ ticket }: { ticket: TicketOut }) {
  const bits: React.ReactNode[] = [];
  if (ticket.run_now) {
    bits.push(
      <span
        key="run-now"
        className="inline-flex items-center gap-1 rounded-full border border-lamp/40 bg-lamp/10 px-2 py-0.5 text-[11px] text-lamp"
      >
        <Zap size={11} /> Run now
      </span>,
    );
  }
  if (ticket.scheduled_after) {
    bits.push(
      <span
        key="scheduled"
        className="inline-flex items-center gap-1 rounded-full border border-ink-700 bg-ink-800 px-2 py-0.5 text-[11px] text-moon-100"
      >
        <CalendarClock size={11} className="text-moon-600" /> {relativeTime(ticket.scheduled_after)}
      </span>,
    );
  }
  if (ticket.commit_on_finish != null) {
    bits.push(
      <span
        key="commit"
        className="inline-flex items-center gap-1 rounded-full border border-ink-700 bg-ink-800 px-2 py-0.5 text-[11px] text-moon-100"
      >
        commit on finish: {ticket.commit_on_finish ? "yes" : "no"}
      </span>,
    );
  }
  if (bits.length === 0) return null;
  return (
    <section>
      <SectionLabel>Scheduling</SectionLabel>
      <div className="flex flex-wrap gap-1.5">{bits}</div>
    </section>
  );
}

/** Prompt bounded to a few lines with internal scroll; an expand affordance opens
 *  a full-height, editable popout backed by the ticket update mutation. */
function PromptSection({ ticket }: { ticket: TicketOut }) {
  const [open, setOpen] = useState(false);
  const hasPrompt = ticket.prompt.trim().length > 0;
  return (
    <section>
      <div className="mb-1 flex items-center gap-2">
        <SectionLabel>Prompt</SectionLabel>
        <button
          onClick={() => setOpen(true)}
          className="ml-auto -mt-1 inline-flex items-center gap-1 rounded-control px-1.5 py-0.5 text-[11px] text-moon-400 hover:bg-ink-800 hover:text-moon-100"
        >
          <Maximize2 size={11} /> {hasPrompt ? "Expand & edit" : "Write"}
        </button>
      </div>
      {hasPrompt ? (
        <button
          onClick={() => setOpen(true)}
          className="block max-h-32 w-full overflow-y-auto rounded-control border border-ink-700 bg-ink-950/40 p-3 text-left text-[13px] leading-relaxed text-moon-100 hover:border-ink-700/80"
        >
          <span className="whitespace-pre-wrap">{ticket.prompt}</span>
        </button>
      ) : (
        <button
          onClick={() => setOpen(true)}
          className="text-sm italic text-moon-600 hover:text-moon-400"
        >
          No prompt yet — click to write one.
        </button>
      )}
      <PromptDialog ticket={ticket} open={open} onOpenChange={setOpen} />
    </section>
  );
}

function PromptDialog({
  ticket,
  open,
  onOpenChange,
}: {
  ticket: TicketOut;
  open: boolean;
  onOpenChange: (v: boolean) => void;
}) {
  const update = useUpdateTicket(ticket.id);
  const [value, setValue] = useState(ticket.prompt);
  // Re-sync when a different prompt loads or the dialog reopens after an edit.
  useEffect(() => {
    if (open) setValue(ticket.prompt);
  }, [open, ticket.prompt]);

  const dirty = value !== ticket.prompt;

  const save = () => {
    update.mutate(
      { prompt: value },
      {
        onSuccess: () => onOpenChange(false),
        onError: (err) => toast.error("Could not save prompt", { error: err }),
      },
    );
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Prompt"
      description={ticket.title}
      size="lg"
      footer={
        <>
          <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            variant="primary"
            size="sm"
            disabled={!dirty || update.isPending}
            onClick={save}
          >
            {update.isPending ? "Saving…" : "Save prompt"}
          </Button>
        </>
      }
    >
      <Textarea
        autoFocus
        className="max-h-[60vh] min-h-[320px]"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="Describe the work for this ticket…"
      />
    </Dialog>
  );
}

function StatusActions({
  ticket,
  actions,
}: {
  ticket: TicketOut;
  actions: ReturnType<typeof useTicketActions>;
}) {
  switch (ticket.status) {
    case "draft":
      return (
        <>
          <Button size="sm" variant="primary" onClick={() => actions.runNow(ticket)}>
            Run now
          </Button>
          <Button size="sm" variant="ghost" onClick={() => actions.sendToInbox(ticket)}>
            Send to inbox
          </Button>
          <Button size="sm" variant="ghost" onClick={() => actions.archive(ticket)}>
            Archive
          </Button>
        </>
      );
    case "queued":
      return (
        <>
          <Button size="sm" variant="primary" onClick={() => actions.runNow(ticket)}>
            Run now
          </Button>
          <Button size="sm" variant="ghost" onClick={() => actions.archive(ticket)}>
            Archive
          </Button>
        </>
      );
    case "running":
      return (
        <Button size="sm" variant="danger" onClick={() => actions.cancel(ticket)}>
          Cancel run
        </Button>
      );
    case "review":
      return (
        <>
          <Button size="sm" variant="primary" onClick={() => actions.requeue(ticket)}>
            Requeue
          </Button>
          <Button size="sm" variant="ghost" onClick={() => actions.archive(ticket)}>
            Archive
          </Button>
        </>
      );
    case "archived":
      return (
        <Button size="sm" variant="ghost" onClick={() => actions.unarchive(ticket)}>
          Restore
        </Button>
      );
    default:
      return null;
  }
}
