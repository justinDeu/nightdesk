import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { CircleDot, ExternalLink as ExternalLinkIcon, GitMerge, Link2, Import, Plug, X } from "lucide-react";
import { qk } from "@/api";
import { integrationsApi } from "@/api/integrations";
import type { GitLabItem, ProjectOut, RepoLinkOut, TicketOut } from "@/api/types";
import { ApiError } from "@/api/client";
import { Badge } from "@/ui/Badge";
import { Button } from "@/ui/Button";
import { EmptyState } from "@/ui/EmptyState";
import { Input } from "@/ui/Input";
import { Select } from "@/ui/Select";
import { Spinner } from "@/ui/Spinner";
import { Tooltip } from "@/ui/Tooltip";
import { toast } from "@/ui/Toast";
import { relativeTime } from "@/lib/time";
import { cn } from "@/lib/cn";
import { externalStateTone } from "@/components/ExternalItemChips";
import { peekRailClasses } from "./peekLayout";

export type Lens = "issues" | "mrs";

/** How the lens panel sources its repo links.
 *  - `project`: the filter resolves one project that has repo links → its repos only.
 *  - `global`:  no single linked project → browse every repo link in the install.
 *  - `unlinked`: one project resolved but it has no repo links → action empty state. */
export type LensScope =
  | { kind: "project"; projectId: string }
  | { kind: "global" }
  | { kind: "unlinked"; projectId: string };

const LENS_REPO_KEY = "nightdesk:lens-repo";

/** Human label for a repo in the global switcher — prefer the owning project's
 *  name, fall back to the repo display name, and disambiguate with the path. */
function repoLabel(repo: RepoLinkOut, projectMap: Map<string, ProjectOut>): string {
  const projName = repo.project_ids.length === 1 ? projectMap.get(repo.project_ids[0])?.name : undefined;
  const primary = projName ?? (repo.display_name || repo.external_path);
  const secondary = repo.external_path;
  if (!secondary || primary.toLowerCase() === secondary.toLowerCase()) return primary;
  if (secondary.toLowerCase().endsWith("/" + primary.toLowerCase())) return secondary;
  return `${primary} · ${secondary}`;
}

/** The lens panel that replaces the board area when Issues/MRs is toggled (§7).
 *  Rows open a side-peek; they never navigate. Scope decides which repo links are
 *  browsable: a single linked project's repos, every repo link, or an empty state
 *  directing the user to link a repo. */
export function IntegrationLensPanel({
  scope,
  lens,
  repos,
  projects,
  loading,
  onPeekActiveChange,
}: {
  scope: LensScope;
  lens: Lens;
  repos: RepoLinkOut[];
  projects: ProjectOut[];
  loading: boolean;
  /** Reports whether this panel's issue/MR peek is open so the parent page can
   *  reserve the rail width alongside the ticket peek. Fired on open, close, and
   *  initial mount (with `false`). */
  onPeekActiveChange?: (active: boolean) => void;
}) {
  const projectMap = useMemo(() => new Map(projects.map((p) => [p.id, p])), [projects]);
  const persistSelection = scope.kind === "global";
  const [repoId, setRepoId] = useState<string>(() =>
    persistSelection ? sessionStorage.getItem(LENS_REPO_KEY) ?? "" : "",
  );
  const [state, setState] = useState<string>("opened");
  const [search, setSearch] = useState("");
  const [peek, setPeek] = useState<{ repo: RepoLinkOut; iid: string } | null>(null);
  useEffect(() => {
    onPeekActiveChange?.(peek !== null);
  }, [peek, onPeekActiveChange]);

  const activeRepo = repos.find((r) => r.id === repoId) ?? repos[0];
  const selectRepo = (id: string) => {
    setRepoId(id);
    if (persistSelection) sessionStorage.setItem(LENS_REPO_KEY, id);
  };

  const items = useQuery({
    queryKey:
      lens === "issues"
        ? qk.integrations.issues(activeRepo?.id ?? "", { state, search })
        : qk.integrations.mrs(activeRepo?.id ?? "", { state, search }),
    queryFn: () =>
      lens === "issues"
        ? integrationsApi.listIssues(activeRepo!.id, { state, search: search || undefined })
        : integrationsApi.listMrs(activeRepo!.id, { state, search: search || undefined }),
    enabled: !!activeRepo,
  });

  if (loading) {
    return <div className="grid h-full place-items-center text-sm text-moon-600"><Spinner size={14} /></div>;
  }
  if (scope.kind === "unlinked") {
    const project = projectMap.get(scope.projectId);
    return (
      <div className="h-full px-4 pb-3 pt-3">
        <div className="grid h-full place-items-center">
          <EmptyState
            icon={<Plug size={18} />}
            title="No linked repositories"
            description={
              <>
                {project ? (
                  <>{project.name} isn't linked to any repository yet. </>
                ) : null}
                Attach a repository to browse its issues and merge requests here.
              </>
            }
            action={
              <Button variant="primary" size="sm" asChild>
                <Link to="/settings/$section" params={{ section: "projects" }}>
                  <Link2 size={14} /> Link a repository
                </Link>
              </Button>
            }
          />
        </div>
      </div>
    );
  }
  if (repos.length === 0) {
    return (
      <div className="grid h-full place-items-center px-4 text-center text-sm text-moon-600">
        No repositories linked. Attach one under Settings → Connections.
      </div>
    );
  }

  const rows = items.data?.items ?? [];
  const showSwitcher = repos.length > 1;

  return (
    <div className="flex h-full min-h-0 flex-col px-4 pb-3 pt-3">
      <div className="mb-2.5 flex flex-wrap items-center gap-2">
        {showSwitcher && (
          <Select
            value={activeRepo?.id}
            onChange={(e) => selectRepo(e.target.value)}
            className="w-64"
            aria-label="Repository"
          >
            {repos.map((r) => (
              <option key={r.id} value={r.id}>{repoLabel(r, projectMap)}</option>
            ))}
          </Select>
        )}
        <Select value={state} onChange={(e) => setState(e.target.value)} className="w-32">
          <option value="opened">Open</option>
          <option value={lens === "mrs" ? "merged" : "closed"}>{lens === "mrs" ? "Merged" : "Closed"}</option>
          {lens === "mrs" && <option value="closed">Closed</option>}
          <option value="all">All</option>
        </Select>
        <Input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search…"
          className="w-48"
        />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {items.isLoading ? (
          <div className="grid h-32 place-items-center text-sm text-moon-600"><Spinner size={14} /></div>
        ) : items.isError ? (
          <p className="rounded-card border border-failed/30 bg-failed/10 px-3 py-2 text-sm text-failed">
            {items.error instanceof ApiError ? items.error.message : "Could not load"}
          </p>
        ) : rows.length === 0 ? (
          <p className="px-1 py-6 text-center text-sm text-moon-600">Nothing here.</p>
        ) : (
          <ul className="space-y-1">
            {rows.map((item) => (
              <ItemRow
                key={item.iid}
                item={item}
                kind={lens === "mrs" ? "merge_request" : "issue"}
                onOpen={() => activeRepo && setPeek({ repo: activeRepo, iid: String(item.iid) })}
              />
            ))}
          </ul>
        )}
      </div>

      {peek && (
        <ExternalItemPeek
          repo={peek.repo}
          kind={lens === "mrs" ? "merge_request" : "issue"}
          iid={peek.iid}
          projectId={resolveImportProjectId(scope, peek.repo)}
          onClose={() => setPeek(null)}
        />
      )}
    </div>
  );
}

/** The project an imported issue is filed under. In project scope this is the
 *  filtered project; in global scope it's inferred from the repo's single
 *  attached project, or undefined when ambiguous (import is then disabled). */
function resolveImportProjectId(scope: LensScope, repo: RepoLinkOut): string | undefined {
  if (scope.kind === "project") return scope.projectId;
  if (scope.kind === "unlinked") return undefined;
  return repo.project_ids.length === 1 ? repo.project_ids[0] : undefined;
}

function ItemRow({
  item,
  kind,
  onOpen,
}: {
  item: GitLabItem;
  kind: "issue" | "merge_request";
  onOpen: () => void;
}) {
  return (
    <li>
      <button
        type="button"
        onClick={onOpen}
        className="flex w-full items-center gap-2.5 rounded-card border border-ink-700 bg-ink-900 px-3 py-2 text-left hover:border-lamp/50 hover:bg-ink-800"
      >
        <span className="text-moon-600">{kind === "merge_request" ? <GitMerge size={13} /> : <CircleDot size={13} />}</span>
        <span className="font-mono text-[11px] text-moon-600">
          {kind === "merge_request" ? "!" : "#"}{item.iid}
        </span>
        <span className="min-w-0 flex-1 truncate text-sm text-moon-100">{item.title}</span>
        <Badge tone={externalStateTone(kind, item.state)} dot>{item.state}</Badge>
        {item.updated_at && <span className="shrink-0 text-[11px] text-moon-600">{relativeTime(item.updated_at)}</span>}
      </button>
    </li>
  );
}

// ---------------------------------------------------------------------------
// Issue / MR side-peek (never navigates)
// ---------------------------------------------------------------------------

function ExternalItemPeek({
  repo,
  kind,
  iid,
  projectId,
  onClose,
}: {
  repo: RepoLinkOut;
  kind: "issue" | "merge_request";
  iid: string;
  /** Project to file an imported issue under. Undefined in global scope when the
   *  repo isn't attached to exactly one project — import is then disabled. */
  projectId?: string;
  onClose: () => void;
}) {
  const item = useQuery({
    queryKey: qk.integrations.item(repo.id, kind, iid),
    queryFn: () =>
      kind === "issue" ? integrationsApi.getIssue(repo.id, iid) : integrationsApi.getMr(repo.id, iid),
  });

  const workedBy = useQuery({
    queryKey: ["integrations", "worked-by", repo.id, kind, iid],
    queryFn: async () => {
      // No cross-ticket external-link index in v1; derive by scanning the
      // project's tickets for a matching link is out of scope. Placeholder for
      // v2 back-links; kept as an empty list so the section renders cleanly.
      return [] as { ticket_id: string; title: string }[];
    },
    enabled: false,
  });

  const data = item.data;
  const [importing, setImporting] = useState(false);

  return (
    <aside className={cn(peekRailClasses)} aria-label="Issue preview">
      <div className="flex items-start justify-between gap-2 border-b border-ink-700 px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="text-moon-600">{kind === "merge_request" ? <GitMerge size={14} /> : <CircleDot size={14} />}</span>
            <span className="font-mono text-[12px] text-moon-600">
              {kind === "merge_request" ? "!" : "#"}{iid}
            </span>
          </div>
          <h2 className="mt-0.5 text-sm font-semibold leading-snug text-moon-100">
            {data?.title ?? (item.isLoading ? "Loading…" : "(unavailable)")}
          </h2>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {data?.web_url && (
            <Tooltip content="Open in GitLab">
              <a href={data.web_url} target="_blank" rel="noreferrer" className="grid h-7 w-7 place-items-center rounded-control text-moon-400 hover:bg-ink-800 hover:text-moon-100" aria-label="Open in GitLab">
                <ExternalLinkIcon size={14} />
              </a>
            </Tooltip>
          )}
          <button onClick={onClose} className="grid h-7 w-7 place-items-center rounded-control text-moon-400 hover:bg-ink-800 hover:text-moon-100" aria-label="Close">
            <X size={15} />
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        {item.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-moon-600"><Spinner size={13} /> Loading…</div>
        ) : item.isError ? (
          <p className="text-sm text-failed">{item.error instanceof ApiError ? item.error.message : "Could not load"}</p>
        ) : data ? (
          <>
            <div className="mb-3 flex flex-wrap items-center gap-1.5">
              <Badge tone={externalStateTone(kind, data.state)} dot>{data.state}</Badge>
              {data.author?.username && <span className="text-[11px] text-moon-600">@{data.author.username}</span>}
              {data.updated_at && <span className="text-[11px] text-moon-600">updated {relativeTime(data.updated_at)}</span>}
              {kind === "merge_request" && data.source_branch && (
                <span className="font-mono text-[11px] text-moon-600">{data.source_branch} → {data.target_branch}</span>
              )}
            </div>
            {(data.labels ?? []).length > 0 && (
              <div className="mb-3 flex flex-wrap gap-1">
                {(data.labels ?? []).map((l) => (
                  <Badge key={l} tone="neutral">{l}</Badge>
                ))}
              </div>
            )}
            <div className="whitespace-pre-wrap break-words border-t border-ink-700/60 pt-3 text-sm leading-relaxed text-moon-300">
              {data.description?.trim() || <span className="text-moon-600">No description.</span>}
            </div>
            {(workedBy.data ?? []).length > 0 && (
              <div className="mt-3 border-t border-ink-700/60 pt-3">
                <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-moon-600">Worked by</div>
                {(workedBy.data ?? []).map((w) => (
                  <div key={w.ticket_id} className="text-xs text-moon-300">{w.title}</div>
                ))}
              </div>
            )}
          </>
        ) : null}
      </div>

      <div className="flex items-center gap-2 border-t border-ink-700 px-4 py-3">
        {kind === "issue" && projectId && (
          <Button
            size="sm"
            variant="primary"
            leadingIcon={<Import size={13} />}
            loading={importing}
            onClick={async () => {
              setImporting(true);
              try {
                const ticket = await integrationsApi.importTicket(repo.id, {
                  kind: "issue",
                  external_iid: iid,
                  project_id: projectId,
                });
                toast.success("Imported as draft", { description: (ticket as TicketOut).title });
                onClose();
              } catch (err) {
                toast.error(err instanceof ApiError ? err.message : "Could not import");
              } finally {
                setImporting(false);
              }
            }}
          >
            Import as ticket
          </Button>
        )}
        {kind === "issue" && !projectId && (
          <Tooltip content="Attach this repository to a single project to import it as a ticket.">
            <span>
              <Button size="sm" variant="primary" leadingIcon={<Import size={13} />} disabled>
                Import as ticket
              </Button>
            </span>
          </Tooltip>
        )}
        <LinkToTicketButton repo={repo} kind={kind} iid={iid} />
      </div>
    </aside>
  );
}

function LinkToTicketButton({
  repo,
  kind,
  iid,
}: {
  repo: RepoLinkOut;
  kind: "issue" | "merge_request";
  iid: string;
}) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [ticketId, setTicketId] = useState("");
  const tickets = useQuery({
    queryKey: ["tickets", "link-picker"],
    queryFn: () => import("@/api/tickets").then((m) => m.ticketsApi.list({ limit: 100 })),
    enabled: open,
  });
  const options = useMemo(
    () => (tickets.data ?? []).filter((t) => t.status !== "archived"),
    [tickets.data],
  );

  async function link() {
    const tid = ticketId || options[0]?.id;
    if (!tid) return;
    try {
      await integrationsApi.createTicketLink(tid, {
        repo_link_id: repo.id,
        kind,
        external_iid: iid,
        role: kind === "merge_request" ? "produced_mr" : "references",
      });
      qc.invalidateQueries({ queryKey: qk.integrations.ticketLinks(tid) });
      toast.success("Linked to ticket");
      setOpen(false);
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not link");
    }
  }

  if (!open) {
    return (
      <Button size="sm" variant="ghost" leadingIcon={<Link2 size={13} />} onClick={() => setOpen(true)}>
        Link to ticket…
      </Button>
    );
  }

  return (
    <div className="flex flex-1 items-center gap-1.5">
      <Select value={ticketId || options[0]?.id || ""} onChange={(e) => setTicketId(e.target.value)} className="min-w-0 flex-1">
        {options.length === 0 ? (
          <option value="">No tickets</option>
        ) : (
          options.map((t) => (
            <option key={t.id} value={t.id}>{t.title}</option>
          ))
        )}
      </Select>
      <Button size="sm" variant="primary" disabled={options.length === 0} onClick={link}>Link</Button>
      <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
    </div>
  );
}
