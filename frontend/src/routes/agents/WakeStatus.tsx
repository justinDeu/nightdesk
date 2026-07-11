import type { ReactNode } from "react";
import { Activity, AlertTriangle, PowerOff } from "lucide-react";
import { cn } from "@/lib/cn";
import { formatCompactElapsed } from "@/lib/time";
import { AgentStatePill } from "./AgentStatePill";
import type { WakeState } from "./useWakeState";

/**
 * Honest wake-state UI. `WakeStatusChip` replaces the bare liveness pill in the
 * agent header: when a wake is in flight it shows a gradient "Waking · Ns" chip
 * with live time-in-state (plus a worker caveat when the wake can't land).
 * `WakeNotice` is the banner pinned over the transcript that explains, in words,
 * why a cold agent is not coming up — worker offline / stale heartbeat / a wake
 * that has run past the sane threshold. Together they kill the indefinite
 * "waking up" spinner the page used to show when nothing was coming.
 */

/** The header state chip (+ a worker caveat beside it when a wake can't land). */
export function WakeStatusChip({
  liveness,
  wake,
}: {
  liveness: Parameters<typeof AgentStatePill>[0]["liveness"];
  wake: WakeState;
}) {
  return (
    <span className="inline-flex items-center gap-2">
      {wake.phase === "waking" ? <WakingChip wake={wake} /> : <AgentStatePill liveness={liveness} />}
      {shouldShowWorkerCaveat(wake) && <WorkerCaveatChip wake={wake} />}
    </span>
  );
}

/** "Waking · 40s" — the dawn-edge gradient + a live, monospace timer. */
function WakingChip({ wake }: { wake: WakeState }) {
  return (
    <span className="relative inline-flex items-center gap-1.5 rounded-full p-[1px]">
      <span aria-hidden className="dawn-edge absolute inset-0 rounded-full" />
      <span className="relative inline-flex items-center gap-1.5 rounded-full bg-ink-900 px-2 py-0.5 text-[11px] font-medium leading-none">
        <span className="h-1.5 w-1.5 rounded-full bg-lamp motion-safe:animate-pulse" />
        <span className="dawn-text">Waking</span>
        <span className="font-mono tabular-nums text-moon-400">· {formatCompactElapsed(wake.elapsedMs)}</span>
      </span>
    </span>
  );
}

/** "Worker offline" / "heartbeat stale" — the reason a cold agent stays cold. */
function WorkerCaveatChip({ wake }: { wake: WakeState }) {
  if (wake.workerOffline) {
    return (
      <span className="inline-flex items-center gap-1 rounded-full border border-failed/30 bg-failed/10 px-2 py-0.5 text-[11px] font-medium leading-none text-failed">
        <PowerOff size={11} />
        Worker offline
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-warn/30 bg-warn/10 px-2 py-0.5 text-[11px] font-medium leading-none text-warn">
      <Activity size={11} />
      Worker stale
    </span>
  );
}

/** Show the caveat only against a parked / waking agent that needs the worker to
 *  move. Active, ended, and crashed agents have their own honest state. */
function shouldShowWorkerCaveat(wake: WakeState): boolean {
  if (wake.phase !== "cold" && wake.phase !== "waking") return false;
  return wake.workerOffline || wake.workerStale;
}

/** The banner is the prominent "your wake is stuck" callout — the replacement
 *  for the old indefinite spinner. It fires only while a wake is actually in
 *  flight (offline / stale / past-threshold). A merely parked cold agent gets
 *  the lighter caveat chip next to its state instead. */
function bannerApplies(wake: WakeState): boolean {
  if (wake.phase !== "waking") return false;
  return wake.workerOffline || wake.workerStale || wake.slow;
}

/**
 * The transcript-stage banner: null when the wake is healthy / nothing to say.
 * Renders the single most-severe applicable notice (offline > stale > slow) so
 * the agent never stacks warnings.
 */
export function WakeNotice({ wake }: { wake: WakeState }) {
  if (!bannerApplies(wake)) return null;

  if (wake.workerOffline) {
    return (
      <Notice tone="failed" icon={<PowerOff size={14} />}>
        Worker offline — this agent can&apos;t wake. No worker process is responding. A queued
        message will be delivered once the worker is back.
      </Notice>
    );
  }
  if (wake.workerStale) {
    return (
      <Notice tone="warn" icon={<Activity size={14} />}>
        Worker heartbeat is stale — the wake may not be picked up. The worker process appears
        unresponsive; still waking for {formatCompactElapsed(wake.elapsedMs)}.
      </Notice>
    );
  }
  // slow: live worker, but past the sane threshold
  return (
    <Notice tone="warn" icon={<AlertTriangle size={14} />}>
      Still waking after {formatCompactElapsed(wake.elapsedMs)} and the worker is live — something may
      be stuck. You can keep waiting, or end and restart the agent.
    </Notice>
  );
}

function Notice({
  tone,
  icon,
  children,
}: {
  tone: "failed" | "warn";
  icon: ReactNode;
  children: ReactNode;
}) {
  return (
    <div
      role="status"
      className={cn(
        "flex items-start gap-2 border-b px-4 py-2 text-[12px] sm:px-6",
        tone === "failed"
          ? "border-failed/25 bg-failed/[0.07] text-failed"
          : "border-warn/25 bg-warn/[0.07] text-warn-soft",
      )}
    >
      <span className="mt-px shrink-0">{icon}</span>
      <span className="min-w-0 flex-1 leading-snug">{children}</span>
    </div>
  );
}
