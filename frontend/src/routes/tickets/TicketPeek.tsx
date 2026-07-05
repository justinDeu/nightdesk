import { useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Cpu, X } from "lucide-react";
import type { LabelOut, ProjectOut, RunOut, TicketOut } from "@/api/types";
import { qk } from "@/api";
import { labelsApi } from "@/api/labels";
import { Button } from "@/ui/Button";
import { StatusPill } from "@/ui/StatusPill";
import { Tooltip } from "@/ui/Tooltip";
import { PriorityPicker, ProjectPicker, LabelPicker } from "@/components/PropertyPickers";
import { useTicketActions } from "@/lib/ticketActions";
import { cn } from "@/lib/cn";
import { ticketStatusKind, runStatusKind, formatUsd } from "@/lib/status";
import { durationBetween, relativeTime } from "@/lib/time";
import { toast } from "@/ui/Toast";

/**
 * Right-rail peek: a fast, dismissible summary of the selected ticket that does
 * NOT navigate. Full navigation is an explicit action (the Open button, Enter,
 * or cmd/middle-click on the card). Editable metadata mirrors the detail page.
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
  const invalidate = () => qc.invalidateQueries({ queryKey: qk.tickets.all });

  const setLabels = (ids: string[]) =>
    labelsApi
      .setTicketLabels(ticket.id, ids)
      .then(invalidate)
      .catch(() => toast.error("Could not update labels"));

  return (
    <aside
      className="fade-in fixed bottom-3 right-0 top-14 z-30 flex w-[420px] max-w-[92vw] flex-col overflow-hidden rounded-bl-card border-b border-l border-ink-700 bg-ink-900 shadow-[var(--shadow-pop)]"
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

        {/* Metadata */}
        <div className="grid grid-cols-[80px_1fr] items-center gap-x-3 gap-y-2 text-sm">
          <span className="text-xs text-moon-600">Priority</span>
          <div>
            <PriorityPicker value={ticket.priority} onChange={(v) => actions.setPriority(ticket, v)} />
          </div>
          <span className="text-xs text-moon-600">Project</span>
          <div>
            <ProjectPicker
              value={ticket.project_id}
              projects={projects}
              onChange={(id) => actions.setProject(ticket, id)}
            />
          </div>
          <span className="text-xs text-moon-600">Labels</span>
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

        {/* Prompt preview */}
        <div>
          <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-moon-600">Prompt</div>
          {ticket.prompt.trim() ? (
            <p className="whitespace-pre-wrap rounded-control border border-ink-700 bg-ink-950/40 p-3 text-[13px] leading-relaxed text-moon-100">
              {ticket.prompt}
            </p>
          ) : (
            <p className="text-sm italic text-moon-600">No prompt yet.</p>
          )}
        </div>

        {/* Latest run */}
        {latestRun && (
          <div>
            <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-moon-600">
              Latest run
            </div>
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
          </div>
        )}

        {project && (
          <p className="text-xs text-moon-600">
            Project: <span className="text-moon-400">{project.name}</span>
          </p>
        )}
      </div>
    </aside>
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
