import { useEffect, useMemo, useRef, useState } from "react";
import {
  draggable,
  dropTargetForElements,
  monitorForElements,
} from "@atlaskit/pragmatic-drag-and-drop/element/adapter";
import { useQueryClient } from "@tanstack/react-query";
import { qk } from "@/api";
import { ticketsApi } from "@/api/tickets";
import type { ProjectOut, RunOut, TicketOut, TicketStatus } from "@/api/types";
import { BoardCard } from "./BoardCard";
import { orderComparator, type OrderBy } from "./displayModel";
import { toast } from "@/ui/Toast";
import { cn } from "@/lib/cn";

const COLUMNS: { status: TicketStatus; label: string }[] = [
  { status: "draft", label: "Draft" },
  { status: "queued", label: "Queued" },
  { status: "running", label: "Running" },
  { status: "review", label: "Review" },
];

type Cols = Record<string, TicketOut[]>;

function groupCols(tickets: TicketOut[], cmp: (a: TicketOut, b: TicketOut) => number): Cols {
  const cols: Cols = { draft: [], queued: [], running: [], review: [] };
  for (const t of tickets) {
    if (cols[t.status]) cols[t.status].push(t);
  }
  for (const s of Object.keys(cols)) cols[s].sort(cmp);
  return cols;
}

export interface BoardProps {
  tickets: TicketOut[];
  projects: Map<string, ProjectOut>;
  latestRun: Map<string, RunOut>;
  orderBy: OrderBy;
  /** Plain click — open the side peek. */
  onSelect: (id: string) => void;
  /** Explicit open — full navigation. */
  onOpen: (id: string) => void;
  onRangeSelect: (id: string) => void;
  onToggleSelect: (id: string) => void;
  selected: Set<string>;
  focusedId?: string;
  peekedId?: string;
  /** The side peek is open — columns drop their min-width floor so all four
   *  reflow into the remaining width instead of overflowing behind the peek. */
  peekOpen?: boolean;
}

export function Board({
  tickets,
  projects,
  latestRun,
  orderBy,
  onSelect,
  onOpen,
  onRangeSelect,
  onToggleSelect,
  selected,
  focusedId,
  peekedId,
  peekOpen,
}: BoardProps) {
  const qc = useQueryClient();
  const cmp = useMemo(() => orderComparator(orderBy, latestRun), [orderBy, latestRun]);
  const [cols, setCols] = useState<Cols>(() => groupCols(tickets, cmp));
  const [draggingId, setDraggingId] = useState<string | null>(null);

  // Re-sync from server truth whenever the ticket set / ordering changes.
  useEffect(() => {
    setCols(groupCols(tickets, cmp));
  }, [tickets, cmp]);

  const invalidate = () => qc.invalidateQueries({ queryKey: qk.tickets.all });

  useEffect(() => {
    return monitorForElements({
      onDragStart: ({ source }) => setDraggingId(String(source.data.ticketId ?? "")),
      onDrop: ({ source, location }) => {
        setDraggingId(null);
        const targets = location.current.dropTargets;
        if (targets.length === 0) return;
        const ticketId = String(source.data.ticketId ?? "");
        const fromStatus = String(source.data.status ?? "");
        if (!ticketId) return;

        // Innermost target is a card or a column.
        const inner = targets[0];
        const toStatus = String(inner.data.status);
        let toIndex: number;
        if (inner.data.type === "card") {
          const rect = inner.element.getBoundingClientRect();
          const after = location.current.input.clientY > rect.top + rect.height / 2;
          toIndex = Number(inner.data.index) + (after ? 1 : 0);
        } else {
          toIndex = cols[toStatus]?.length ?? 0;
        }

        applyMove(ticketId, fromStatus, toStatus, toIndex);
      },
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cols]);

  function applyMove(
    ticketId: string,
    fromStatus: string,
    toStatus: string,
    toIndex: number,
  ) {
    const moving = cols[fromStatus]?.find((t) => t.id === ticketId);
    if (!moving) return;
    if (fromStatus === toStatus) {
      const arr = [...cols[fromStatus]];
      const from = arr.findIndex((t) => t.id === ticketId);
      if (from === toIndex || from === toIndex - 1) return; // no-op
      arr.splice(from, 1);
      arr.splice(from < toIndex ? toIndex - 1 : toIndex, 0, moving);
      setCols((c) => ({ ...c, [fromStatus]: arr }));
      ticketsApi
        .reorder({ status: fromStatus as TicketStatus, ticket_ids: arr.map((t) => t.id) })
        .then(invalidate)
        .catch((err) => {
          toast.error("Reorder failed", { error: err });
          invalidate();
        });
      return;
    }

    // Cross-column: optimistic move, then transition with target position.
    const src = cols[fromStatus].filter((t) => t.id !== ticketId);
    const dst = [...(cols[toStatus] ?? [])];
    dst.splice(Math.min(toIndex, dst.length), 0, { ...moving, status: toStatus });
    setCols((c) => ({ ...c, [fromStatus]: src, [toStatus]: dst }));
    ticketsApi
      .transition(ticketId, {
        status: toStatus as TicketStatus,
        position: Math.min(toIndex, dst.length - 1),
      })
      .then(() => {
        if (toStatus === "running") toast.success("Run dispatched");
        invalidate();
      })
      .catch((err) => {
        toast.error(`Can't move to ${toStatus}`, { error: err });
        invalidate();
      });
  }

  return (
    <div className="flex h-full gap-3 overflow-x-auto pb-4">
      {COLUMNS.map((col) => (
        <Column
          key={col.status}
          status={col.status}
          label={col.label}
          tickets={cols[col.status] ?? []}
          projects={projects}
          latestRun={latestRun}
          onSelect={onSelect}
          onOpen={onOpen}
          onRangeSelect={onRangeSelect}
          onToggleSelect={onToggleSelect}
          selected={selected}
          draggingId={draggingId}
          focusedId={focusedId}
          peekedId={peekedId}
          peekOpen={peekOpen}
        />
      ))}
    </div>
  );
}

function Column({
  status,
  label,
  tickets,
  projects,
  latestRun,
  onSelect,
  onOpen,
  onRangeSelect,
  onToggleSelect,
  selected,
  draggingId,
  focusedId,
  peekedId,
  peekOpen,
}: {
  status: TicketStatus;
  label: string;
  tickets: TicketOut[];
  projects: Map<string, ProjectOut>;
  latestRun: Map<string, RunOut>;
  onSelect: (id: string) => void;
  onOpen: (id: string) => void;
  onRangeSelect: (id: string) => void;
  onToggleSelect: (id: string) => void;
  selected: Set<string>;
  draggingId: string | null;
  focusedId?: string;
  peekedId?: string;
  peekOpen?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [over, setOver] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    return dropTargetForElements({
      element: el,
      getData: () => ({ type: "column", status }),
      onDragEnter: () => setOver(true),
      onDragLeave: () => setOver(false),
      onDrop: () => setOver(false),
    });
  }, [status]);

  const running = status === "running";

  return (
    <div className={cn("flex w-0 min-h-0 flex-1 flex-col", peekOpen ? "min-w-0" : "min-w-[240px]")}>
      <div className="mb-2 flex items-center gap-2 px-1">
        <span
          className={cn(
            "font-display text-xs font-semibold uppercase tracking-wide",
            running ? "dawn-text" : "text-moon-400",
          )}
        >
          {label}
        </span>
        <span className="rounded-full bg-ink-800 px-1.5 py-0.5 font-mono text-[10px] text-moon-600">
          {tickets.length}
        </span>
      </div>
      <div
        ref={ref}
        className={cn(
          "flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto overscroll-contain rounded-card border border-dashed p-2 transition-colors",
          over ? "border-lamp/40 bg-ink-900/60" : "border-ink-700/60 bg-ink-950/30",
        )}
      >
        {tickets.map((t, i) => (
          <DraggableCard
            key={t.id}
            ticket={t}
            index={i}
            project={t.project_id ? projects.get(t.project_id) : undefined}
            latestRun={latestRun.get(t.id)}
            onSelect={() => onSelect(t.id)}
            onOpen={() => onOpen(t.id)}
            onRangeSelect={() => onRangeSelect(t.id)}
            onToggleSelect={() => onToggleSelect(t.id)}
            selected={selected.has(t.id)}
            dragging={draggingId === t.id}
            focused={focusedId === t.id}
            peeked={peekedId === t.id}
          />
        ))}
        {tickets.length === 0 && (
          <p className="px-2 py-6 text-center text-xs text-moon-600">Drop here</p>
        )}
      </div>
    </div>
  );
}

function DraggableCard({
  ticket,
  index,
  project,
  latestRun,
  onSelect,
  onOpen,
  onRangeSelect,
  onToggleSelect,
  selected,
  dragging,
  focused,
  peeked,
}: {
  ticket: TicketOut;
  index: number;
  project?: ProjectOut;
  latestRun?: RunOut;
  onSelect: () => void;
  onOpen: () => void;
  onRangeSelect: () => void;
  onToggleSelect: () => void;
  selected: boolean;
  dragging: boolean;
  focused: boolean;
  peeked: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const data = { ticketId: ticket.id, status: ticket.status, index, type: "card" };
    return combine(
      draggable({ element: el, getInitialData: () => data }),
      dropTargetForElements({ element: el, getData: () => data }),
    );
  }, [ticket.id, ticket.status, index]);

  return (
    <BoardCard
      ref={ref}
      ticket={ticket}
      project={project}
      latestRun={latestRun}
      onSelect={onSelect}
      onOpen={onOpen}
      onRangeSelect={onRangeSelect}
      onToggleSelect={onToggleSelect}
      selected={selected}
      dragging={dragging}
      focused={focused}
      peeked={peeked}
    />
  );
}

/** Combine multiple cleanup-returning registrations. */
function combine(...cleanups: (() => void)[]): () => void {
  return () => cleanups.forEach((c) => c());
}
