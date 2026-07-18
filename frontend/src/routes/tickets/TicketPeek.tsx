import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ArrowRight, CalendarClock, Cpu, Maximize2, Zap, X } from "lucide-react";
import type { LabelOut, ProjectOut, RunOut, TicketOut } from "@/api/types";
import { qk } from "@/api";
import { labelsApi } from "@/api/labels";
import { useAcknowledge } from "@/api/ack";
import { ticketNeedsAck } from "@/components/UnackedDot";
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
import { MarkdownSource } from "@/components/MarkdownSource";
import { HighlightedPromptArea } from "@/components/HighlightedPromptArea";
import { useTicketActions } from "@/lib/ticketActions";
import { useUnsavedGuard } from "@/lib/useUnsavedGuard";
import { ticketHref, inAppNav } from "@/lib/routes";
import { cn } from "@/lib/cn";
import { ticketStatusKind, runStatusKind, formatUsd } from "@/lib/status";
import { durationBetween, relativeTime } from "@/lib/time";
import { peekRailClasses } from "./peekLayout";

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

  // Click-outside-to-dismiss. Capture phase + `pointerdown` (not `click`)
  // matters for two reasons:
  // 1. It lets this fire *before* a target's own onClick (React listens in
  //    the bubble phase, well after pointerdown), so when the outside click
  //    is itself "select a different ticket" (a board/list card, a Desk
  //    acknowledge row), both state updates land in the same batch and the
  //    later one (select the new ticket) wins — the peek swaps contents
  //    instead of closing.
  // 2. Opening a Radix dropdown/menu trigger (e.g. the Priority picker)
  //    synchronously locks body scroll, which can shift layout *between*
  //    the trigger's own mousedown and mouseup/click — so a `click`
  //    listener can see a stray event whose target has drifted off the
  //    trigger entirely. `pointerdown` reads the target before that shift,
  //    matching Radix's own dismissable-layer, which also keys off
  //    pointerdown for this exact reason.
  // Interactive pickers/dialogs/tooltips render into a portal outside this
  // <aside>'s subtree, so they're excluded explicitly rather than by DOM
  // containment.
  const rootRef = useRef<HTMLElement>(null);
  useEffect(() => {
    function onDocPointerDown(e: PointerEvent) {
      const target = e.target;
      if (!(target instanceof Element)) return;
      if (rootRef.current?.contains(target)) return;
      if (target.closest('[data-radix-popper-content-wrapper], [role="dialog"], .overlay-in')) return;
      onClose();
    }
    document.addEventListener("pointerdown", onDocPointerDown, true);
    return () => document.removeEventListener("pointerdown", onDocPointerDown, true);
  }, [onClose]);

  const setLabels = (ids: string[]) =>
    labelsApi
      .setTicketLabels(ticket.id, ids)
      .then(invalidate)
      .catch(() => toast.error("Could not update labels"));

  const needsProfile = !ticket.profile_id;

  return (
    <aside ref={rootRef} className={cn(peekRailClasses)} aria-label="Ticket preview">
      {/* Header */}
      <div className="flex items-center gap-2 border-b border-ink-700 px-4 py-3">
        {ticket.status === "running" ? (
          <StatusPill status="running" />
        ) : (
          <StatusPill status={ticketStatusKind(ticket.status)} />
        )}
        <span className="font-mono text-xs text-moon-600">{ticket.id.slice(0, 8)}</span>
        <div className="ml-auto flex items-center gap-1">
          {/* Expand control (ticket 55105222): the peek stays the default,
              dismissible summary; this is the opt-in escape hatch to the full
              ticket page for deep reading (transcript, activity, linked
              items) that the bounded rail can't fit. Navigating to the
              existing detail route beats an in-place full-width takeover
              here — that page is already the fully-built "document +
              evidence" surface, so a takeover would just duplicate it inside
              the peek. */}
          <Tooltip content="Expand to full ticket">
            <a
              href={ticketHref(ticket.id)}
              aria-label="Expand to full ticket"
              onClick={(e) => {
                if (inAppNav(e)) onOpenFull();
              }}
              className="rounded-control p-1.5 text-moon-400 hover:bg-ink-800 hover:text-moon-100"
            >
              <Maximize2 size={16} />
            </a>
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

      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto overscroll-contain px-4 py-4">
        <a
          href={ticketHref(ticket.id)}
          onClick={(e) => {
            if (inAppNav(e)) onOpenFull();
          }}
          className="block w-full text-left"
        >
          <h2 className="font-display text-base font-semibold leading-snug text-moon-100 hover:text-lamp">
            {ticket.title}
          </h2>
        </a>

        {/* Quick actions */}
        <div className="flex flex-wrap gap-1.5">
          <StatusActions ticket={ticket} actions={actions} onClose={onClose} />
          <Button asChild size="sm" variant="subtle">
            <a
              href={ticketHref(ticket.id)}
              onClick={(e) => {
                if (inAppNav(e)) onOpenFull();
              }}
            >
              Open
              <ArrowRight size={13} />
            </a>
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

        {/* Description (human what/why) first-class; the agent prompt is demoted
            below it. */}
        <DescriptionSection ticket={ticket} />

        {/* Agent prompt — bounded, with an expand-to-edit popout. */}
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
                  : runStatusKind(latestRun.exit_status) === "canceled"
                    ? "border-warn/25 bg-warn/[0.05]"
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
/** Description (human what/why) with an inline edit toggle. First-class in the
 *  peek; the agent prompt sits below it. */
function DescriptionSection({ ticket }: { ticket: TicketOut }) {
  const update = useUpdateTicket(ticket.id);
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(ticket.description ?? "");
  useEffect(() => setValue(ticket.description ?? ""), [ticket.description]);
  const dirty = value !== (ticket.description ?? "");
  useUnsavedGuard(editing && dirty);

  const save = () =>
    update.mutate(
      { description: value.trim() },
      {
        onSuccess: () => setEditing(false),
        onError: (err) => toast.error("Could not save description", { error: err }),
      },
    );

  return (
    <section>
      <div className="mb-1 flex items-center gap-2">
        <SectionLabel>Description</SectionLabel>
        {!editing && (
          <button
            onClick={() => setEditing(true)}
            className="ml-auto -mt-1 inline-flex items-center gap-1 rounded-control px-1.5 py-0.5 text-[11px] text-moon-400 hover:bg-ink-800 hover:text-moon-100"
          >
            {ticket.description?.trim() ? "Edit" : "Add"}
          </button>
        )}
      </div>
      {editing ? (
        <div className="space-y-2">
          <Textarea
            autoFocus
            className="min-h-[96px]"
            placeholder="What is this ticket, and why? For a human, not the agent."
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
          <div className="flex gap-2">
            <Button size="sm" variant="primary" onClick={save}>
              Save
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setValue(ticket.description ?? "");
                setEditing(false);
              }}
            >
              Cancel
            </Button>
          </div>
        </div>
      ) : ticket.description?.trim() ? (
        <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-moon-100">
          {ticket.description}
        </p>
      ) : (
        <button
          onClick={() => setEditing(true)}
          className="text-sm italic text-moon-600 hover:text-moon-400"
        >
          No description yet — add the what/why for a human.
        </button>
      )}
    </section>
  );
}

function PromptSection({ ticket }: { ticket: TicketOut }) {
  const [open, setOpen] = useState(false);
  const hasPrompt = ticket.prompt.trim().length > 0;
  return (
    <section>
      <div className="mb-1 flex items-center gap-2">
        <SectionLabel>Agent prompt</SectionLabel>
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
          className="block max-h-32 w-full overflow-y-auto rounded-control border border-ink-700 bg-ink-950/40 p-3 text-left hover:border-ink-700/80"
        >
          <MarkdownSource text={ticket.prompt} className="text-[12px]" />
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

  // Guard navigation / reload while the popout holds unsaved edits, and confirm
  // before an explicit close (Cancel / Esc / overlay / X) discards them.
  useUnsavedGuard(open && dirty);
  const requestClose = (next: boolean) => {
    if (!next && dirty && !window.confirm("Discard your unsaved prompt changes?")) return;
    onOpenChange(next);
  };

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
      onOpenChange={requestClose}
      title="Agent prompt"
      description={ticket.title}
      size="lg"
      footer={
        <>
          <Button variant="ghost" size="sm" onClick={() => requestClose(false)}>
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
      <HighlightedPromptArea
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
  onClose,
}: {
  ticket: TicketOut;
  actions: ReturnType<typeof useTicketActions>;
  onClose: () => void;
}) {
  const acknowledge = useAcknowledge();
  // Settled outcome no human has stamped as seen — the peek is where a
  // surfaced "unacked" marker gets resolved. Acking is a resolving action:
  // the item no longer needs attention, so close the peek once it lands
  // instead of leaving a now-stale panel open.
  const ackButton = ticketNeedsAck(ticket) ? (
    <Button
      size="sm"
      variant="subtle"
      onClick={() => acknowledge.mutate(ticket.id, { onSuccess: onClose })}
    >
      Ack
    </Button>
  ) : null;
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
          {ackButton}
          <Button size="sm" variant="ghost" onClick={() => actions.archive(ticket)}>
            Archive
          </Button>
        </>
      );
    case "archived":
      return (
        <>
          {ackButton}
          <Button size="sm" variant="ghost" onClick={() => actions.unarchive(ticket)}>
            Restore
          </Button>
        </>
      );
    default:
      return null;
  }
}
