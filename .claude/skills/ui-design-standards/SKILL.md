---
name: ui-design-standards
description: nightdesk frontend design standards — load BEFORE building or reworking any page/screen/component in frontend/. Encodes the dusk-console tokens, layout archetypes, density rules, and the banned "AI slop" patterns (centered column dumps, unused viewport, native tooltips).
internal: true
---

# nightdesk UI design standards

Load this before touching any screen in `frontend/`. The bar: a page must look
designed for this product, not generated. The user explicitly rejects "AI slop".

## Banned patterns (each has caused a user complaint)

1. **The centered column dump.** Never `max-w-*` + `mx-auto` a work surface and
   stack content vertically down the middle with dead gutters left and right.
   Work surfaces (board, detail, analytics, settings content, triage) use the
   full viewport minus chrome. Centered narrow layouts are acceptable ONLY for
   login and true focused-form modals.
2. **Scroll-to-discover.** If the only way to find information is scrolling a
   single column, the layout has failed. Place regions side by side; keep
   properties/metadata visible without scrolling; scroll only long content
   (transcripts, tables, prompt bodies) inside its own region.
3. **Navigation where a peek belongs.** Clicking an item in a collection opens
   a side peek/rail. Full navigation only on Enter / an Open button / cmd-click.
4. **Native `title=` tooltips.** Use the styled Tooltip primitive.
5. **Hand-rolled solved domains.** Charts = Recharts styled to tokens. Don't
   DIY chart axes, virtualized lists, etc. Bespoke is for signature pieces only.
6. **Default library styling.** Any third-party component (Recharts, Radix) must
   be restyled to the token system — no default palettes, fonts, or shadows.

## Layout archetypes (pick deliberately, per page job)

- **Work surface** (board, list, triage): full-bleed, dense, keyboard cursor,
  side peek for the focused item, bulk bar on multi-select.
- **Document + evidence** (ticket detail): header band with identity + actions,
  then regions for content, activity, and live evidence (transcript/diff); let
  ticket state shift the emphasis. No single static column.
- **Dashboard** (Desk, analytics): full-width grid; stat tiles row; charts and
  breakdown tables share rows on wide screens; minimal scrolling at 1440+.
- **Two-pane settings**: section nav left, content right; dirty-state save bar,
  not save-per-field.

## Density and hierarchy

- Reference density is Linear: compact rows, real information per square inch,
  breathable but never sparse. If a 1440px screen shows fewer than ~8 meaningful
  facts, the page is under-designed.
- Every screen answers within one second of looking: what is this, what changed,
  what can I do next. Lead with state (status pills, costs, timestamps), not
  labels.
- Empty states direct action (what lands here, one button), never just an icon.

## Tokens (styles/theme.css is the source of truth)

- Surfaces: ink scale — being retuned toward neutral near-black with HIGHER
  contrast (user preference; keep blue tint out of new work).
- Accent: lamp amber for primary/focus/attention; dawn gradient edge = running.
- Status: review violet, queued steel, success sage, failed ember, draft muted.
- Type: Space Grotesk display / Inter UI / IBM Plex Mono for ids, costs, paths,
  timestamps, transcripts. Numerals that matter get display treatment.
- Focus ring on everything; prefers-reduced-motion respected; hover affordance
  on every interactive element.

## Quality gate before calling a screen done

- Screenshot at ~1440px: is the viewport used? Is anything discoverable only by
  scrolling that shouldn't be? Would this pass as a Linear page?
- Keyboard: cursor, actions, escape/enter paths all work.
- Run the `web-design-guidelines` review skill (repo `.claude/skills/`) over the
  changed files and fix real findings.
