/** Client-stored "last visited the Desk" watermark, powering the
 *  "While you were away" delta feed. Stored as an epoch-ms string. */

const KEY = "nightdesk:desk-last-visit";

export function getLastVisit(): number | null {
  const raw = localStorage.getItem(KEY);
  if (!raw) return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

export function setLastVisit(ts = Date.now()): void {
  localStorage.setItem(KEY, String(ts));
}
