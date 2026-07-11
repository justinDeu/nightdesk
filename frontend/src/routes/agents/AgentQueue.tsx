import { useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  draggable,
  dropTargetForElements,
  monitorForElements,
} from "@atlaskit/pragmatic-drag-and-drop/element/adapter";
import { Check, GripVertical, Layers, Loader2, Pencil, X } from "lucide-react";
import { agentsApi, useAgentTurns } from "@/api/agents";
import { qk } from "@/api/keys";
import { Tooltip } from "@/ui/Tooltip";
import { Textarea } from "@/ui/Input";
import { Button } from "@/ui/Button";
import { toast } from "@/ui/Toast";
import { cn } from "@/lib/cn";
import type { AgentTurnOut } from "@/api/types";

/**
 * The agent's pending turn queue: user messages typed while a turn is in flight,
 * shown above the composer. Queued chips are inline-editable, cancelable, and
 * drag-reorderable; a delivering chip locks with a spinner (the host has claimed
 * it — edits/reorder/cancel 409 there). Mirrors the ticket SteerQueue.
 */
export function AgentQueue({
  agentId,
  refetchInterval,
}: {
  agentId: string;
  refetchInterval: number | false;
}) {
  const qc = useQueryClient();
  const turnsQ = useAgentTurns(agentId, { refetchInterval });
  // Only turns still WAITING belong in the queue. A turn leaves the widget the
  // moment the host claims/streams it — from that point its user_message is in
  // the transcript, and showing it here too would double-render it.
  const messages = useMemo(
    () => (turnsQ.data ?? []).filter((t) => t.status === "queued"),
    [turnsQ.data],
  );
  const [order, setOrder] = useState<AgentTurnOut[]>(messages);
  const [draggingId, setDraggingId] = useState<string | null>(null);

  // Reconcile local order from the server whenever the query refetches.
  useEffect(() => setOrder(messages), [messages]);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: qk.agents.turns(agentId) });
    qc.invalidateQueries({ queryKey: qk.agents.detail(agentId) });
  };

  useEffect(() => {
    return monitorForElements({
      onDragStart: ({ source }) => setDraggingId(String(source.data.turnId ?? "")),
      onDrop: ({ source, location }) => {
        setDraggingId(null);
        const targets = location.current.dropTargets;
        if (targets.length === 0) return;
        const movingId = String(source.data.turnId ?? "");
        const inner = targets[0];
        const overId = String(inner.data.turnId ?? "");
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
        agentsApi
          .reorderTurns(agentId, arr.map((m) => m.id))
          .then(invalidate)
          .catch((err) => {
            setOrder(messages); // revert
            toast.error("Reorder failed", { error: err });
          });
      },
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [order, messages, agentId]);

  if (order.length === 0) return null;

  return (
    <div className="mb-2 space-y-1.5">
      <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-moon-500">
        <Layers size={11} className="text-dawn" />
        Queue
        <span className="text-moon-600">· {order.length}</span>
        <span className="ml-auto font-normal normal-case text-moon-600">
          Sent in order as the agent frees up
        </span>
      </div>
      {order.map((m) => (
        <QueueChip key={m.id} agentId={agentId} turn={m} dragging={draggingId === m.id} onChange={invalidate} />
      ))}
    </div>
  );
}

function QueueChip({
  agentId,
  turn,
  dragging,
  onChange,
}: {
  agentId: string;
  turn: AgentTurnOut;
  dragging: boolean;
  onChange: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(turn.body);
  const [busy, setBusy] = useState(false);
  const locked = turn.status !== "queued";

  useEffect(() => setDraft(turn.body), [turn.body]);

  useEffect(() => {
    const el = ref.current;
    if (!el || locked || editing) return;
    const data = { turnId: turn.id, type: "agent-turn" };
    return combine(
      draggable({ element: el, getInitialData: () => data }),
      dropTargetForElements({ element: el, getData: () => data }),
    );
  }, [turn.id, locked, editing]);

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
            disabled={busy || !draft.trim() || draft === turn.body}
            onClick={() =>
              run("Edit", () => agentsApi.editTurn(agentId, turn.id, draft)).then(() => setEditing(false))
            }
          >
            Save
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setDraft(turn.body);
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
        locked ? "border-dawn/40 bg-dawn/[0.06]" : "border-ink-700 bg-ink-950 hover:border-ink-600",
        dragging && "opacity-40",
      )}
    >
      {locked ? (
        <Loader2 size={13} className="mt-0.5 shrink-0 text-dawn motion-safe:animate-spin" />
      ) : (
        <span className="mt-0.5 shrink-0 cursor-grab text-moon-600 active:cursor-grabbing" aria-hidden>
          <GripVertical size={13} />
        </span>
      )}
      <p className="min-w-0 flex-1 whitespace-pre-wrap break-words text-[12px] leading-snug text-moon-100 line-clamp-2">
        {turn.body}
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
              onClick={() => run("Remove", () => agentsApi.cancelTurn(agentId, turn.id))}
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
