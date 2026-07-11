/** Time + duration formatting helpers. All timestamps from the API are ISO
 *  strings in UTC; the browser localizes on display. Kept dependency-free. */

/** Parse an API ISO timestamp to epoch ms, tolerating a missing timezone
 *  (SQLite naive datetimes are UTC — see the work-hours memory). */
export function parseTs(iso: string | null | undefined): number | null {
  if (!iso) return null;
  // If the string has no timezone marker, treat it as UTC.
  const hasTz = /[zZ]|[+-]\d{2}:?\d{2}$/.test(iso);
  const ms = Date.parse(hasTz ? iso : `${iso}Z`);
  return Number.isNaN(ms) ? null : ms;
}

/** "2m ago", "3h ago", "just now", "in 5m" — compact relative time. */
export function relativeTime(iso: string | null | undefined, now = Date.now()): string {
  const ms = parseTs(iso);
  if (ms === null) return "";
  const diff = ms - now;
  const abs = Math.abs(diff);
  const past = diff <= 0;
  const s = Math.round(abs / 1000);
  if (s < 45) return past ? "just now" : "in a moment";
  const m = Math.round(s / 60);
  if (m < 60) return past ? `${m}m ago` : `in ${m}m`;
  const h = Math.round(m / 60);
  if (h < 24) return past ? `${h}h ago` : `in ${h}h`;
  const d = Math.round(h / 24);
  if (d < 7) return past ? `${d}d ago` : `in ${d}d`;
  const w = Math.round(d / 7);
  if (w < 5) return past ? `${w}w ago` : `in ${w}w`;
  const mo = Math.round(d / 30);
  if (mo < 12) return past ? `${mo}mo ago` : `in ${mo}mo`;
  return past ? `${Math.round(d / 365)}y ago` : `in ${Math.round(d / 365)}y`;
}

/** Elapsed / duration as H:MM:SS or M:SS. */
export function formatDuration(ms: number): string {
  if (ms < 0) ms = 0;
  const total = Math.floor(ms / 1000);
  const s = total % 60;
  const m = Math.floor(total / 60) % 60;
  const h = Math.floor(total / 3600);
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

/** Duration between two API timestamps (finished may be null → uses `now`). */
export function durationBetween(
  start: string | null | undefined,
  finish: string | null | undefined,
  now = Date.now(),
): string {
  const a = parseTs(start);
  if (a === null) return "";
  const b = parseTs(finish) ?? now;
  return formatDuration(b - a);
}

/** Compact elapsed for a live "time-in-state" chip: "40s", then "1m 12s".
 *  Sibling of formatDuration (M:SS) for the few places that read better in
 *  seconds-first form (a wake timer, a short heartbeat). */
export function formatCompactElapsed(ms: number): string {
  if (ms < 0) ms = 0;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${s % 60}s`;
}

/** Absolute local time label, e.g. "Jul 5, 21:14". */
export function absoluteTime(iso: string | null | undefined): string {
  const ms = parseTs(iso);
  if (ms === null) return "";
  return new Date(ms).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
