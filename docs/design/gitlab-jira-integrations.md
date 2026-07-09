# RFC: GitLab and Jira Integrations (read, link, import)

**Status**: Draft for owner review
**Date**: 2026-07-08
**Author**: design investigation agent
**Depends on**: `docs/design/projects.md`, `docs/design/providers-and-endpoints.md`

## Product intent

Connect a repo to nightdesk, then view its GitLab issues and merge requests
inside nightdesk. Jira is a sibling integration through the same architecture.

Positioning constraint (fixed): nightdesk tickets stay separate from
GitLab/Jira issues. A nightdesk ticket is an agent prompt with an execution
lifecycle. A GitLab or Jira issue is usually not a runnable prompt. So the
integration is **read, view, link** first. There is no sync engine. The two
write-shaped actions are explicit, user- or agent-initiated:

1. **Import as draft ticket** ("make a ticket from this issue") — a one-time
   copy action, not a subscription.
2. **Create MR from a run's branch** (v2) — pushes work out, then tracks the
   MR's state back onto the ticket.

Everything else is a cached read of the external system.

## 1. External API research

### GitLab

Self-hosted GitLab is a first-class case. Every API below takes a
per-connection `base_url` (default `https://gitlab.com`); paths are identical
on gitlab.com and self-managed.

**Surfaces.** REST v4 covers everything v1 needs:

- `GET /api/v4/projects/:id` — project lookup; `:id` accepts the numeric id
  or URL-encoded `path/with/namespace`.
- `GET /api/v4/projects/:id/issues?state=opened&search=&order_by=updated_at`
- `GET /api/v4/projects/:id/issues/:iid` (+ `/notes` for comments, later)
- `GET /api/v4/projects/:id/merge_requests?state=opened|merged|closed`
- `GET /api/v4/projects/:id/merge_requests/:iid` — includes `state`,
  `merge_status`, `head_pipeline`, `source_branch`, `target_branch`.
- `GET /api/v4/projects/:id/merge_requests?source_branch=X` — find the MR for
  a run's branch.
- `POST /api/v4/projects/:id/merge_requests` — create MR; required:
  `source_branch`, `target_branch`, `title` (v2).
- Pagination: `page`/`per_page` (max 100) with `X-Next-Page` headers; keyset
  pagination exists but offset is fine at our volumes.

GraphQL (`/api/graphql`) exists and would batch multi-repo reads into one
request, but it adds a second client, its own complexity-based rate scoring,
and more version drift across self-hosted instances. REST-only for now; the
client interface below leaves room to swap.

**Auth.** Three realistic options:

- **Personal access token** with `read_api` scope (or `api` once MR creation
  lands). Works on every GitLab back to ancient versions, works self-hosted,
  is what people already use for scripts. Header: `PRIVATE-TOKEN: <token>`
  (or `Authorization: Bearer`, both accepted).
- **Project / group access tokens** — same wire format as a PAT, scoped to a
  project or group, with their own bot identity. Zero extra code: they are
  just PATs from the client's point of view. Recommended in docs for
  least-privilege setups.
- **OAuth 2.0** — needs an app registration per GitLab instance plus a
  redirect/device flow. Wrong cost/benefit for a single-admin, often-LAN
  deployment. Rejected for v1/v2 (revisit only if nightdesk grows multi-user
  auth).

**Rate limits.** Self-managed: the authenticated API limit is off by default;
when enabled the default is 7,200 requests per user per hour (~120/min).
gitlab.com has per-endpoint limits well above anything nightdesk will do.
Responses carry `RateLimit-Remaining` / `RateLimit-Reset` headers and 429 +
`Retry-After` on trip. Our background refresh budget (tens of requests per
poll cycle) is orders of magnitude below all of these; the client still must
honor 429/`Retry-After` and back off.

**Webhooks vs polling.** Project webhooks (issue events, MR events, note
events) push instantly but require the GitLab instance to reach nightdesk
over HTTP. nightdesk typically runs on localhost or a LAN box; gitlab.com can
never call it, and even self-hosted GitLab may not route to it. Polling is
therefore the baseline and webhooks are a v2 opt-in accelerator for
self-hosted deployments where connectivity exists (endpoint
`POST /api/v1/webhooks/gitlab/{connection_id}` guarded by GitLab's
`X-Gitlab-Token` secret; a received event just invalidates/refreshes the
cache rows it names — no state machine of its own).

### Jira

Two materially different products behind one name; the connection must record
which (`provider = jira_cloud | jira_dc`).

**Jira Cloud.**

- Base URL: `https://<site>.atlassian.net`.
- Auth: Basic auth with `email:api_token` (classic, unscoped tokens). The
  newer *scoped* API tokens only work against
  `https://api.atlassian.com/ex/jira/{cloudId}` base URLs — supportable
  later, but v1 documents classic tokens. Note: Atlassian now enforces token
  expiry (max ~1 year), so the connection needs a friendly re-auth error
  state, not just a silent failure.
- Search: the old `/rest/api/2|3/search` endpoints were **removed** (traffic
  blocked end of October 2025). Use `GET|POST /rest/api/3/search/jql` with
  `nextPageToken` pagination and an explicit `fields` list. JQL per repo
  link: `project = KEY AND statusCategory != Done ORDER BY updated DESC`.
- Single issue: `GET /rest/api/3/issue/{key}`. Descriptions are Atlassian
  Document Format (ADF) JSON, not markdown — the renderer needs an
  ADF-to-text/HTML pass (flatten to plain text in v1-lite; there are small
  ADF walkers, no heavyweight dependency needed).
- Rate limits: points/burst based, per user+app, with `Retry-After` on 429.
  API-token traffic got explicit rate limits in November 2025. Same client
  rule as GitLab: honor 429, back off, budget polls.

**Jira Server / Data Center** (brief): auth is a personal access token sent
as `Authorization: Bearer` (Basic-with-token does not work), search is still
`/rest/api/2/search` with `startAt`/`maxResults` offset pagination, and
descriptions are wiki markup or plain text rather than ADF. Roughly three
small divergences (auth header, search endpoint+pagination, body format), all
of which fit behind the same client interface. Do not attempt to paper over
them with one code path.

**Jira has no MR concept.** The MR-lifecycle features are GitLab-only; Jira
participation ends at issue read/link/import. (Jira dev-status links to
GitLab MRs exist in Cloud but ride Atlassian's own GitLab app — out of
scope.)

## 2. Current codebase facts that shape the design

- **Project** (`db/models.py:162`, `domain/projects.py`) is a saved work
  context: `source_path`, workspace defaults, color, position. Per
  `docs/design/projects.md` it is deliberately *not* a permissions boundary
  or a page. It has no repo identity beyond `source_path` — no remote URL is
  stored anywhere at rest.
- **TicketWorkspace** (`db/models.py:545`) captures per-run git facts:
  `repo_root`, `branch`, `base_ref`, `head_sha`. This is where "which branch
  did this run produce" already lives — the MR feature reads it, it needs no
  schema change.
- **ProviderEndpoint** (`domain/providers.py`) is the credential pattern to
  copy: encrypted-at-rest via `ProfileSecretBox` (Fernet keyed from the
  bearer token by HKDF), API responses return only `credential_set: bool`,
  resolution never raises (log + degrade). The entity itself is *not*
  reusable here — it models model-inference surfaces (protocol kinds,
  harness locks, model menus) and is wired into the backend compatibility
  gate. Bending it to hold a GitLab PAT would pollute both the providers UI
  and the gate.
- **Run tokens** (`domain/run_tokens.py`) already give sandboxed agents a
  scoped API principal: `SELF_SCOPES` auto-granted per run,
  `GRANTABLE_SCOPES` opt-in per profile. The agent surface below extends
  `GRANTABLE_SCOPES`; no new auth machinery.
- **SPA conventions** (`frontend/src`): side-peek not navigate
  (`TicketPeek.tsx`), a compact editable `PropertiesRail` with bordered
  `Section`s (Workspaces / Additional directories / Dependencies), Radix
  `Tooltip` wrapper for all hints, `StatusPill` + token classes (`ink-*`
  surfaces, `moon-*` text, `lamp` accent, `jade`/`failed` status colors).
  Projects currently surface as board lenses and a Settings section
  (`ProjectsSection.tsx` + `/projects/{id}/activity`); the `ui/project-pages`
  branch adds addressable project pages but is unmerged.
- Alembic head on the integration branch is `0026_session_kind`; new tables
  chain from whatever head exists at implementation time.

## 3. Domain model

Three new entities plus one join table. Names chosen so nothing collides
with the existing Provider/ProviderEndpoint vocabulary.

### Connection

One authenticated external system. Instance-global, like Providers — it lives
in Settings, not inside a project, because one GitLab instance serves many
repos and many nightdesk projects, and because projects are explicitly not a
security boundary.

```
connections
  id            uuid pk
  name          str unique        "company gitlab", "prod jira"
  provider      str               gitlab | jira_cloud | jira_dc
  base_url      str               https://gitlab.example.com / https://x.atlassian.net
  auth_kind     str               pat | api_token_basic   (room for oauth later)
  credential    text nullable     encrypted (ProfileSecretBox), never returned;
                                  for jira_cloud this is an encrypted JSON
                                  {"email": ..., "token": ...}
  status        str               ok | auth_failed | unreachable | unchecked
  status_detail text nullable     last error message, shown in Settings
  last_checked_at datetime nullable
  created_at / updated_at
```

Same v1 tradeoff as profile secrets: rotating the bearer token invalidates
stored credentials and the user re-enters them. `status` is written by an
explicit "Test" action and by background refresh failures, so a dead token is
visible in Settings instead of silently returning empty issue lists.

### RepoLink

One external project on one connection: a GitLab project, or a Jira project
key. This is the unit users browse and the unit nightdesk projects attach.

```
repo_links
  id             uuid pk
  connection_id  fk connections, ondelete restrict
  external_kind  str        gitlab_project | jira_project
  external_id    str        GitLab numeric project id (stable across renames)
                            or Jira project key
  external_path  str        "group/repo" or "KEY" — display + URL building
  display_name   str        cached project title
  git_remote_url str nullable  normalized clone URL (see matching, below);
                               null for Jira
  web_url        str        cached, for link-outs
  created_at / updated_at
  unique (connection_id, external_kind, external_id)
```

**Git-remote matching.** When the user attaches a repo to a project, the UI
runs `git remote get-url origin` under the project's `source_path` (via the
existing fs/inspection surface), normalizes it
(`git@host:g/r.git` ≡ `https://host/g/r` → `host/g/r`), and pre-selects the
matching GitLab project from a typeahead. The normalized form is stored on
the RepoLink so later features (MR creation from a run's `repo_root`) can
resolve workspace → RepoLink without asking. Matching is a *suggestion*, the
user's explicit pick is authoritative — remote URLs are too ambiguous
(mirrors, ssh aliases) to be the sole key.

### project_repo_links (join)

```
project_repo_links
  project_id   fk projects, ondelete cascade
  repo_link_id fk repo_links, ondelete cascade
  position     int
  pk (project_id, repo_link_id)
```

M:N because the brief's premise is real in both directions: a project may
span several repos (app + infra), and one repo may back several projects
(nightdesk-the-repo could back a "nightdesk ui" and "nightdesk backend"
lens). This join is what makes issues/MRs appear under a project lens.

### ExternalLink (ticket ↔ issue/MR)

```
external_links
  id            uuid pk
  ticket_id     fk tickets, ondelete cascade, index
  repo_link_id  fk repo_links, ondelete restrict
  kind          str      issue | merge_request | jira_issue
  external_iid  str      GitLab iid or Jira key
  role          str      fixes | references | produced_mr | imported_from
  url           str      cached web url
  title         str      cached
  state         str      cached: opened|closed (issue), opened|merged|closed (MR),
                         or Jira statusCategory (todo|in_progress|done)
  state_detail  json nullable   small provider blob (pipeline status, merge sha,
                                assignees) for chips/peeks
  synced_at     datetime nullable
  author_kind   str      admin | agent      (same acting-principal pattern
                                             as diff_comments.author_kind)
  created_at / updated_at
  unique (ticket_id, repo_link_id, kind, external_iid)
```

The link rows are the only *persistently synced* external state. Everything
else (issue lists, issue bodies) is fetched through a short-lived cache. That
keeps the freshness problem tiny: the background job refreshes exactly the
rows that drive always-visible UI (board chips, rail sections), and browse
surfaces are as fresh as the moment you open them.

`role` semantics: `fixes` — this ticket is working that issue; `references`
— related, no lifecycle implication; `produced_mr` — this MR was created from
this ticket's run branch; `imported_from` — this ticket was created by the
import action from that issue. One ticket can carry any number of links.

### Caching and freshness

- **Browse reads** (issue/MR lists, single-item detail for the peek): proxied
  live through the nightdesk API with a per-URL in-process TTL cache
  (default 60 s) to absorb repeated opens and agent re-reads. No DB rows.
  Failures return the provider error plus the connection's `status` so the
  UI can distinguish "no issues" from "token dead".
- **Linked items** (`external_links`): refreshed by the worker's existing
  periodic loop every 5 minutes (configurable) for links on non-archived
  tickets, batched per connection, honoring 429/`Retry-After`. GitLab batch
  trick: one `GET .../merge_requests?iids[]=1&iids[]=5...` per repo per
  cycle instead of N item fetches; same with `issues?iids[]=`. Jira: one JQL
  `key in (A-1, B-2)` search per connection.
- A state change detected by refresh emits an internal event (see §6).

## 4. HTTP API surface

All under the JSON `/api/v1/*` surface; skills (`nightdesk-api`,
`nightdesk-ticket-ops`) get updated in the same change per CLAUDE.md.

Admin (bearer):

```
POST   /api/v1/connections                     create (credential write-only)
GET    /api/v1/connections                     list (credential_set flag only)
PATCH  /api/v1/connections/{id}
DELETE /api/v1/connections/{id}                409 if repo_links exist
POST   /api/v1/connections/{id}/test           live auth check, writes status
GET    /api/v1/connections/{id}/projects?search=   provider project typeahead

POST   /api/v1/repo-links                      {connection_id, external_id, ...}
GET    /api/v1/repo-links
DELETE /api/v1/repo-links/{id}                 409 if external_links exist

PUT    /api/v1/projects/{pid}/repo-links       set/attach (ordered ids)
GET    /api/v1/projects/{pid}/repo-links

GET    /api/v1/repo-links/{id}/issues?state=&search=&page_token=
GET    /api/v1/repo-links/{id}/issues/{iid}
GET    /api/v1/repo-links/{id}/merge-requests?state=&page_token=
GET    /api/v1/repo-links/{id}/merge-requests/{iid}

POST   /api/v1/tickets/{tid}/external-links    {repo_link_id, kind, external_iid, role}
GET    /api/v1/tickets/{tid}/external-links
DELETE /api/v1/tickets/{tid}/external-links/{link_id}
POST   /api/v1/external-links/{id}/refresh     force one-item resync

POST   /api/v1/repo-links/{id}/import-ticket   {kind:"issue", external_iid,
                                                project_id?} → draft TicketOut
```

`import-ticket` creates a **draft** ticket: title = issue title, prompt = a
template that *quotes* the issue body and links it, rather than pasting the
body as if it were a prompt (issue text usually isn't one; the human edits
the draft before queueing). It attaches an `imported_from` link and infers
`project_id` from the repo link's attachments when unambiguous. It never
transitions the ticket — import is authoring, not execution.

v2 additions:

```
POST /api/v1/tickets/{tid}/merge-requests     create MR from a run branch
POST /api/v1/webhooks/gitlab/{connection_id}  inbound events (secret-checked)
```

### Pagination

Provider pagination leaks through deliberately as an opaque `page_token`
(GitLab: page number; Jira Cloud: `nextPageToken`). The SPA and agents treat
it as opaque; no attempt to unify into offsets, since Jira Cloud no longer
has them.

## 5. Agent surface (agent-native angle)

Agents already call back into the API with per-run scoped tokens. Extend
`GRANTABLE_SCOPES` in `domain/run_tokens.py`:

- `integrations.read` — GET on repo-link issue/MR endpoints and on
  `external-links` of any ticket. Read-only, so project-scoping it buys
  little and costs a lookup on every call; instance-wide read is acceptable
  for v1 (open question 4 if the owner disagrees).
- `integrations.link.self` — POST/DELETE `external-links` on the run's own
  ticket only (enforced by the existing `*.self` ticket-match check in
  `api/auth.py`).
- v2: `integrations.create_mr.self` — the MR-creation endpoint on own ticket.

Credentials never reach the sandbox. The agent talks to nightdesk, nightdesk
talks to GitLab/Jira. That means profiles with `network_mode=off` still get
issue context, and a prompt-injected agent cannot exfiltrate the PAT.

**End-to-end: "agent picks up a GitLab issue and works it."**

1. Human (or a triage agent with `ticket.create`) finds issue `#123` in the
   project's Issues tab and hits *Import as ticket* → draft ticket with an
   `imported_from` link, prompt template referencing the issue.
2. Human reviews the draft prompt, queues it. Nothing here differs from any
   other ticket.
3. The running agent, holding `integrations.read`, GETs the issue body and
   discussion through nightdesk to ground itself (the prompt told it the
   iid; it does not need GitLab credentials or network).
4. Agent does the work; run ends on branch `fix/issue-123` (recorded on
   `TicketWorkspace.branch`).
5. v2: human clicks *Create MR* on the ticket (or the agent calls
   `create_mr.self` if granted); nightdesk resolves workspace `repo_root` →
   RepoLink via `git_remote_url`, creates the MR with the issue reference in
   the description, links it back with role `produced_mr`.
6. The refresh loop tracks the MR; when it merges, the chip flips to merged
   and an event fires (§6). The visible chain on the ticket rail is:
   `imported_from #123` → `produced_mr !456 (merged)`.

Prompt-injection note: issue bodies are untrusted input flowing into
prompts (import) and into agent context (reads). The import template already
frames the body as quoted data. The read API should do the same framing in
its skill documentation; nightdesk cannot sanitize semantics, but it must
never auto-execute anything based on issue content (and this design has no
such path — every transition stays human/agent-explicit).

## 6. MR lifecycle and the work-acknowledgement interface

What this design owns: creating `produced_mr` links (v2), keeping
`external_links.state` fresh, and rendering state on ticket surfaces.

What it deliberately does not own: what happens to a ticket when its MR
merges, or to a linked issue when its ticket archives. That is the separate
work-acknowledgement design. The interface handed to it is one internal
event, emitted by the refresh loop and the webhook receiver:

```
external_link.state_changed
  { ticket_id, external_link_id, kind, role,
    old_state, new_state, occurred_at }
```

plus the queryable `external_links` table itself. The ack design can decide
"MR merged → nudge ticket toward archive" or "issue closed upstream → flag
the ticket" without this integration knowing those policies exist. The only
lifecycle-ish behavior shipped here is presentational: a merged
`produced_mr` chip renders in jade, a closed-without-merge one in the failed
tone, so the board already *shows* completion before any policy acts on it.

One optional GitLab-native hook: when creating an MR from a ticket that has
a `fixes` link, prepend `Closes #<iid>` to the MR description so GitLab
itself closes the issue on merge. Off by default; checkbox on the create-MR
dialog. This keeps issue-closing inside GitLab's own semantics instead of
nightdesk writing to issues.

## 7. UX

### Where things live

- **Settings → Connections** (new section beside Providers): connection
  cards with provider icon, base_url, masked credential (`credential_set`),
  status dot + `status_detail`, Test button. Below each connection, its repo
  links with attach counts. This mirrors `ProvidersSection` closely enough
  to reuse its layout skeleton.
- **Project ↔ repos**: attach/detach in the project editor
  (`ProjectsSection`, and the project page if `ui/project-pages` merges). A
  "Connect repo…" affordance runs the remote-matching typeahead.
- **Browsing issues/MRs — project lens.** Projects are lenses, and the
  Tickets page is the home surface. Add a lens-scoped panel: when the board
  is filtered to a project with repo links, the FilterBar grows an
  `Issues` / `MRs` toggle (count-badged) that swaps the board area for the
  external list. If `ui/project-pages` merges, the same two components mount
  as project-page tabs — the components are written lens-first so both
  homes work.
- **Issue/MR peek.** Clicking a row opens a side-peek (same pattern and
  width as `TicketPeek`), never navigates. Link-out to the provider is an
  explicit icon button with a `Tooltip`.
- **Ticket rail — Linked items.** New `Section` in `PropertiesRail` between
  Dependencies and the timestamps, following the Dependencies row idiom.
- **Board card chip.** `BoardCard` gets one compact chip when a
  `produced_mr` link exists (highest-signal state only; issues don't chip —
  cards are already dense).

### Text mocks

Settings → Connections:

```
┌ Connections ────────────────────────────────────────────────┐
│ ● company gitlab            gitlab · https://git.corp.dev   │
│   token ●●●●●●●● set        status: ok · checked 2m ago     │
│   [Test] [Edit] [Delete]                                     │
│   repos:  group/app (2 projects) · group/infra (1)  [+ Add] │
│                                                              │
│ ● prod jira                 jira_cloud · x.atlassian.net    │
│   token ●●●●●●●● set        status: auth_failed ⚠           │
│   "401: token expired 2026-07-01" [Test] [Edit]             │
└──────────────────────────────────────────────────────────────┘
```

Project lens, Issues panel (FilterBar toggle active):

```
[ project: nightdesk ▾ ]  [ Tickets | Issues (14) | MRs (3) ]  [state: open ▾] [search]
┌──────────────────────────────────────────────────────────────┐
│ #482  Worker loses heartbeat on suspend      bug, worker  3d │
│ #479  Board drag misorders within column     ui           5d │
│ #471  ...                                                    │
└──────────────────────────────────────────────────────────────┘
   row click → issue peek; row shows a small ⧉ tickets-badge when
   external_links already reference it (back-link visibility)
```

Issue peek (right side-peek):

```
│ #482 Worker loses heartbeat on suspend          [↗] [✕]     │
│ open · bug, worker · @jdoe · updated 3d ago                  │
│ ──────────────────────────────────────────────────────────── │
│ (rendered markdown body)                                     │
│ ──────────────────────────────────────────────────────────── │
│ Worked by:  ▸ ticket "fix worker heartbeat" (review)         │
│ ──────────────────────────────────────────────────────────── │
│ [ Import as ticket ]  [ Link to ticket… ]                    │
```

Ticket rail, Linked items section:

```
│ LINKED ITEMS                                                 │
│ ⊙ #482 Worker loses heartbeat…      fixes      open    ✕    │
│ ⇄ !517 fix: heartbeat on suspend    MR         merged  ✕    │
│ + Link issue or MR                                           │
```

Board card chip (right edge of the meta row): `⇄ !517` with tone by state
(open = amber outline, merged = jade, closed = failed) and a `Tooltip`
carrying title + state + target branch.

All hints use the `Tooltip` wrapper; chips/pills reuse `StatusPill` tones and
token classes; every list row press opens a peek, never a route change.

## 8. Phasing

**v1 — GitLab read + link + import.**
Migration (4 tables), `domain/integrations/` (client interface + GitLab REST
client + cache + refresh hook in the worker loop), routes
(`connections`, `repo_links`, `external-links`, `import-ticket`), run-token
scopes, SPA (Connections settings, project attach, Issues/MRs panel, peek,
rail section), skills update. No writes to GitLab at all.
Sizing: roughly five agent tickets — (1) schema+domain+connection routes,
(2) GitLab client+proxy endpoints+cache, (3) links+import+scopes+refresh
loop, (4) SPA settings+attach, (5) SPA panels+peek+rail. Comparable in
total to the providers-endpoints build; call it a week of runs plus review.

**v2 — MR outbound + freshness upgrades.**
Create-MR endpoint + dialog (branch pick from `TicketWorkspace`, `Closes #`
checkbox), `produced_mr` chips on `BoardCard`, `state_changed` event
emission, optional GitLab webhook receiver, 429-aware batching hardening.
Sizing: two to three tickets.

**v3 — Jira read + link + import (lite).**
Jira Cloud client behind the same interface (`search/jql`, ADF flattening,
Basic auth), `jira_dc` variant (Bearer + old search) if the owner runs DC,
Issues panel + peek reuse with a Jira renderer, no MR features. Sizing: two
tickets. It could run parallel to v2 since the entities already carry it,
but sequencing it third lets GitLab v1 shake out the Connection/RepoLink UX
before a second provider hardens it.

## 9. Decisions

1. **New Connection entity; do not reuse Provider/ProviderEndpoint.** Those
   model inference surfaces and feed the backend compatibility gate; a
   GitLab PAT in that table would corrupt both the UI and the gate
   semantics. Only the *encryption and masking pattern* is copied.
2. **Connections are instance-global; projects attach RepoLinks M:N.**
   Per-project credentials were rejected (projects are not a security
   boundary per `docs/design/projects.md`; one GitLab serves many projects).
   Per-workspace was rejected (workspaces are per-run artifacts with the
   wrong lifetime).
3. **Read/link/import, no sync.** No two-way mirroring, no auto-created
   tickets from issues, no writing ticket state to issues. Matches the
   positioning constraint and avoids the hardest failure modes (loops,
   conflicting truths).
4. **Polling baseline, webhooks opt-in v2.** nightdesk is usually not
   reachable from the provider; volumes are trivial; linked-item refresh
   every 5 minutes is fresh enough for chips.
5. **Persist only linked items; proxy-browse everything else** with a 60 s
   TTL. Keeps cache invalidation nearly nonexistent.
6. **REST clients, not GraphQL**, behind a small per-provider interface
   (`list_issues`, `get_issue`, `list_mrs`, `get_mr`, `find_mr_by_branch`,
   `create_mr`, `test_auth`, opaque `page_token`).
7. **PAT-family auth only in v1** (GitLab PAT/project token; Jira classic
   API token; Jira DC PAT). OAuth rejected for cost/benefit.
8. **Explicit user pick for repo mapping, git-remote match as suggestion
   only.** Remote URLs are ambiguous; the stored normalized URL still powers
   automatic workspace→repo resolution for MR creation.
9. **Import creates a draft with a quoting template**, never a verbatim
   prompt and never a non-draft status.
10. **Lifecycle policy is out of scope**; ship the `state_changed` event and
    the queryable link table as the interface to the work-acknowledgement
    design.
11. **Agent access via run-token scopes, credentials never in the sandbox.**

### Alternatives rejected (summary)

- Reuse ProviderEndpoint for forge credentials — semantic pollution (§9.1).
- Credential or connection on the Project row — wrong boundary (§9.2).
- Two-way issue↔ticket sync — positioning violation, loop risk (§9.3).
- Webhooks-first — reachability (§9.4).
- Full local mirror of issues/MRs — cache invalidation cost with no v1
  payoff; revisit only if offline browsing or cross-repo search over issues
  becomes a goal (§9.5).
- GraphQL-first GitLab client — second client + version drift (§9.6).
- OAuth flows — app registration per instance, redirect plumbing (§9.7).
- git-remote-only implicit mapping — ambiguity (§9.8).
- A dedicated top-level "Issues" nav page — projects are lenses; a global
  cross-provider issue browser is a different product decision, and the
  lens-scoped panel covers the stated intent.

## 10. Open questions for the owner

1. **Jira variant**: Cloud, DC, or both? Determines whether `jira_dc` code
   ships in v3 or stays a stub, and whether ADF rendering matters at all.
2. **Tickets without a project**: linking works regardless (ExternalLink
   hangs off the ticket), but browsing is project-lens-scoped. Is a repo
   picker inside the ticket "Link issue or MR" flow (searching across all
   repo links) enough for project-less tickets? (Design assumes yes.)
3. **Where browsing lives if `ui/project-pages` never merges**: the FilterBar
   toggle on the Tickets page is the standalone answer — confirm you're
   happy with issues borrowing the board area rather than getting a page.
4. **Agent read breadth**: is instance-wide `integrations.read` acceptable,
   or should it be constrained to repo links attached to the run ticket's
   project? (Constraining is easy to add later; loosening is not, once
   prompts depend on it.)
5. **MR creation actor**: v2 ships the human button. Do you want agents
   granted `integrations.create_mr.self` from day one, or keep MR creation
   human-only until the ack design lands? (Interacts with provenance —
   `author_kind` is recorded either way.)
6. **`Closes #iid` default**: off (proposed) or on when a `fixes` link
   exists?
7. **Import template wording**: quoting template proposed here; want to see
   two or three concrete template drafts before implementation?

## Sources

- GitLab rate limits: https://docs.gitlab.com/security/rate_limits/ and
  https://docs.gitlab.com/administration/settings/user_and_ip_rate_limits/
- GitLab REST API: https://docs.gitlab.com/api/rest/ ·
  merge requests: https://docs.gitlab.com/api/merge_requests/
- Jira Cloud rate limiting: https://developer.atlassian.com/cloud/jira/platform/rate-limiting/
  and https://www.atlassian.com/blog/platform/evolving-api-rate-limits
- Jira search endpoint removal: https://community.atlassian.com/forums/Jira-questions/When-are-JQL-search-endpoints-rest-api-2-search-and-rest-api-3/qaq-p/3029221
- Jira Cloud REST v3: https://developer.atlassian.com/cloud/jira/platform/rest/v3/intro/ ·
  basic auth: https://developer.atlassian.com/cloud/jira/platform/basic-auth-for-rest-apis/
- Atlassian API tokens (scopes, expiry): https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/
- Jira DC PATs: https://developer.atlassian.com/server/jira/platform/personal-access-token/
