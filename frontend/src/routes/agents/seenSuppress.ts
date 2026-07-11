/**
 * Suppression of the agent screen's auto-mark-seen after a manual mark-unread.
 *
 * The screen stamps "seen" on mount and as transcript events land — which would
 * instantly undo a "Mark unread" clicked while still on the screen. Adding the
 * agent id here mutes the auto-stamp; AgentScreen clears the entry when it
 * unmounts, so leaving and returning resumes normal seen behavior (mirrors
 * email clients: mark unread, walk away, the badge stays lit). Module-local,
 * same spirit as wakeSeed's create→screen hand-off.
 */
const suppressed = new Set<string>();

export function suppressAutoSeen(agentId: string): void {
  suppressed.add(agentId);
}

export function clearAutoSeenSuppression(agentId: string): void {
  suppressed.delete(agentId);
}

export function isAutoSeenSuppressed(agentId: string): boolean {
  return suppressed.has(agentId);
}
