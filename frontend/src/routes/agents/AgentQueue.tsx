import { Layers, Loader2 } from "lucide-react";
import type { AgentTurnOut } from "@/api/types";
import { cn } from "@/lib/cn";

/**
 * The agent's pending turn queue: user messages typed while a turn was in
 * flight, shown above the composer. Sends always enqueue (the composer stays
 * enabled while streaming), so this strip is how queued work stays visible.
 *
 * Read-only: the backend inbox has no per-turn reorder/edit/cancel endpoint
 * (unlike ticket steering) — a queued turn clears when the host claims it, or
 * all queued turns are cancelled together when the agent is ended. If those
 * endpoints land later, this adapts to SteerQueue's editable chips.
 */
export function AgentQueue({ turns }: { turns: AgentTurnOut[] }) {
  const queued = turns
    .filter((t) => t.kind === "user" && (t.status === "queued" || t.status === "delivering"))
    .sort((a, b) => a.position - b.position);

  if (queued.length === 0) return null;

  return (
    <div className="mb-2 space-y-1.5">
      <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-moon-500">
        <Layers size={11} className="text-dawn" />
        Queue
        <span className="text-moon-600">· {queued.length}</span>
        <span className="ml-auto font-normal normal-case text-moon-600">
          Sent in order as the agent frees up
        </span>
      </div>
      {queued.map((t) => {
        const delivering = t.status === "delivering";
        return (
          <div
            key={t.id}
            className={cn(
              "flex items-start gap-2 rounded-control border px-2 py-1.5",
              delivering ? "border-dawn/40 bg-dawn/[0.06]" : "border-ink-700 bg-ink-950",
            )}
          >
            {delivering && (
              <Loader2 size={13} className="mt-0.5 shrink-0 text-dawn motion-safe:animate-spin" />
            )}
            <p className="min-w-0 flex-1 whitespace-pre-wrap break-words text-[12px] leading-snug text-moon-100 line-clamp-2">
              {t.body}
            </p>
            {delivering && (
              <span className="mt-0.5 shrink-0 text-[10px] font-medium uppercase tracking-wide text-dawn">
                Sending
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}
