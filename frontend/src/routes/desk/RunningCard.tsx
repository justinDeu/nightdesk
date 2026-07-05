import { useNavigate } from "@tanstack/react-router";
import { Cpu, Eye, Square } from "lucide-react";
import type { ProjectOut, RunOut, TicketOut } from "@/api/types";
import { Card } from "@/ui/Card";
import { Button } from "@/ui/Button";
import { ProjectTag } from "@/components/ProjectDot";
import { useLiveTranscript, latestMeaningfulLine } from "@/lib/transcript";
import { useNow } from "@/lib/useNow";
import { durationBetween } from "@/lib/time";
import { formatUsd } from "@/lib/status";
import { ticketHref } from "@/lib/routes";
import { useTicketActions } from "@/lib/ticketActions";
import { cn } from "@/lib/cn";

const TAIL_TONE: Record<string, string> = {
  text: "text-moon-100",
  tool: "text-lamp",
  thinking: "text-moon-400 italic",
  result: "text-success",
  error: "text-failed",
  idle: "text-moon-600",
};

export function RunningCard({
  ticket,
  run,
  project,
}: {
  ticket: TicketOut;
  run: RunOut | undefined;
  project: ProjectOut | undefined;
}) {
  const navigate = useNavigate();
  const actions = useTicketActions();
  const now = useNow(true);
  const { events } = useLiveTranscript(ticket.id, true);
  const tail = latestMeaningfulLine(events);

  const elapsed = run ? durationBetween(run.started_at, run.finished_at, now) : "";
  const model = run?.model_used ?? null;
  const cost = run?.cost_usd ?? null;

  const open = () => navigate({ to: "/tickets/$id", params: { id: ticket.id } });

  return (
    <Card running raised className="flex flex-col">
      <div className="flex flex-col gap-2 px-4 pb-3 pt-3.5">
        <div className="flex items-start justify-between gap-3">
          <a
            href={ticketHref(ticket.id)}
            onClick={(e) => {
              if (e.metaKey || e.ctrlKey) return;
              e.preventDefault();
              open();
            }}
            className="min-w-0 rounded-[4px] text-left font-display text-sm font-semibold text-moon-100 hover:text-lamp focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lamp"
          >
            <span className="line-clamp-2">{ticket.title}</span>
          </a>
          <span className="shrink-0 font-mono text-xs tabular-nums text-lamp">{elapsed}</span>
        </div>

        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-moon-400">
          <ProjectTag project={project} />
          {model && (
            <span className="inline-flex items-center gap-1 font-mono text-[11px] text-moon-400">
              <Cpu size={11} /> {model}
            </span>
          )}
          {cost != null && (
            <span className="font-mono text-[11px] tabular-nums text-moon-400">
              {formatUsd(cost)}
            </span>
          )}
        </div>

        <div className="min-h-[2.5rem] rounded-control border border-ink-700/60 bg-ink-950/50 px-2.5 py-1.5">
          <p className={cn("font-mono text-[11px] leading-snug line-clamp-2", TAIL_TONE[tail.kind])}>
            {tail.text}
          </p>
        </div>
      </div>

      <div className="mt-auto flex items-center gap-2 border-t border-ink-700/70 bg-ink-950/30 px-3 py-2">
        <Button size="sm" variant="subtle" leadingIcon={<Eye size={14} />} onClick={open}>
          Watch
        </Button>
        <Button
          size="sm"
          variant="danger"
          leadingIcon={<Square size={13} />}
          onClick={() => actions.cancel(ticket)}
        >
          Cancel
        </Button>
      </div>
    </Card>
  );
}
