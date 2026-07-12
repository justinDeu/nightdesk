import type { MouseEvent } from "react";

/** Absolute href for a ticket detail page, honoring the SPA base path (BASE_URL
 *  is "/" in dev and "/app/" in prod). Used by raw <a> links so native
 *  middle-click / cmd-click open the same URL the router navigates to. */
export function ticketHref(id: string): string {
  return `${import.meta.env.BASE_URL}tickets/${id}`;
}

/**
 * Click gate for a raw <a href> that should navigate in-app on a plain
 * left-click but defer to the browser for every other activation.
 *
 * Returns true (and preventDefaults the event) for an unmodified left-click —
 * the caller then runs its client-side navigation. Returns false and leaves the
 * event untouched for middle-click (button !== 0), cmd/ctrl-click, shift-click,
 * and alt-click, so the browser performs its native affordance: open in new
 * tab / new window / download / copy-link. Using this on a ticket surface
 * restores the middle-click and ctrl-click affordances that onClick handlers
 * broke.
 */
export function inAppNav(e: MouseEvent): boolean {
  if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return false;
  e.preventDefault();
  return true;
}
