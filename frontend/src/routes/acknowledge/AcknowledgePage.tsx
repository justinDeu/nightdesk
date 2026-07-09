import { forwardRef, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { CheckCircle2, CheckSquare, Square } from "lucide-react";
import { Page } from "@/components/Page";
import { Button } from "@/ui/Button";
import { EmptyState } from "@/ui/EmptyState";
import { Kbd } from "@/ui/Kbd";
import { StatusPill } from "@/ui/StatusPill";
import { useAckDigest, useAcknowledge, useBulkAck } from "@/api/ack";
import { useProjectMap } from "@/api/projects";
import type { AckDigestGroup, AckDigestTicket } from "@/api/types";
import { useKeybinds } from "@/lib/keymap";
import { formatUsd } from "@/lib/status";
import { relativeTime } from "@/lib/time";
import { ticketHref } from "@/lib/routes";
import { cn } from "@/lib/cn";

const POLL = 5000;

/** Flatten groups into one ordered ticket list for cursor/selection, keeping a
 *  back-pointer to each ticket's group for group-scoped actions. */
function flatten(groups: AckDigestGroup[]): { ticket: AckDigestTicket; group: AckDigestGroup }[] {
  const out: { ticket: AckDigestTicket; group: AckDigestGroup }[] = [];
  for (const group of groups) for (const ticket of group.tickets) out.push({ ticket, group });
  return out;
}

function dayLabel(day: string): string {
  const d = new Date(`${day}T00:00:00`);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const diff = Math.round((today.getTime() - d.getTime()) / 86_400_000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}

export function AcknowledgePage() {
  const navigate = useNavigate();
  const projects = useProjectMap();
  const digest = useAckDigest(null, { refetchInterval: POLL });
  const acknowledge = useAcknowledge();
  const bulkAck = useBulkAck();

  const groups = digest.data?.groups ?? [];
  const flat = useMemo(() => flatten(groups), [groups]);

  const [cursor, setCursor] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  useEffect(() => {
    if (cursor >= flat.length) setCursor(Math.max(0, flat.length - 1));
  }, [flat.length, cursor]);

  // Drop selections for tickets that have left the digest (acked elsewhere).
  useEffect(() => {
    const live = new Set(flat.map((f) => f.ticket.ticket_id));
    setSelected((prev) => {
      const next = new Set([...prev].filter((id) => live.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [flat]);

  const rowRefs = useRef<(HTMLDivElement | null)[]>([]);
  useEffect(() => {
    rowRefs.current[cursor]?.scrollIntoView({ block: "nearest" });
  }, [cursor]);

  const focused = flat[cursor];
  const before = digest.data?.generated_at;

  const ackIds = (ids: string[]) => {
    if (ids.length === 0) return;
    bulkAck.mutate({ ticket_ids: ids });
    setSelected(new Set());
  };
  const ackSelectedOrFocused = () => {
    if (selected.size > 0) ackIds([...selected]);
    else if (focused) acknowledge.mutate(focused.ticket.ticket_id);
  };
  const ackGroup = (group: AckDigestGroup) => {
    if (group.project_id != null) {
      bulkAck.mutate({ project_scope: true, project_id: group.project_id, before });
    } else {
      // No-project group: ack its tickets by id (project_id null is a valid
      // scope, but only within this day's slice — ids are the precise set).
      ackIds(group.tickets.map((t) => t.ticket_id));
    }
  };
  const ackAll = () => {
    for (const group of groups) ackGroup(group);
    setSelected(new Set());
  };
  const toggleSelect = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  const openFocused = () =>
    focused && navigate({ to: "/tickets/$id", params: { id: focused.ticket.ticket_id } });

  useKeybinds([
    { combo: "j", label: "Cursor down", group: "Acknowledge", handler: () => setCursor((c) => Math.min(flat.length - 1, c + 1)) },
    { combo: "k", label: "Cursor up", group: "Acknowledge", handler: () => setCursor((c) => Math.max(0, c - 1)) },
    { combo: "x", label: "Select", group: "Acknowledge", handler: () => focused && toggleSelect(focused.ticket.ticket_id) },
    { combo: "e", label: "Acknowledge", group: "Acknowledge", handler: ackSelectedOrFocused },
    { combo: "a", label: "Acknowledge", group: "Acknowledge", handler: ackSelectedOrFocused },
    { combo: "shift+e", label: "Acknowledge group", group: "Acknowledge", handler: () => focused && ackGroup(focused.group) },
    { combo: "o", label: "Open ticket", group: "Acknowledge", handler: openFocused },
    { combo: "Enter", label: "Open ticket", group: "Acknowledge", handler: openFocused },
  ]);

  const total = digest.data?.total ?? 0;
  const projName = (id: string | null) => (id ? projects.get(id)?.name ?? "No project" : "No project");

  let flatIndex = -1;
  return (
    <Page
      title="To acknowledge"
      subtitle="Everything an agent finished or archived that you have not yet seen. Read it, dismiss it in batches."
      width="wide"
      actions={
        total > 0 ? (
          <Button variant="primary" leadingIcon={<CheckCircle2 size={15} />} onClick={ackAll}>
            Ack all ({total})
          </Button>
        ) : undefined
      }
    >
      {total > 0 && (
        <div className="mb-4 hidden items-center gap-1.5 text-xs text-moon-600 md:flex">
          <Kbd>j</Kbd>
          <Kbd>k</Kbd> move
          <Kbd>x</Kbd> select
          <Kbd>e</Kbd> ack
          <Kbd>shift</Kbd>
          <Kbd>e</Kbd> ack group
          <Kbd>o</Kbd> open
        </div>
      )}

      {total === 0 ? (
        <EmptyState
          icon={<CheckCircle2 size={18} />}
          title="All caught up"
          description="No unacknowledged work. Agent-archived and agent-reviewed tickets land here so you can dismiss a whole shift in one read."
        />
      ) : (
        <div className="space-y-6">
          {groups.map((group) => (
            <section key={`${group.project_id ?? "none"}-${group.day}`}>
              <div className="mb-2 flex items-center gap-2">
                <h2 className="font-display text-xs font-semibold uppercase tracking-wide text-moon-400">
                  {projName(group.project_id)} · {dayLabel(group.day)}
                </h2>
                <span className="rounded-full bg-ink-800 px-1.5 py-0.5 font-mono text-[10px] text-moon-400">
                  {group.count}
                </span>
                <span className="text-xs text-moon-600">
                  {group.succeeded} ok
                  {group.failed > 0 && <span className="text-failed"> · {group.failed} failed</span>}
                  {group.cost_usd > 0 && <span> · {formatUsd(group.cost_usd)}</span>}
                </span>
                <Button size="sm" variant="ghost" className="ml-auto" onClick={() => ackGroup(group)}>
                  Ack group
                </Button>
              </div>
              <div className="space-y-1.5">
                {group.tickets.map((t) => {
                  flatIndex += 1;
                  const i = flatIndex;
                  return (
                    <DigestRow
                      key={t.ticket_id}
                      ref={(el) => (rowRefs.current[i] = el)}
                      ticket={t}
                      projectName={projName(t.project_id)}
                      focused={i === cursor}
                      selected={selected.has(t.ticket_id)}
                      onFocus={() => setCursor(i)}
                      onToggle={() => toggleSelect(t.ticket_id)}
                      onOpen={() => navigate({ to: "/tickets/$id", params: { id: t.ticket_id } })}
                      onAck={() => acknowledge.mutate(t.ticket_id)}
                    />
                  );
                })}
              </div>
            </section>
          ))}
        </div>
      )}
    </Page>
  );
}

const DigestRow = forwardRef<
  HTMLDivElement,
  {
    ticket: AckDigestTicket;
    projectName: string;
    focused: boolean;
    selected: boolean;
    onFocus: () => void;
    onToggle: () => void;
    onOpen: () => void;
    onAck: () => void;
  }
>(function DigestRow({ ticket, projectName, focused, selected, onFocus, onToggle, onOpen, onAck }, ref) {
  return (
    <div
      ref={ref}
      onMouseEnter={onFocus}
      className={cn(
        "group flex items-center gap-3 rounded-control border px-3 py-2.5 transition-colors",
        focused ? "border-lamp/40 bg-ink-800" : "border-ink-700 bg-ink-900 hover:bg-ink-800",
      )}
    >
      <button
        onClick={onToggle}
        aria-label={selected ? "Deselect" : "Select"}
        className="shrink-0 text-moon-500 hover:text-moon-200"
      >
        {selected ? <CheckSquare size={16} className="text-lamp" /> : <Square size={16} />}
      </button>
      {ticket.outcome === "failed" ? (
        <StatusPill status="failed" label="Failed" />
      ) : (
        <StatusPill status={ticket.status === "archived" ? "archived" : "review"} />
      )}
      <a
        href={ticketHref(ticket.ticket_id)}
        onClick={(e) => {
          if (e.metaKey || e.ctrlKey) return;
          e.preventDefault();
          onOpen();
        }}
        className="min-w-0 flex-1 rounded-[4px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lamp"
      >
        <span className="block truncate text-sm font-medium text-moon-100 group-hover:text-lamp">
          {ticket.description?.trim() || ticket.title}
        </span>
        <span className="mt-0.5 flex flex-wrap items-center gap-x-2 text-xs text-moon-600">
          <span>{projectName}</span>
          {ticket.actor_kind && ticket.actor_kind !== "admin" && <span>agent</span>}
          {ticket.cost_usd != null && (
            <span className="font-mono tabular-nums">{formatUsd(ticket.cost_usd)}</span>
          )}
          {ticket.entered_at && <span>{relativeTime(ticket.entered_at)}</span>}
        </span>
      </a>
      <Button size="sm" variant="subtle" onClick={onAck} className="shrink-0">
        Ack
      </Button>
    </div>
  );
});
