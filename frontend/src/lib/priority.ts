/** Priority scale mirror of src/nightdesk/domain/priority.py.
 *  0 none · 1 low · 2 medium · 3 high · 4 urgent. Higher sorts first. */

export interface PriorityMeta {
  value: number;
  name: string;
  label: string;
  /** short chip label, e.g. "P4" */
  short: string;
  /** dusk-console tone classes for the chip */
  cls: string;
  dot: string;
}

// Severity ramp: the chip's tone encodes urgency, not just the label. It climbs
// from faintest (none) through muted/neutral grays to the accent jade (high) and
// a strong ember (urgent), so a glance reads severity by fill strength.
export const PRIORITY_SCALE: PriorityMeta[] = [
  { value: 0, name: "none", label: "No priority", short: "P0", cls: "text-moon-600 border-ink-700 bg-ink-800/40", dot: "bg-moon-600/60" },
  { value: 1, name: "low", label: "Low", short: "P1", cls: "text-moon-400 border-ink-700 bg-ink-800", dot: "bg-moon-600" },
  { value: 2, name: "medium", label: "Medium", short: "P2", cls: "text-moon-100 border-moon-600/40 bg-ink-800", dot: "bg-moon-400" },
  { value: 3, name: "high", label: "High", short: "P3", cls: "text-lamp border-lamp/40 bg-lamp/10", dot: "bg-lamp" },
  { value: 4, name: "urgent", label: "Urgent", short: "P4", cls: "text-failed border-failed/50 bg-failed/15 font-bold", dot: "bg-failed" },
];

export const PRIORITY_MIN = 0;
export const PRIORITY_MAX = 4;

export function priorityMeta(value: number): PriorityMeta {
  return PRIORITY_SCALE[value] ?? PRIORITY_SCALE[0];
}
