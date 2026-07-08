# Session-suite feature scopes

Four features scoped 2026-07-08 off base/session-suite (= feat/providers-endpoints + ui/ground-up merge):

- mid-run-steering.md — queue + steer messages into live runs (migration 0023_steer_messages)
- diff-comments.md — inline review comments on run diffs (migration 0024_diff_comments)
- k8s-executor.md — Kubernetes execution target for runs (migration 0025_execution_target)
- interactive-sessions.md — ticketless chat sessions (migration 0026_session_kind)

## Integration contract

**Ordering decision:** the k8s design's Phase 1 (extract run_one's execution body
into `src/nightdesk/executors/` with a behavior-preserving `LocalExecutor`) lands
on base/session-suite BEFORE the four feature branches fork. Steering's worker
hooks and sessions' finish-path guards then target the post-extraction layout,
so the master merge never has to reconcile feature edits against moved code.

**Branches** (all off base/session-suite, post-extraction):
- feat/mid-run-steering
- feat/diff-comments
- feat/k8s-executor (Phases 2-4 only; Phase 1 is already in base)
- feat/interactive-sessions

**Migrations:** each branch's migration sets
`down_revision = "0022_providers_and_endpoints"` so branches migrate standalone.
The integration branch (integration/session-suite) re-chains them linearly:
0023 <- 0024 <- 0025 <- 0026.

**Shared-surface notes:**
- `next_run_context` is the convergence point for steering (queue drain) and
  diff comments (request-changes bundling). Both APPEND; diff-comments adds
  `append_next_run_context` in domain/tickets.py; steering's drain uses append
  semantics too. Integration keeps one helper.
- Sessions v1 rejects mid-turn messages (409). Steering + sessions integration
  (steer a session turn) is an explicit follow-up, not in either branch.
- The run-token write-back routes added by k8s Phase 2 (POST transcript/diff/
  result) are also the auth pattern diff-comments Phase 4 (agent reads) will
  use later; diff-comments ships admin-only in v1.
- The `~/.claude` skill files (nightdesk-api, nightdesk-ticket-ops) are NOT
  edited on feature branches (shared global dir; parallel edits collide).
  Integration updates them once for all four features.
- run_one finish-path ownership: transitions/webhook/dependents stay in
  run_one (k8s design); sessions' kind-guards land there; steering's
  run-completion drain lands there too. Expected small, resolvable overlap.
