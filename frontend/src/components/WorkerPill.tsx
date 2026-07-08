import { useWorkerStatus } from "@/api/config";
import { formatUsd } from "@/lib/status";
import { cn } from "@/lib/cn";
import { Tooltip } from "@/ui/Tooltip";

/** Worker status pill + spend chip. Polls /api/v1/worker/status every 3s.
 *  Degrades to an "offline/unknown" state when the API is absent (isError). */
export function WorkerPill() {
  const { data, isError, isLoading } = useWorkerStatus();

  let tone: "running" | "idle" | "stale" | "offline" = "offline";
  let text = "API offline";

  if (isLoading) {
    tone = "idle";
    text = "Connecting…";
  } else if (isError || !data) {
    tone = "offline";
    text = "API offline";
  } else if (data.stale) {
    tone = "stale";
    text = "Worker stale";
  } else if (data.total_running > 0) {
    tone = "running";
    text = `${data.total_running} running`;
  } else {
    tone = "idle";
    text = data.in_window ? "Idle · in window" : "Idle · off hours";
  }

  const dot = {
    running: "bg-lamp",
    idle: "bg-success",
    stale: "bg-failed",
    offline: "bg-moon-600",
  }[tone];

  const tip = data
    ? `${data.host ?? "worker"} · max ${data.max_parallel} parallel · ${
        data.in_window ? "in active window" : "outside window"
      } · ${data.schedule_timezone}`
    : "Worker status unavailable — is the API running?";

  return (
    <div className="flex items-center gap-2">
      <Tooltip content={tip}>
        <div className="flex h-8 items-center gap-2 rounded-control border border-ink-700 bg-ink-900 px-2.5">
          <span
            className={cn(
              "h-2 w-2 rounded-full",
              dot,
              tone === "running" && "motion-safe:animate-pulse",
            )}
          />
          <span className="text-xs font-medium text-moon-100">{text}</span>
        </div>
      </Tooltip>

      {/* Spend chip folds away on the narrowest screens to keep the strip clear. */}
      <div className="hidden sm:block">
        <Tooltip content="Today's completed-run spend · month-to-date">
          <div className="flex h-8 items-center gap-1.5 rounded-control border border-ink-700 bg-ink-900 px-2.5 font-mono text-xs">
            <span className="text-lamp">{formatUsd(data?.day_spend_usd)}</span>
            <span className="text-moon-600">today</span>
            {data && data.month_spend_usd > 0 && (
              <>
                <span className="text-moon-600">·</span>
                <span className="text-moon-400">{formatUsd(data.month_spend_usd)}</span>
                <span className="text-moon-600">mo</span>
              </>
            )}
          </div>
        </Tooltip>
      </div>
    </div>
  );
}
