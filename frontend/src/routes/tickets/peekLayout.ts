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
 *   - md+ (768 / 1024 / 1280 / 1440 / 1920px): right-anchored 440px rail
 *     (`right-0`, top under the top strip, bottom inset, capped at 92vw).
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
  "md:inset-auto md:bottom-3 md:right-0 md:top-14 md:w-[440px] md:max-w-[92vw] md:rounded-bl-card md:border-b md:border-l",
];
