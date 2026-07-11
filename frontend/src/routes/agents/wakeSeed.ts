/**
 * Hand-off of the wake timestamp from the create flow to the agent screen.
 *
 * Creating an agent wakes it server-side (POST /agents defaults wake=true), then
 * navigates to the detail screen — which tracks "waking" via a client-side
 * `pokedAt` timestamp that a fresh mount cannot know about. The create dialog
 * seeds this map before navigating; AgentScreen consumes it on mount so the
 * just-created agent honestly shows "Waking" (via useWakeState) instead of a
 * dead "Cold" while its host boots. Module-local and one-shot: a later revisit
 * of the same agent reads nothing and falls back to normal liveness.
 */
const seeds = new Map<string, number>();

export function seedWake(agentId: string, at: number = Date.now()): void {
  seeds.set(agentId, at);
}

/** Read-and-clear the seed for an agent (null when none was planted). */
export function consumeWakeSeed(agentId: string): number | null {
  const at = seeds.get(agentId) ?? null;
  seeds.delete(agentId);
  return at;
}
