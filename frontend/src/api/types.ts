/**
 * TypeScript mirrors of the nightdesk pydantic schemas
 * (src/nightdesk/api/schemas.py). Kept hand-synced; when schemas.py changes,
 * update here and the nightdesk-api / nightdesk-ticket-ops skills in the same
 * change (see CLAUDE.md).
 */

// --- Lifecycle -----------------------------------------------------------------

export type TicketStatus = "draft" | "queued" | "running" | "review" | "archived";
export const LIFECYCLE_STATUSES: TicketStatus[] = [
  "draft",
  "queued",
  "running",
  "review",
  "archived",
];

export type WorkspaceKind = "directory" | "git_worktree" | "in_place" | "worktree";
export type WorkspaceRole = "primary" | "linked";
export type WorkspaceAccess = "read_write" | "read_only";
export type WorkspaceRetention =
  | "preserve"
  | "cleanup_on_success"
  | "cleanup_after_review";

// --- Workspaces ----------------------------------------------------------------

export interface TicketWorkspaceIn {
  role?: WorkspaceRole;
  label?: string;
  kind: WorkspaceKind;
  access?: WorkspaceAccess;
  source_path?: string | null;
  worktree_name?: string | null;
  worktree_path?: string | null;
  branch?: string | null;
  base_ref?: string | null;
  retention?: WorkspaceRetention;
}

export interface TicketWorkspaceOut {
  id: string;
  ticket_id: string;
  run_id: string | null;
  role: string;
  label: string;
  kind: string;
  access: string;
  source_path: string | null;
  resolved_path: string | null;
  repo_root: string | null;
  git_common_dir: string | null;
  relative_path: string | null;
  worktree_name: string | null;
  worktree_path: string | null;
  branch: string | null;
  base_ref: string | null;
  base_sha: string | null;
  head_sha: string | null;
  retention: string;
  state: string;
  position: number;
}

export interface AdditionalDir {
  path: string;
  mode?: "rw" | "ro";
}

export interface ToolchainOverrides {
  enable?: string[];
  disable?: string[];
  extra_paths?: string[];
}

// --- Labels & dependencies -----------------------------------------------------

export interface LabelOut {
  id: string;
  name: string;
  color: string;
}

export interface DependencyOut {
  id: string;
  ticket_id: string;
  depends_on_id: string;
  depends_on_title: string;
  depends_on_status: string;
  created_at: string;
}

export interface DependencyCreate {
  depends_on_id: string;
}

// --- Tickets -------------------------------------------------------------------

export interface TicketOut {
  id: string;
  title: string;
  prompt: string;
  status: string;
  /** 'ticket' (board work) | 'session' (ad-hoc interactive chat). */
  kind: string;
  priority: number;
  position: number;
  project_id: string | null;
  profile_id: string | null;
  permission_overrides: Record<string, unknown> | null;
  toolchain_overrides: Record<string, unknown> | null;
  additional_dirs: AdditionalDir[];
  workspaces: TicketWorkspaceOut[];
  labels: LabelOut[];
  run_now: boolean;
  commit_on_finish: boolean | null;
  scheduled_after: string | null;
  current_run_id: string | null;
  next_run_context: string | null;
  next_run_context_updated_at: string | null;
  dependencies: DependencyOut[];
  created_at: string;
  updated_at: string;
}

export interface TicketCreate {
  title: string;
  prompt?: string;
  status?: string | null;
  priority?: number;
  /** Required for non-inbox creates; may be null when status="inbox"
   *  (triage's completeness gate demands it at promote time). */
  profile_id?: string | null;
  project_id?: string | null;
  permission_overrides?: Record<string, unknown> | null;
  toolchain_overrides?: ToolchainOverrides | null;
  additional_dirs?: AdditionalDir[];
  source_path?: string | null;
  workspace_mode?: WorkspaceKind;
  worktree_name?: string | null;
  worktree_path?: string | null;
  workspaces?: TicketWorkspaceIn[] | null;
  run_now?: boolean;
  commit_on_finish?: boolean | null;
  scheduled_after?: string | null;
}

export type TicketUpdate = Partial<Omit<TicketCreate, "profile_id" | "status">> & {
  profile_id?: string;
};

export interface TicketTransition {
  status: TicketStatus;
  position?: number | null;
}

export interface TicketReorder {
  status: TicketStatus;
  ticket_ids: string[];
}

export interface TicketContinue {
  message: string;
}

export interface TicketNewConversation {
  message?: string | null;
  profile_id?: string | null;
  workspace?: "keep" | "fresh";
}

// Conversation / run actions (JSON parity with the HTMX ticket-page forms).
export interface TicketResumeOrRetry {
  message?: string | null;
}
export interface TicketRestart {
  message?: string | null;
  workspace_policy: "recreate_in_place" | "fresh_path";
}
export interface TicketClone {
  title?: string | null;
  carry_context?: boolean;
}
export interface TicketNextRunContext {
  body: string;
}

// Mid-run steering: a live-run follow-up queue on the ticket's active conversation.
export type SteerState = "pending" | "delivering" | "delivered" | "cancelled";
export type SteerDeliveryMode = "at_turn" | "inject";
export interface SteerMessageOut {
  id: string;
  body: string;
  position: number;
  state: SteerState;
  delivery_mode: SteerDeliveryMode;
  delivered_run_id: string | null;
  created_at: string | null;
  delivered_at: string | null;
}
export interface SteerQueueOut {
  messages: SteerMessageOut[];
  capability: { inject: boolean };
}
export interface SteerMessageCreate {
  body: string;
  delivery_mode?: SteerDeliveryMode;
}

export interface AdditionalDirAdd {
  path: string;
  mode?: "rw" | "ro";
}

// Bulk (labels / archive / unarchive)
export interface BulkLabelsUpdate {
  ticket_ids: string[];
  label_ids: string[];
}
export interface BulkIdsOnly {
  ticket_ids: string[];
}

// Bulk
export interface BulkStatusUpdate {
  ticket_ids: string[];
  status: TicketStatus;
}
export interface BulkPriorityUpdate {
  ticket_ids: string[];
  priority: number;
}
export interface BulkProjectUpdate {
  ticket_ids: string[];
  project_id: string | null;
}
export interface BulkProfileUpdate {
  ticket_ids: string[];
  profile_id: string;
}
export interface BulkUpdateResult {
  updated: TicketOut[];
  skipped: Array<Record<string, unknown>>;
}

// --- Runs ----------------------------------------------------------------------

export interface RunOut {
  id: string;
  ticket_id: string;
  started_at: string;
  finished_at: string | null;
  exit_status: string | null;
  error_summary: string | null;
  worktree_path: string;
  transcript_path: string;
  pid: number | null;
  host: string;
  started_as_run_now: boolean;
  intent: string;
  parent_run_id: string | null;
  headless_policy_version: string | null;
  restart_workspace_policy: string | null;
  failure_kind: string | null;
  model_used: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
  cost_usd: number | null;
  sandbox_tool_paths: string[] | null;
}

// --- Run diff (structured, from GET /runs/{rid}/diff) --------------------------

export interface RunDiffRow {
  kind: "hunk" | "ctx" | "ins" | "del";
  gutter: string;
  text: string;
  line_no_old: string;
  line_no_new: string;
}
export interface RunDiffFile {
  path: string;
  old_path: string;
  new_path: string;
  binary: boolean;
  lines_added: number;
  lines_deleted: number;
  hunks: RunDiffRow[];
}
export interface RunDiff {
  files: RunDiffFile[];
  total_added: number;
  total_deleted: number;
  total_files: number;
  truncated: boolean;
  hidden_files: number;
  hidden_lines: number;
  error: string;
  branch: string | null;
  base_sha: string | null;
  head_sha: string | null;
  repo_root: string | null;
}

// --- Diff comments (line-anchored review comments on a run's diff) -------------

export interface DiffCommentOut {
  id: string;
  run_id: string;
  ticket_id: string;
  conversation_id: string | null;
  parent_id: string | null;
  file_path: string | null;
  side: string | null;
  line: number | null;
  anchor_head_sha: string | null;
  anchor_text: string | null;
  body: string;
  author_kind: string;
  author_run_id: string | null;
  resolved: boolean;
  resolved_at: string | null;
  delivered_at: string | null;
  created_at: string;
  updated_at: string | null;
  /** Anchor head differs from the live diff head — render against anchor_text. */
  outdated: boolean;
}

export interface DiffCommentCreate {
  parent_id?: string | null;
  file_path?: string | null;
  side?: "old" | "new" | null;
  line?: number | null;
  anchor_head_sha?: string | null;
  anchor_text?: string | null;
  body: string;
}

export interface RequestChangesResult {
  ticket_id: string;
  next_run_context: string | null;
}

// --- Projects ------------------------------------------------------------------

export interface ProjectOut {
  id: string;
  name: string;
  slug: string;
  source_path: string;
  default_workspace_mode: string | null;
  default_worktree_name_template: string | null;
  default_base_ref: string | null;
  default_linked_workspaces: Array<Record<string, unknown>> | null;
  default_toolchains: string[];
  default_tool_paths: string[];
  color: string | null;
  position: number;
  archived_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProjectCreate {
  name: string;
  slug?: string | null;
  source_path: string;
  default_workspace_mode?: WorkspaceKind | null;
  default_worktree_name_template?: string | null;
  default_base_ref?: string | null;
  default_linked_workspaces?: TicketWorkspaceIn[] | null;
  default_toolchains?: string[];
  default_tool_paths?: string[];
  color?: string | null;
  position?: number;
}

export type ProjectUpdate = Partial<ProjectCreate>;

// --- Profiles ------------------------------------------------------------------

export interface ClaudeCredentialsOut {
  source: "inherit" | "api_key" | "auth_token";
  value_set: boolean;
  base_url?: string | null;
}

export interface ClaudeCredentialsIn {
  source: "inherit" | "api_key" | "auth_token";
  value?: string | null;
  base_url?: string | null;
}

export type PermissionMode = "default" | "acceptEdits" | "bypassPermissions";

/** One entry of a multi_endpoint backend's ``backend_config.agents[]`` (opencode). */
export interface BackendConfigAgent {
  name: string;
  model?: string | null;
  endpoint_id?: string | null;
  tools?: string[];
  prompt?: string | null;
  [key: string]: unknown;
}

export interface ProfileOut {
  id: string;
  name: string;
  description: string;
  fs_read: string[];
  fs_write: string[];
  allowed_tools: string[];
  denied_tools: string[];
  network_mode: string;
  network_allowlist: string[];
  secret_keys: string[];
  default_model: string | null;
  backend: string;
  execution_target: string;
  endpoint_id: string | null;
  backend_config: Record<string, unknown>;
  claude_credentials: ClaudeCredentialsOut | null;
  claude_binary_path: string | null;
  env_keys: string[];
  system_prompt: string | null;
  permission_mode: string | null;
  cc_settings_passthrough: Record<string, unknown>;
  run_token_scopes: string[];
  /** Non-blocking save-time notices (e.g. an off-menu model assignment). */
  warnings: string[];
  created_at: string;
  updated_at: string;
}

export interface ProfileCreate {
  name: string;
  description?: string;
  fs_read?: string[];
  fs_write?: string[];
  allowed_tools?: string[];
  denied_tools?: string[];
  network_mode?: string;
  network_allowlist?: string[];
  secret_keys?: string[];
  default_model?: string | null;
  backend?: string;
  execution_target?: "local" | "k8s";
  endpoint_id?: string | null;
  backend_config?: Record<string, unknown>;
  claude_credentials?: ClaudeCredentialsIn | null;
  claude_binary_path?: string | null;
  env?: Record<string, string> | null;
  system_prompt?: string | null;
  permission_mode?: PermissionMode | null;
  cc_settings_passthrough?: Record<string, unknown>;
  run_token_scopes?: string[];
}

export type ProfileUpdate = Partial<ProfileCreate>;

// --- Config / scheduling -------------------------------------------------------

export interface ScheduleWindowOut {
  id: number;
  label: string;
  day_mask: number;
  start: string;
  end: string;
  max_parallel: number;
  position: number;
}

export interface ScheduleWindowCreate {
  label?: string;
  day_mask?: number;
  start?: string;
  end?: string;
  max_parallel?: number;
  position?: number;
}

export type ScheduleWindowUpdate = Partial<ScheduleWindowCreate>;

export interface ScheduleWindowsReplace {
  timezone?: string;
  windows: ScheduleWindowCreate[];
}

export interface ConfigOut {
  window_start: string;
  window_end: string;
  max_parallel: number;
  worktree_root: string;
  transcript_root: string;
  worktree_base_ref: string | null;
  notify_webhook_url: string | null;
  schedule_timezone: string;
  windows: ScheduleWindowOut[];
  toolchain_presets: Record<string, string[]>;
  // Resident interactive agents (global, live-evaluated).
  session_idle_timeout_s: number;
  max_live_sessions: number;
  max_queued_turns: number;
  max_turn_seconds: number;
  claude_binary_path: string | null;
  opencode_binary_path: string | null;
  k8s_kubeconfig_path: string | null;
  k8s_in_cluster: boolean;
  k8s_namespace: string;
  k8s_runner_image: string | null;
  k8s_cpu_request: string | null;
  k8s_cpu_limit: string | null;
  k8s_mem_request: string | null;
  k8s_mem_limit: string | null;
  k8s_node_selector: Record<string, string>;
  k8s_runtime_class: string | null;
  k8s_git_credentials_secret: string | null;
}

export interface ConfigUpdate {
  window_start?: string | null;
  window_end?: string | null;
  max_parallel?: number | null;
  worktree_base_ref?: string | null;
  notify_webhook_url?: string | null;
  schedule_timezone?: string | null;
  toolchain_presets?: Record<string, string[]> | null;
  // Resident interactive agents (global, live-evaluated). Applied to every
  // inheriting agent on the next reaper pass; no restart.
  session_idle_timeout_s?: number | null;
  max_live_sessions?: number | null;
  max_queued_turns?: number | null;
  max_turn_seconds?: number | null;
  claude_binary_path?: string | null;
  opencode_binary_path?: string | null;
  k8s_kubeconfig_path?: string | null;
  k8s_in_cluster?: boolean | null;
  k8s_namespace?: string | null;
  k8s_runner_image?: string | null;
  k8s_cpu_request?: string | null;
  k8s_cpu_limit?: string | null;
  k8s_mem_request?: string | null;
  k8s_mem_limit?: string | null;
  k8s_node_selector?: Record<string, string> | null;
  k8s_runtime_class?: string | null;
  k8s_git_credentials_secret?: string | null;
}

// --- Providers & endpoints -------------------------------------------------
// See docs/design/providers-and-endpoints.md ("Layer 1"). Credentials are
// write-only: responses never carry plaintext, only *_set booleans.

export type ProtocolKind =
  | "anthropic"
  | "anthropic_compat"
  | "openai"
  | "openai_compat"
  | "openai_codex"
  | "openrouter"
  | "ollama";

export type CredentialSource = "api_key" | "oauth_file" | "subscription_file" | "env_var" | "none";

export interface EndpointOut {
  id: string;
  provider_id: string;
  label: string;
  protocol_kind: ProtocolKind;
  base_url: string | null;
  credential_source: CredentialSource;
  credential_set: boolean;
  harness_lock: string | null;
  default_model: string | null;
  models: string[];
  models_pulled_at: string | null;
  extra_set: boolean;
  created_at: string;
  updated_at: string;
}

export interface EndpointCreate {
  label?: string;
  protocol_kind: ProtocolKind;
  base_url?: string | null;
  credential_source?: CredentialSource;
  credential_value?: string | null;
  harness_lock?: string | null;
  default_model?: string | null;
  models?: string[];
  extra?: Record<string, unknown> | null;
}

export type EndpointUpdate = Partial<EndpointCreate>;

export interface ProviderOut {
  id: string;
  name: string;
  vendor: string;
  endpoints: EndpointOut[];
  created_at: string;
  updated_at: string;
}

export interface ProviderCreate {
  name: string;
  vendor: string;
  endpoints?: EndpointCreate[];
  /** Seeded into every nested api_key endpoint above that sets no credential_value of its own. */
  credential_value?: string | null;
}

export interface ProviderUpdate {
  name?: string;
  vendor?: string;
}

export interface ProviderRotateCredential {
  credential_value: string;
}

export interface ProviderRotateResult {
  rotated: number;
}

export interface CatalogEndpointOut {
  label: string;
  protocol_kind: ProtocolKind;
  base_url: string | null;
  harness_lock: string | null;
  default_model: string | null;
}

/**
 * A single vendor *offering* — one fixed credential mode plus the
 * endpoint(s) it seeds. Vendors with more than one way to authenticate
 * (OpenAI API vs Codex OAuth, Anthropic API vs a Claude subscription, ZAI
 * vs its Coding Plan) show up as separate offerings so a user is never
 * asked to choose between a pasted secret and a credential file path for
 * the same registration — the offering already picked one.
 */
export interface CatalogOfferingOut {
  key: string;
  label: string;
  vendor: string;
  credential_source: CredentialSource;
  credential_hint: string | null;
  description: string;
  suggested_name: string;
  endpoints: CatalogEndpointOut[];
}

// --- Backends (Layer 2: harness capability catalog) ------------------------
// See docs/design/providers-and-endpoints.md ("Layer 2: Harnesses"). Mirrors
// nightdesk.domain.backend_capabilities.BackendCapability.

export interface ModelSlotOut {
  name: string;
  label: string;
  required: boolean;
}

export interface BackendRuntimeOut {
  binary_path_override: string | null;
  resolved_path: string | null;
  source: "override" | "path" | "default" | null;
  found: boolean;
  version: string | null;
}

export interface BackendOut {
  code: string;
  label: string;
  summary: string;
  protocol_kinds: ProtocolKind[];
  multi_endpoint: boolean;
  requires_provider: boolean;
  enabled: boolean;
  executable: boolean;
  group_keys: string[];
  model_slots: ModelSlotOut[];
  capabilities: string[];
  /** Hard yes/no on whether the harness binary is present. Null for backends
   *  without a runtime binary to probe. */
  runtime: BackendRuntimeOut | null;
}

export interface WorkerStatusOut {
  host: string | null;
  pid: number | null;
  last_seen_at: string | null;
  stale: boolean;
  in_window: boolean;
  window_start: string | null;
  window_end: string | null;
  max_parallel: number;
  active_window: string | null;
  schedule_timezone: string;
  normal_running: number;
  run_now_running: number;
  total_running: number;
  running_count: number;
  day_spend_usd: number;
  month_spend_usd: number;
}

// --- Search --------------------------------------------------------------------

export interface SearchHit {
  id: string;
  title: string;
  snippet: string;
  status: string;
  project_id: string | null;
  project_name: string | null;
  project_color: string | null;
}

// --- Inbox ---------------------------------------------------------------------

export interface InboxItemOut {
  ticket: TicketOut;
  blockers: string[];
}
export interface InboxCountOut {
  count: number;
}
export interface TicketPromote {
  target: "draft" | "queued";
}

// --- Saved views ---------------------------------------------------------------

export interface SavedViewCreate {
  name: string;
  surface: string;
  params?: Record<string, string>;
}
export interface SavedViewUpdate {
  name: string;
}

// --- Analytics -----------------------------------------------------------------
// JSON shapes are best-effort; the endpoints are part of the API gap-fill and
// may not exist yet, so consumers degrade gracefully on 404.

export interface AnalyticsSummary {
  day_spend_usd: number;
  month_spend_usd: number;
  total_runs: number;
  success_rate: number | null;
  [key: string]: unknown;
}
export interface SpendPoint {
  date: string;
  cost_usd: number;
}

// --- Cron ----------------------------------------------------------------------

export type CronWorkspaceMode = "directory" | "in_place";

export interface CronJobOut {
  id: string;
  title: string;
  prompt: string;
  profile_id: string;
  source_path: string;
  schedule: string;
  timezone: string;
  priority: number;
  workspace_mode: string;
  additional_dirs: AdditionalDir[];
  permission_overrides: Record<string, unknown> | null;
  enabled: boolean;
  force_run: boolean;
  misfire_policy: string;
  overlap_policy: string;
  next_fire_at: string | null;
  last_fire_at: string | null;
  last_ticket_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface CronJobCreate {
  title: string;
  prompt?: string;
  profile_id: string;
  source_path: string;
  schedule: string;
  timezone?: string;
  priority?: number;
  workspace_mode?: CronWorkspaceMode;
  additional_dirs?: AdditionalDir[];
  permission_overrides?: Record<string, unknown> | null;
  enabled?: boolean;
  force_run?: boolean;
  misfire_policy?: "coalesce";
  overlap_policy?: "skip_if_active" | "always";
}

export type CronJobUpdate = Partial<CronJobCreate>;

// --- Labels (HTMX-JSON hybrid; shape from routes/labels.py) --------------------

export interface LabelCreate {
  name: string;
  color?: string;
}
export type LabelUpdate = Partial<LabelCreate>;

// --- Resident interactive agents (UI label: "Agents") --------------------------
// The backend entity is still `Session` (tables sessions / session_turns /
// pending_inputs); the API and UI speak "agent". See
// docs/design/session-suite/resident-agents-v3.md.

/** Derived five-state model (domain: describe_liveness). `crashed` is a cold
 *  variant carrying a crash breadcrumb. */
export type AgentLiveness = "alive" | "needs-input" | "warm" | "cold" | "ended" | "crashed";

/** Kind of an open human-input request the agent is blocked on. */
export type PendingKind = "permission" | "ask_question" | "plan_approval";

export interface AgentOut {
  id: string;
  title: string;
  profile_id: string | null;
  project_id: string | null;
  backend: string;
  model: string | null;
  /** Coarse persisted status: active | idle | ended | crashed. */
  status: string;
  liveness: AgentLiveness;
  has_pending: boolean;
  source_path: string;
  idle_timeout_s: number | null;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  created_at: string;
  updated_at: string;
  ended_at: string | null;
}

/** One inbox item AND turn record. `kind`: user | interrupt | answer. */
export interface AgentTurnOut {
  id: string;
  position: number;
  kind: string;
  body: string;
  ref_request_id: string | null;
  /** queued | delivering | streaming | done | cancelled | interrupted | failed */
  status: string;
  model_used: string | null;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost_usd: number;
  includes_resume: boolean;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface AgentPendingOut {
  id: string;
  session_id: string;
  request_id: string;
  kind: PendingKind;
  tool: string | null;
  payload: Record<string, unknown>;
  status: string;
  created_at: string;
}

/** A pending row plus its agent title, for the cross-agent badge / Desk band. */
export interface AgentPendingItem extends AgentPendingOut {
  session_title: string;
}

/** API-safe env view: a secret value is never returned (only `set`). */
export interface AgentEnvEntryOut {
  key: string;
  secret: boolean;
  set: boolean;
  value: string | null;
}

export interface AgentDetailOut extends AgentOut {
  turns: AgentTurnOut[];
  pending_input: AgentPendingOut | null;
  env: AgentEnvEntryOut[];
  /** Local claude session id (from resume_handle) for the `claude --resume <id>`
   *  terminal handoff. Null until the agent has run. Not a secret. */
  claude_session_id: string | null;
}

export interface AgentTurnEdit {
  body: string;
}

export interface AgentTurnReorder {
  ordered_ids: string[];
}

/** One env entry on the wire for PUT: a secret with `value: null` keeps the
 *  stored cipher (write-only secret contract). */
export interface AgentEnvEntryIn {
  value: string | null;
  secret: boolean;
}

export interface AgentCreate {
  title?: string | null;
  profile_id: string;
  project_id?: string | null;
  source_path?: string | null;
  model?: string | null;
  idle_timeout_s?: number | null;
  env?: Record<string, AgentEnvEntryIn>;
}

export interface AgentMessage {
  message: string;
}

/** Answer to an open PendingInput. `decision` is allow/deny (permission,
 *  ask_question) or approve/deny (plan_approval). */
export interface AgentAnswer {
  decision: "allow" | "deny" | "approve";
  answer?: string | null;
  updated_input?: Record<string, unknown> | null;
}

export interface AgentEnvPut {
  env: Record<string, AgentEnvEntryIn>;
}

export interface AgentRestart {
  force?: boolean;
}
