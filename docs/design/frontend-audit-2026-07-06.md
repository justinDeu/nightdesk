# Frontend audit — ui/ground-up (2026-07-06)

Full-code audit of `frontend/src` (129 files, ~18k LOC) against the Web Interface
Guidelines and house standards, with UX weighted first. Four scoped review passes
(primitives/foundations, board/tickets, settings/scheduled/login, analytics/archive/api)
plus a live browser pass at 1440x900 and 390x844.

Legend: P0 broken/blocking, P1 real UX friction, P2 polish.
Items marked **[fixed]** were addressed on this branch (mobile pass + audit-fix waves, 2026-07-06).

## P0

- **Shell is desktop-only.** `components/AppShell.tsx:26` + `components/SideNav.tsx:70-73`:
  fixed 212px rail with no breakpoint; at 390px the sidebar claims >50% of the screen.
  **[fixed]** — rail hides below `md`, hamburger + off-canvas drawer in TopStrip.
- **Board drag does not work on touch.** `tickets/Board.tsx` uses
  pragmatic-drag-and-drop's element adapter (HTML5 DnD) with no touch addon: phones
  cannot move a card between columns at all. Matches the user-filed ticket
  "mobile shouldn't be able to drag tickets" — drag also hijacks column scroll.
  Alternative shipped: per-card "Move to…" transition menu (touch/keyboard-safe),
  drag kept for pointer devices only.
- **`uv tool install` ships no UI.** `pyproject.toml` packages only `src/nightdesk`;
  `frontend/dist` is not in the wheel and `nightdesk-setup` never builds it, so the
  README's headline install flow serves a JSON API with no SPA. Needs a ruling:
  bundle a prebuilt dist into the wheel (hatch force-include + release build step)
  or change the install story. Docs updated meanwhile.

## P1 — interaction correctness

- **[fixed]** **No unsaved-changes guard anywhere.** `settings/parts/SaveBar.tsx:12-76`
  (`useEditableForm` tracks `dirty`, nothing blocks navigation),
  `tickets/TicketDetailPage.tsx:264-372` and `tickets/TicketPeek.tsx:339-399`
  (prompt editors). Suggested: `beforeunload` + TanStack `useBlocker` keyed on
  `dirty`, once in `useEditableForm` and once in the prompt editors.
- **[fixed]** **Cancel run is one click, no confirm.** `desk/RunningCard.tsx:88-95`,
  `TicketPeek` StatusActions, `detail/DetailHeader.tsx:244-249`. Sits next to
  "Watch" on the running card; a stray tap kills an expensive run. Suggested:
  reuse the existing delete ConfirmDialog pattern, or undo-window.
- **[fixed]** **Hover-only affordances invisible on touch.** `tickets/BoardCard.tsx:84-101`
  (16px select circle, `opacity-0` until hover), `tickets/List.tsx:113-133`
  (row checkbox), `archive/ArchivePage.tsx:307` (Restore/Delete cluster).
  Suggested: persistent low-opacity rest state + `group-focus-within`, larger hit box.
- **Dialog/Dropdown can exceed the viewport.** `ui/Dialog.tsx:38-46` has no
  max-height/scroll/overscroll-contain safety net; `ui/DropdownMenu.tsx:23-39`
  doesn't bind `--radix-dropdown-menu-content-available-height`. Long content
  gets clipped off-screen on phones. **[fixed]** for Dialog max-height/overscroll;
  dropdown height binding recommended.
- **Touch targets systematically under-size.** `ui/Button.tsx:26-29` (sm 28px,
  md 36px are the only sizes), `ui/IconButton.tsx:15-18`, SideNav rows (32px).
  Suggested: `@media (pointer: coarse)` min-height bump once in the primitives,
  not per call site.
- **Settings/profiles two-pane has no mobile fallback.** `SettingsPage.tsx:56-59`
  (sticky 210px nav), `settings/ProfilesSection.tsx:54`
  (`grid-cols-[240px_1fr]`). **[fixed]** — stacks below `md`.

## P1 — state & navigation

- **[fixed]** **Archive filter/sort not in URL.** `archive/ArchivePage.tsx:78-90` keeps them in
  useState/localStorage; reload/share/back loses them. AnalyticsPage already does
  this right — mirror its search-schema pattern.
- **[fixed — content-visibility containment]** **No virtualization at limit=200.** `tickets/TicketsPage.tsx:86` +
  per-column/group `.map()` in Board/List. Cheap first step:
  `content-visibility: auto` + `contain-intrinsic-size` on cards/rows;
  `@tanstack/react-virtual` if boards grow past a few hundred.

## P1 — a11y & semantics

- **[fixed]** Native `title=` on day-toggle chips — house-rule violation:
  `settings/SchedulingSection.tsx:333`. Use the shared Tooltip.
- **[fixed — shared ui/Switch]** Hand-rolled toggle without `role="switch"`/`aria-checked`:
  `scheduled/CronEditorDialog.tsx:320-356`; `ScheduledPage.tsx:196-211` is a second
  independent reimplementation. Extract one `ui/Switch` with a11y baked in.
- **[fixed]** Analytics "Project" select lacks a programmatic label:
  `analytics/AnalyticsPage.tsx:213-232`.
- **[fixed]** Range segmented control lacks `aria-pressed`: `analytics/AnalyticsPage.tsx:196-207`.

## P2 — polish

- **[fixed]** `tabular-nums` missing on BreakdownTable + archive cost columns:
  `analytics/charts.tsx:411-438`, `archive/ArchivePage.tsx:298-300`.
- **[fixed]** `formatUsd` hardcodes `$…toFixed(2)` (no thousands separators):
  `lib/status.ts:27-31`. Use `Intl.NumberFormat` currency.
- **[fixed]** Latency card shows false "no data" while loading:
  `analytics/AnalyticsPage.tsx:186,416-420` (isLoading ignores latencyQ).
- **[fixed]** Toast card is a div-with-onClick (dismiss button already exists — drop the
  card-level handler): `ui/Toast.tsx:159-176`. Toaster offset ignores
  `env(safe-area-inset-*)`: `ui/Toast.tsx:251-259`. **[fixed]** safe-area offset.
- **[fixed]** Label names can overflow their card (no truncate/min-w-0):
  `settings/LabelsSection.tsx:69`.
- **[fixed]** Raw internals leak into desk card copy ("query crashed: [Errno 11] write could
  not complete without blocking"). Suggested: humanize the failure line
  ("Run failed — worker couldn't write the transcript") and keep the raw error
  in the peek/detail error section.
- Keyboard-hint chip row on Desk (`j k move · o open …`) is always visible —
  noise once learned, meaningless on touch. **[fixed]** hidden below `md`;
  consider desktop-side showing only in the `?` cheatsheet.
- `frontend/README.md` design-language section is stale: describes the lamp
  accent as amber; `styles/theme.css:36-42` is jade/green since the retheme.

## Clean

Input, Select, Badge, Card, EmptyState, ErrorState, Kbd, Spinner, StatusPill,
theme.css, index.html, main.tsx, router.tsx, all lib/ helpers, and the api/
layer (ApiError normalization, 401 event, thin mutation hooks) passed review.
Reduced motion, `color-scheme: dark`, focus-visible, and non-git degradation
are handled correctly throughout.
