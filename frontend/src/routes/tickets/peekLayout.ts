/**
 * Shared positioning for the right-rail side-peek. Consumed by BOTH the ticket
 * peek (TicketPeek) and the Issues/MR peek (ExternalItemPeek) so the two panels
 * cannot drift apart — the classes were previously copy/pasted into each file,
 * which is exactly what made their placement look suspect (ticket 9f5686f9).
 *
 * Verified via headless Chromium against the production CSS bundle, with the
 * faithful AppShell → main → TicketsPage ancestor chain:
 *   - below md (phone, e.g. 375px): full-screen cover (`inset-0 w-full`) so the
 *     panel is readable instead of a squeezed rail. This is the only width at
 *     which the peek covers the left side, and it is the intended mobile shape.
 *   - md+ (768 / 1024 / 1280 / 1440 / 1920px): right-anchored rail (`right-0`,
 *     top under the top strip, bottom inset, capped at 92vw so it never eats
 *     the whole screen on a pinched window), sized fluidly (ticket 55105222)
 *     by `clamp(480px,44vw,760px)` — ~40-50% of the viewport at typical
 *     laptop/desktop widths, with a floor so it never collapses back into the
 *     unreadable ~300px rail a plain vw unit would give at md/lg, and a
 *     ceiling so it doesn't balloon on ultrawide monitors:
 *       - 768px (md floor): clamped to the 480px min  → 62.5% (mobile-
 *         adjacent, min-width wins on purpose).
 *       - 1024px:           clamped to the 480px min  → 46.9%.
 *       - 1280px (laptop):  44vw = 563px               → 44.0%.
 *       - 1440px:           44vw = 634px               → 44.0%.
 *       - 1920px:           clamped to the 760px max   → 39.6% (ceiling,
 *         keeps the rail from reading as its own full page on a huge
 *         monitor).
 *     Tailwind's build-time class scanner only picks up literal class
 *     strings, not interpolated ones — so this exact `clamp(...)` string is
 *     repeated verbatim (not pulled from a shared JS constant) everywhere
 *     that needs to reserve layout space for the open rail (e.g.
 *     TicketsPage's header/content right-padding). Keep those in sync by
 *     hand if this value changes, same as the pre-existing `92vw` cap.
 * No ancestor on either peek's path establishes a transform/filter/contain
 * containing block, so `position: fixed` resolves against the viewport for
 * both — they render identically at every width.
 *
 * Pass to `cn(...)`, e.g. `cn(peekRailClasses)`.
 */
export const peekRailClasses: string[] = [
  "fade-in fixed z-40 flex flex-col overflow-hidden border-ink-700 bg-ink-900 shadow-[var(--shadow-pop)]",
  // Phone: a full-screen overlay so the panel is readable instead of a
  // squeezed side rail. md+: the right-anchored rail.
  "inset-0 w-full",
  "md:inset-auto md:bottom-3 md:right-0 md:top-14 md:w-[clamp(480px,44vw,760px)] md:max-w-[92vw] md:rounded-bl-card md:border-b md:border-l",
];

/**
 * Dimmed scrim shown behind the peek while it's open, so the rail reads as a
 * deliberate overlay instead of a floating stray panel (ticket 55105222).
 *
 * Deliberately `pointer-events-none`: it is visual only. The board/list stays
 * fully clickable underneath — clicking a different card is expected to swap
 * the peek's contents in place rather than requiring a close-then-reopen, and
 * ticket ab109541's outside-pointerdown dismissal (capture-phase, keyed off
 * the real event target) already gives "click elsewhere closes it" for free.
 * A click-catching scrim would fight both of those: it would swallow the
 * pointerdown before it ever reaches a card, and it would need its own
 * close/swap logic duplicating ab109541's. Letting clicks pass through keeps
 * this purely additive — it composes with whichever of "no outside-click
 * handling yet" or "ab109541 landed" is true at build time, with no
 * coordination needed between the two changes.
 *
 * z-30: above ordinary (unpositioned) page content so the dim actually reads,
 * below the peek rail's z-40 so the peek itself is never dimmed.
 *
 * Opacity matches the existing overlay convention (Dialog's `bg-black/70`,
 * NavDrawer's `bg-ink-950/70`) rather than something lighter — against this
 * theme's already-dark ink surfaces, anything much under 70% reads as barely
 * there and defeats the point ("so the overlay reads as intentional").
 */
export const peekBackdropClasses =
  "fade-in pointer-events-none fixed inset-0 z-30 hidden bg-ink-950/70 md:block";
