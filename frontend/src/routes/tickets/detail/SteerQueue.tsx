import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  draggable,
  dropTargetForElements,
  monitorForElements,
} from "@atlaskit/pragmatic-drag-and-drop/element/adapter";
import { Check, GripVertical, Loader2, Pencil, Send, X } from "lucide-react";
import type { SteerMessageOut } from "@/api/types";
import { ticketsApi } from "@/api/tickets";
import { qk } from "@/api";
import { Tooltip } from "@/ui/Tooltip";
import { Textarea } from "@/ui/Input";
import { Button } from "@/ui/Button";
import { toast } from "@/ui/Toast";
import { cn } from "@/lib/cn";

/**
 * The live-run steer queue: the follow-ups the user has typed while a run is in
 * flight, shown above the composer. Pending chips are editable, deletable, and
 * drag-reorderable; a delivering chip is locked with a spinner. Delivered and
 * cancelled messages drop off the list (the delivery shows as a breadcrumb in
 * the transcript). Mirrors the board's pragmatic-drag-and-drop approach — no new
 * dnd dependency.
 */
export function SteerQueue({
  ticketId,
  messages,
  inject,
  onChange,
}: {
  ticketId: string;
  messages: SteerMessageOut[];
  inject: boolean;
  onChange: () => void;
}) {
  const qc = useQueryClient();
  const [order, setOrder] = useState<SteerMessageOut[]>(messages);
  const [draggingId, setDraggingId] = useState<string | null>(null);

  // Reconcile local order from the server whenever the query refetches.
  useEffect(() => setOrder(messages), [messages]);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: qk.tickets.steer(ticketId) });
    onChange();
  };

  useEffect(() => {
    return monitorForElements({
      onDragStart: ({ source }) => setDraggingId(String(source.data.messageId ?? "")),
      onDrop: ({ source, location }) => {
        setDraggingId(null);
        const targets = location.current.dropTargets;
        if (targets.length === 0) return;
        const movingId = String(source.data.messageId ?? "");
        const inner = targets[0];
        const overId = String(inner.data.messageId ?? "");
        if (!movingId || !overId || movingId === overId) return;

        const arr = [...order];
        const from = arr.findIndex((m) => m.id === movingId);
        const overIdx = arr.findIndex((m) => m.id === overId);
        if (from < 0 || overIdx < 0) return;
        const rect = inner.element.getBoundingClientRect();
        const after = location.current.input.clientY > rect.top + rect.height / 2;
        let to = overIdx + (after ? 1 : 0);
        const [moving] = arr.splice(from, 1);
        if (from < to) to -= 1;
        arr.splice(to, 0, moving);
        if (arr.every((m, i) => m.id === order[i]?.id)) return; // no-op

        setOrder(arr); // optimistic
        ticketsApi
          .steerReorder(ticketId, arr.map((m) => m.id))
          .then(invalidate)
          .catch((err) => {
            setOrder(messages); // revert
            toast.error("Reorder failed", { error: err });
          });
      },
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [order, messages, ticketId]);

  if (order.length === 0) return null;

  return (
    <div className="mb-2 space-y-1.5">
      <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-moon-500">
        <Send size={11} className="text-dawn" />
        Steer queue
        <span className="text-moon-600">· {order.length}</span>
        <span className="ml-auto font-normal normal-case text-moon-600">
          {inject ? "Delivered to the running agent" : "Sent as the next turn when this run finishes"}
        </span>
      </div>
      {order.map((m) => (
        <SteerChip
          key={m.id}
          message={m}
          dragging={draggingId === m.id}
          onChange={invalidate}
          ticketId={ticketId}
        />
      ))}
    </div>
  );
}

function SteerChip({
  ticketId,
  message,
  dragging,
  onChange,
}: {
  ticketId: string;
  message: SteerMessageOut;
  dragging: boolean;
  onChange: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(message.body);
  const [busy, setBusy] = useState(false);
  const locked = message.state !== "pending";

  useEffect(() => setDraft(message.body), [message.body]);

  useEffect(() => {
    const el = ref.current;
    if (!el || locked || editing) return;
    const data = { messageId: message.id, type: "steer" };
    return combine(
      draggable({ element: el, getInitialData: () => data }),
      dropTargetForElements({ element: el, getData: () => data }),
    );
  }, [message.id, locked, editing]);

  async function run(label: string, fn: () => Promise<unknown>) {
    setBusy(true);
    try {
      await fn();
      onChange();
    } catch (err) {
      toast.error(`${label} failed`, { error: err });
    } finally {
      setBusy(false);
    }
  }

  if (editing) {
    return (
      <div className="rounded-control border border-dawn/40 bg-ink-950 p-2">
        <Textarea
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          className="min-h-[52px] text-[12px]"
        />
        <div className="mt-1.5 flex items-center gap-2">
          <Button
            size="sm"
            variant="primary"
            leadingIcon={<Check size={12} />}
            disabled={busy || !draft.trim() || draft === message.body}
            onClick={() =>
              run("Edit", () => ticketsApi.steerEdit(ticketId, message.id, draft)).then(() =>
                setEditing(false),
              )
            }
          >
            Save
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setDraft(message.body);
              setEditing(false);
            }}
          >
            Cancel
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div
      ref={ref}
      className={cn(
        "group flex items-start gap-2 rounded-control border px-2 py-1.5 transition-opacity",
        locked
          ? "border-dawn/40 bg-dawn/[0.06]"
          : "border-ink-700 bg-ink-950 hover:border-ink-600",
        dragging && "opacity-40",
      )}
    >
      {locked ? (
        <Loader2 size={13} className="mt-0.5 shrink-0 text-dawn motion-safe:animate-spin" />
      ) : (
        <span
          className="mt-0.5 shrink-0 cursor-grab text-moon-600 active:cursor-grabbing"
          aria-hidden
        >
          <GripVertical size={13} />
        </span>
      )}
      <p className="min-w-0 flex-1 whitespace-pre-wrap break-words text-[12px] leading-snug text-moon-100 line-clamp-2">
        {message.body}
      </p>
      {locked ? (
        <span className="mt-0.5 shrink-0 text-[10px] font-medium uppercase tracking-wide text-dawn">
          Sending
        </span>
      ) : (
        <div className="flex shrink-0 items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
          <Tooltip content="Edit">
            <button
              onClick={() => setEditing(true)}
              disabled={busy}
              aria-label="Edit queued message"
              className="rounded-control p-1 text-moon-400 hover:bg-ink-800 hover:text-moon-100"
            >
              <Pencil size={12} />
            </button>
          </Tooltip>
          <Tooltip content="Remove">
            <button
              onClick={() => run("Remove", () => ticketsApi.steerCancel(ticketId, message.id))}
              disabled={busy}
              aria-label="Remove queued message"
              className="rounded-control p-1 text-moon-400 hover:bg-ink-800 hover:text-failed"
            >
              <X size={12} />
            </button>
          </Tooltip>
        </div>
      )}
    </div>
  );
}

/** Combine multiple cleanup-returning registrations (mirrors Board.tsx). */
function combine(...cleanups: (() => void)[]): () => void {
  return () => cleanups.forEach((c) => c());
}
