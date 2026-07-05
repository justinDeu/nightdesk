/** Absolute href for a ticket detail page, honoring the SPA base path (BASE_URL
 *  is "/" in dev and "/app/" in prod). Used by raw <a> links so native
 *  middle-click / cmd-click open the same URL the router navigates to. */
export function ticketHref(id: string): string {
  return `${import.meta.env.BASE_URL}tickets/${id}`;
}
