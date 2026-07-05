/** The browser's IANA timezone, best-effort. */
export function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** A curated set of common IANA zones for the timezone picker, always including
 *  the browser's zone and any value already on record. Scheduler window times
 *  are wall-clock in whichever zone is selected here — there is no UTC
 *  conversion (see worker/scheduler.py window_matches). */
const COMMON = [
  "UTC",
  "America/Los_Angeles",
  "America/Denver",
  "America/Chicago",
  "America/New_York",
  "America/Sao_Paulo",
  "Europe/London",
  "Europe/Paris",
  "Europe/Berlin",
  "Europe/Moscow",
  "Asia/Dubai",
  "Asia/Kolkata",
  "Asia/Singapore",
  "Asia/Tokyo",
  "Australia/Sydney",
];

export function timezoneOptions(...ensure: (string | null | undefined)[]): string[] {
  const set = new Set<string>(COMMON);
  set.add(browserTimezone());
  for (const z of ensure) if (z) set.add(z);
  return [...set].sort();
}

/** The days, Monday-first, aligned to Python's weekday() (Mon=0). day_mask bit
 *  for day index d is 1 << d, so Mon=1, Tue=2 … Sun=64. */
export const DAYS: { short: string; label: string; bit: number }[] = [
  { short: "Mon", label: "Monday", bit: 1 << 0 },
  { short: "Tue", label: "Tuesday", bit: 1 << 1 },
  { short: "Wed", label: "Wednesday", bit: 1 << 2 },
  { short: "Thu", label: "Thursday", bit: 1 << 3 },
  { short: "Fri", label: "Friday", bit: 1 << 4 },
  { short: "Sat", label: "Saturday", bit: 1 << 5 },
  { short: "Sun", label: "Sunday", bit: 1 << 6 },
];

export const EVERY_DAY = 127;
export const WEEKDAYS = 0b0011111; // Mon–Fri
export const WEEKENDS = 0b1100000; // Sat–Sun

export function maskSummary(mask: number): string {
  if (mask === EVERY_DAY) return "Every day";
  if (mask === WEEKDAYS) return "Weekdays";
  if (mask === WEEKENDS) return "Weekends";
  const on = DAYS.filter((d) => mask & d.bit).map((d) => d.short);
  return on.length ? on.join(", ") : "No days";
}
