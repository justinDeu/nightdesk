# nightdesk frontend

The ground-up SPA rebuild. Vite + React 18 + TypeScript (strict), TanStack
Router + Query, Tailwind v4 with a bespoke token block, Radix primitives styled
from scratch. Talks only to the `/api/v1/*` JSON surface.

## Design language — "dusk console"

Dark-first, used at night, deliberately not the near-black + acid-green look.
All tokens live in `src/styles/theme.css` (`@theme` block). Surfaces are `ink-*`,
text is `moon-*`, the accent is `lamp` (desk-lamp amber). Running work carries
the **dawn edge**: an animated amber→coral hairline (static under
`prefers-reduced-motion`). See `/dev/kitchen-sink` for every primitive in every
state — that page is how the design is reviewed.

## Dev workflow

The SPA needs a JSON API to talk to. In dev, vite proxies `/api` and `/auth` to
a **throwaway** test instance on `:8799` — never the live instance on `:8765`.

```bash
# 1. Start a test API on :8799 (from the repo root, separate terminal).
#    Point it at a scratch DB so you never touch live data.

# 2. Start the SPA dev server (proxies to :8799).
cd frontend
npm install
npm run dev            # http://localhost:5173

# override the proxy target if your test API is elsewhere:
NIGHTDESK_API=http://127.0.0.1:8799 npm run dev
```

The app renders gracefully when the API is absent: the worker pill shows
"API offline" and lists fall back to empty states. A 401 from any request
redirects to `/login`.

## Build & typecheck

```bash
npm run build          # tsc --noEmit + vite build → dist/
npm run typecheck      # tsc --noEmit only
```

Fonts (Space Grotesk / Inter / IBM Plex Mono) are self-hosted via `@fontsource`
and bundled into `dist/` — no network fetch at runtime.

## Auth

`/login` posts the admin bearer token to `POST /auth/login` (form field
`bearer`, matching `src/nightdesk/api/routes/auth.py`). The server exchanges it
for the signed `nightdesk_session` cookie; the token is never stored in JS.

## Layout

```
src/
  api/          typed fetch client + TanStack Query hooks, one file per resource
  ui/           primitive component set (Button, Dialog, StatusPill, …)
  components/   app shell (SideNav, TopStrip, WorkerPill, Page)
  routes/       page components (pages.tsx stubs, login, kitchenSink)
  lib/          queryClient, cn, status/format helpers
  styles/       theme.css (tokens, base, animations)
  router.tsx    code-based typed route tree
  main.tsx      providers + mount
```

## Status

P1[A] scaffold: design system, app shell, auth, typed API client, primitive set,
and routes stubbed with empty states. Screens are filled in P2–P3.
