import { useWorkerStatus } from "@/api/config";
import { useNow } from "@/lib/useNow";
import type { AgentLiveness } from "@/api/types";

/**
 * Honest wake-state feedback for a resident agent.
 *
 * The server reports a coarse `liveness` (alive / needs-input / warm / cold /
 * crashed / ended), but "cold" hides two very different realities: an agent that
 * is simply parked, and one we just kicked and are waiting to boot. This hook
 * splits those apart by combining liveness with the client-side wake timestamp
 * and the live worker heartbeat, then surfaces time-in-state and the reasons a
 * wake will never complete (worker offline / stale heartbeat). See the ticket:
 * "Wake-state feedback is dishonest."
 */

/** A wake taking longer than this with a live worker is flagged "slow" so the UI
 *  warns instead of spinning forever. ~60s covers a cold runtime boot plus a turn
 *  or two of compilation, so a real first wake is not flagged. */
export const WAKE_WARN_MS = 60_000;

export type WakePhase = "active" | "cold" | "waking" | "error" | "ended";

export interface WakeState {
  phase: WakePhase;
  /** ms since the wake was triggered; 0 outside the waking phase. */
  elapsedMs: number;
  /** waking past WAKE_WARN_MS with a live worker — prompt a warning. */
  slow: boolean;
  /** the worker process is not reachable (API down / 500s). */
  workerOffline: boolean;
  /** the worker is reachable but its heartbeat is stale. */
  workerStale: boolean;
}

/**
 * Derive a truthful wake phase. Polls the worker heartbeat (deduped with the
 * global strip pill, so this adds no extra traffic) and ticks once a second only
 * while a wake is in flight.
 *
 * @param liveness  server-reported liveness
 * @param wakeAt    epoch ms the user triggered a wake (send-to-cold / Wake btn),
 *                  or null when no wake is outstanding
 */
export function useWakeState(
  liveness: AgentLiveness,
  wakeAt: number | null,
): WakeState {
  const worker = useWorkerStatus();
  // Offline only once the query has settled — the initial loading state
  // (data === undefined) must read as "unknown", not "offline", or a hard
  // refresh of a cold agent flashes a false "Worker offline" caveat.
  const workerOffline = worker.isError || (worker.isSuccess && !worker.data);
  const workerStale = !!worker.data?.stale;

  const waking = wakeAt != null && (liveness === "cold" || liveness === "crashed");
  // useNow pauses when idle, so the screen re-renders nothing between wakes.
  const now = useNow(waking);
  const elapsedMs = waking && wakeAt != null ? Math.max(0, now - wakeAt) : 0;

  let phase: WakePhase;
  if (liveness === "ended") phase = "ended";
  else if (waking) phase = "waking";
  else if (liveness === "crashed") phase = "error";
  else if (liveness === "cold") phase = "cold";
  else phase = "active"; // alive | needs-input | warm

  // "Slow" only means something against a live worker. An offline/stale worker
  // has its own, more honest caveat — "agent cannot wake" — so we don't double up.
  const slow = phase === "waking" && elapsedMs > WAKE_WARN_MS && !workerOffline && !workerStale;

  return { phase, elapsedMs, slow, workerOffline, workerStale };
}
