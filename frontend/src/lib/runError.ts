/**
 * Humanize a raw run failure summary for at-a-glance surfaces (the Desk "Needs
 * you" row). The API's `error_summary` often leaks worker/OS internals like
 * "query crashed: [Errno 11] write could not complete without blocking"; strip
 * the leading noise, sentence-case what's left, and lead with "Run failed — ".
 * Keep the raw string available (e.g. as a tooltip) for anyone who needs it.
 */
const NOISE_PREFIXES = [
  /^query crashed:\s*/i,
  /^runner exited with code\s*-?\d+:\s*/i,
  /^\[errno\s*-?\d+\]\s*/i,
  /^error:\s*/i,
  /^exception:\s*/i,
];

export function humanizeRunError(raw: string | null | undefined): string | null {
  if (!raw) return null;
  let s = raw.trim();

  // Peel stacked leading prefixes, e.g. "query crashed: [Errno 11] write…".
  let changed = true;
  while (changed) {
    changed = false;
    for (const re of NOISE_PREFIXES) {
      const next = s.replace(re, "");
      if (next !== s) {
        s = next.trim();
        changed = true;
      }
    }
  }

  if (!s) s = "the run ended unexpectedly";
  const sentence = s.charAt(0).toUpperCase() + s.slice(1);
  return `Run failed — ${sentence}`;
}
