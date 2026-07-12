import { useMemo, useState } from "react";
import { Link } from "@tanstack/react-router";
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { AlertTriangle, Check, CircleDot, ExternalLink, Inbox as InboxIcon, Workflow } from "lucide-react";
import type { GitLabItemPage, InboxItemOut, RepoLinkOut } from "@/api/types";
import type { ProjectActivityRow } from "@/api/projectActivity";
import type { SpendModelRow, SpendProfileRow } from "@/api/analytics";
import { qk } from "@/api";
import { integrationsApi, useConnections } from "@/api/integrations";
import { Button } from "@/ui/Button";
import { Spinner } from "@/ui/Spinner";
import { StatusPill, type StatusKind } from "@/ui/StatusPill";
import { Tooltip } from "@/ui/Tooltip";
import { useTicketActions } from "@/lib/ticketActions";
import { formatTokens } from "@/lib/status";
import { formatDuration, relativeTime } from "@/lib/time";
import { cn } from "@/lib/cn";
import { ExternalItemPeek } from "@/components/ExternalItemPeek";
import { dayKey, dayLabel } from "./shared";

// ---------------------------------------------------------------------------
// Panel chrome — a titled card used by every right-column panel for consistency.
// ---------------------------------------------------------------------------

export function Panel({
  title,
  count,
  action,
  children,
  className,
}: {
  title: string;
  count?: number;
  action?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={cn("overflow-hidden rounded-card border border-ink-700 bg-ink-900 shadow-[var(--shadow-raised)]", className)}>
      <div className="flex shrink-0 items-center justify-between border-b border-ink-700 px-3.5 py-2">
        <div className="flex items-center gap-2">
          <h2 className="font-display text-xs font-semibold uppercase tracking-wide text-moon-400">
            {title}
          </h2>
          {count != null && count > 0 && (
            <span className="font-mono text-[10px] text-moon-600">{count}</span>
          )}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}

function SlimEmpty({ icon, text }: { icon?: React.ReactNode; text: string }) {
  return (
    <div className="flex items-center gap-2.5 px-3.5 py-3 text-sm text-moon-600">
      {icon && <span className="shrink-0 text-moon-600">{icon}</span>}
      <span className="min-w-0 flex-1 truncate">{text}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Inbox band — the project's inbox items with inline promote / decline.
// ---------------------------------------------------------------------------

export function ProjectInbox({ items }: { items: InboxItemOut[] }) {
  const actions = useTicketActions();

  return (
    <Panel
      title="Inbox"
      count={items.length}
      action={
        <Link to="/inbox" className="text-[11px] text-moon-400 hover:text-lamp">
          Open inbox →
        </Link>
      }
    >
      {items.length === 0 ? (
        <SlimEmpty icon={<InboxIcon size={14} />} text="Nothing captured for this project." />
      ) : (
        <div>
          {items.map(({ ticket, blockers }) => {
            const ready = blockers.length === 0;
            return (
              <div
                key={ticket.id}
                className="group flex items-start gap-2.5 border-b border-ink-700/40 px-3.5 py-2.5 last:border-b-0"
              >
                <span className="mt-0.5 shrink-0">
                  {ready ? (
                    <Check size={14} className="text-success" />
                  ) : (
                    <Tooltip content={blockers.join(" · ")}>
                      <span className="inline-flex items-center gap-1 text-warn">
                        <AlertTriangle size={12} />
                        <span className="text-[10px] font-semibold">{blockers.length}</span>
                      </span>
                    </Tooltip>
                  )}
                </span>
                <Link
                  to="/tickets/$id"
                  params={{ id: ticket.id }}
                  className="min-w-0 flex-1 rounded-[4px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lamp"
                >
                  <span className="block truncate text-sm text-moon-100 group-hover:text-lamp">
                    {ticket.title || "Untitled capture"}
                  </span>
                  <span className="text-[11px] text-moon-600">{relativeTime(ticket.created_at)}</span>
                </Link>
                <div className="flex shrink-0 items-center gap-1">
                  <Button
                    size="sm"
                    variant="subtle"
                    disabled={!ready}
                    onClick={() => actions.promote(ticket, "draft")}
                    aria-label="Promote to draft"
                  >
                    Draft
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={!ready}
                    onClick={() => actions.promote(ticket, "queued")}
                    aria-label="Promote and queue"
                  >
                    Queue
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => actions.decline(ticket)}
                    aria-label="Decline"
                  >
                    Decline
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Recent activity ledger — the project's recent runs, dense + grouped by day.
// ---------------------------------------------------------------------------

function activityPill(outcome: string): { status: StatusKind; label?: string } {
  switch (outcome) {
    case "success":
      return { status: "success" };
    case "failed":
      return { status: "failed" };
    case "canceled":
      return { status: "failed", label: "Canceled" };
    case "running":
      return { status: "running" };
    default:
      return { status: "draft", label: "Unknown" };
  }
}

export function ActivityLedger({ rows }: { rows: ProjectActivityRow[] }) {
  const [failuresOnly, setFailuresOnly] = useState(false);

  const filtered = useMemo(
    () => (failuresOnly ? rows.filter((r) => r.outcome !== "success") : rows),
    [rows, failuresOnly],
  );

  const groups = useMemo(() => {
    const m = new Map<string, ProjectActivityRow[]>();
    for (const r of filtered) {
      const k = dayKey(r.started_at) ?? "—";
      if (!m.has(k)) m.set(k, []);
      m.get(k)!.push(r);
    }
    return [...m.entries()].sort(([a], [b]) => (a < b ? 1 : a > b ? -1 : 0));
  }, [filtered]);

  return (
    <Panel
      title="Recent activity"
      count={rows.length}
      action={
        rows.some((r) => r.outcome !== "success") ? (
          <button
            onClick={() => setFailuresOnly((v) => !v)}
            className={cn(
              "rounded-full border px-2 py-0.5 text-[10px] font-medium transition-colors",
              failuresOnly
                ? "border-failed/40 bg-failed/10 text-failed"
                : "border-ink-700 text-moon-600 hover:text-moon-400",
            )}
          >
            Failures
          </button>
        ) : undefined
      }
    >
      {rows.length === 0 ? (
        <SlimEmpty icon={<Workflow size={14} />} text="No runs against this project yet." />
      ) : groups.length === 0 ? (
        <SlimEmpty text="No failed runs in this window." />
      ) : (
        <div>
          {groups.map(([key, items]) => (
            <div key={key}>
              <div className="bg-ink-950/50 px-3.5 py-1 text-[10px] font-semibold uppercase tracking-wide text-moon-600">
                {dayLabel(key)}
              </div>
              {items.map((r) => {
                const pill = activityPill(r.outcome);
                return (
                  <Link
                    key={r.run_id}
                    to="/tickets/$id/runs/$rid"
                    params={{ id: r.ticket_id, rid: r.run_id }}
                    className="group flex items-center gap-2.5 border-b border-ink-700/40 px-3.5 py-2 text-sm transition-colors last:border-b-0 hover:bg-ink-800/60"
                  >
                    <StatusPill status={pill.status} label={pill.label} />
                    <span className="min-w-0 flex-1 truncate text-moon-100 group-hover:text-lamp">
                      {r.ticket_title}
                    </span>
                    <span className="hidden shrink-0 font-mono text-[11px] text-moon-600 sm:block">
                      {r.duration_seconds != null ? formatDuration(r.duration_seconds * 1000) : ""}
                      {r.tokens != null ? ` · ${formatTokens(r.tokens)}` : ""}
                    </span>
                    <span className="shrink-0 text-[11px] text-moon-600">
                      {relativeTime(r.started_at)}
                    </span>
                  </Link>
                );
              })}
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Models & profiles — where the project's 30d spend went.
// ---------------------------------------------------------------------------

export function ModelsProfiles({
  byModel,
  byProfile,
  totalRuns,
  totalCost,
}: {
  byModel: SpendModelRow[];
  byProfile: SpendProfileRow[];
  totalRuns: number;
  totalCost: number;
}) {
  if (totalRuns === 0) {
    return (
      <Panel title="Models & profiles">
        <SlimEmpty text="No priced usage in the last 30 days." />
      </Panel>
    );
  }
  return (
    <Panel title="Models & profiles" count={totalRuns}>
      <div className="grid grid-cols-1 divide-y divide-ink-700/40 md:grid-cols-2 md:divide-x md:divide-y-0">
        <UsageList
          title="By model"
          rows={byModel.slice(0, 6).map((m) => ({
            name: m.model,
            runs: m.run_count,
            cost: m.cost,
            share: totalRuns > 0 ? m.run_count / totalRuns : 0,
          }))}
          totalCost={totalCost}
        />
        <UsageList
          title="By profile"
          rows={byProfile.slice(0, 6).map((p) => ({
            name: p.name || "(none)",
            runs: p.run_count,
            cost: p.cost,
            share: totalRuns > 0 ? p.run_count / totalRuns : 0,
          }))}
          totalCost={totalCost}
        />
      </div>
    </Panel>
  );
}

function UsageList({
  title,
  rows,
  totalCost,
}: {
  title: string;
  rows: { name: string; runs: number; cost: number; share: number }[];
  totalCost: number;
}) {
  return (
    <div className="p-1">
      <div className="px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-moon-600">
        {title}
      </div>
      {rows.length === 0 ? (
        <div className="px-2 py-2 text-xs text-moon-600">—</div>
      ) : (
        rows.map((r) => {
          const costPct = totalCost > 0 ? Math.round((r.cost / totalCost) * 100) : 0;
          return (
            <Tooltip
              key={r.name}
              content={`${r.runs} run${r.runs === 1 ? "" : "s"} · ${Math.round(r.share * 100)}% of runs${
                costPct > 0 ? ` · ${costPct}% of cost` : ""
              }`}
            >
              <div className="flex items-center gap-2 rounded-control px-2 py-1.5 text-sm hover:bg-ink-800/60">
                <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-moon-400">
                  {r.name}
                </span>
                <span className="shrink-0 font-mono text-[11px] tabular-nums text-moon-400">
                  {formatCost(r.cost)}
                </span>
              </div>
            </Tooltip>
          );
        })
      )}
    </div>
  );
}

function formatCost(n: number): string {
  if (n === 0) return "$0";
  if (n > 0 && n < 0.005) return "<$0.01";
  return `$${n.toFixed(2)}`;
}

// ---------------------------------------------------------------------------
// GitLab integrations — live state for the project's linked repos (open issues
// / MRs), with a click-to-peek + Import flow reused from the Integration lens.
// The host renders this only when the project has repo links; zero links = no
// panel. One issues fetch + one MRs fetch per repo (open only), no polling —
// the browse endpoints are TTL-cached server-side.
// ---------------------------------------------------------------------------

export function GitIntegrationsPanel({
  projectId,
  repos,
  filterToken,
}: {
  projectId: string;
  repos: RepoLinkOut[];
  filterToken: string;
}) {
  const connections = useConnections();
  // connection_id → name, so a failed fetch can name the connection it hit.
  const connectionName = useMemo(() => {
    const m = new Map((connections.data ?? []).map((c) => [c.id, c.name]));
    return (cid: string) => m.get(cid);
  }, [connections.data]);

  const [peek, setPeek] = useState<{ repo: RepoLinkOut; iid: string } | null>(null);

  return (
    <Panel
      title="Linked repositories"
      count={repos.length}
      action={
        <Tooltip content="Open the board filtered to this project, then toggle the Issues / MRs lens">
          <Link to="/tickets" search={{ f: filterToken }} className="text-[11px] text-moon-400 hover:text-lamp">
            Issues &amp; MRs →
          </Link>
        </Tooltip>
      }
    >
      <div>
        {repos.map((repo) => (
          <RepoBlock
            key={repo.id}
            repo={repo}
            connectionName={connectionName(repo.connection_id)}
            onOpenIssue={(iid) => setPeek({ repo, iid })}
          />
        ))}
      </div>

      {peek && (
        <ExternalItemPeek
          repo={peek.repo}
          kind="issue"
          iid={peek.iid}
          projectId={projectId}
          onClose={() => setPeek(null)}
        />
      )}
    </Panel>
  );
}

function RepoBlock({
  repo,
  connectionName,
  onOpenIssue,
}: {
  repo: RepoLinkOut;
  connectionName: string | undefined;
  onOpenIssue: (iid: string) => void;
}) {
  const issues = useQuery({
    queryKey: qk.integrations.issues(repo.id, { state: "opened" }),
    queryFn: () => integrationsApi.listIssues(repo.id, { state: "opened" }),
  });
  const mrs = useQuery({
    queryKey: qk.integrations.mrs(repo.id, { state: "opened" }),
    queryFn: () => integrationsApi.listMrs(repo.id, { state: "opened" }),
  });

  const issueRows = (issues.data?.items ?? []).slice(0, 5);

  return (
    <div className="border-b border-ink-700/40 px-3.5 py-2.5 last:border-b-0">
      {/* Repo header: name (real anchor to GitLab) + open counts. */}
      <div className="flex items-center gap-2">
        <a
          href={repo.web_url || undefined}
          target="_blank"
          rel="noreferrer"
          className={cn(
            "inline-flex min-w-0 items-center gap-1.5 rounded-[4px] text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lamp",
            repo.web_url ? "text-moon-100 hover:text-lamp" : "cursor-default text-moon-100",
          )}
        >
          <span className="truncate">{repo.display_name || repo.external_path}</span>
          {repo.web_url && <ExternalLink size={12} className="shrink-0 text-moon-600" />}
        </a>
        <Tooltip content="Open issues · open merge requests (a “+” means more exist beyond the first page)">
          <span className="ml-auto flex shrink-0 items-center gap-1.5 font-mono text-[11px] tabular-nums text-moon-500">
            <Count result={issues} />
            <span className="text-moon-600">issues</span>
            <span className="text-moon-600">·</span>
            <Count result={mrs} />
            <span className="text-moon-600">MRs</span>
          </span>
        </Tooltip>
      </div>

      {/* Body: connection error strip, loading, the top open issues, or empty. */}
      <div className="mt-1">
        {issues.isError ? (
          <div className="flex items-center gap-1.5 text-[11px] text-failed">
            <AlertTriangle size={12} className="shrink-0" />
            <span className="min-w-0 truncate">Couldn’t reach {connectionName ?? "GitLab"}.</span>
          </div>
        ) : issues.isLoading ? (
          <div className="flex items-center gap-1.5 py-0.5 text-[11px] text-moon-600">
            <Spinner size={11} /> Loading issues…
          </div>
        ) : issueRows.length === 0 ? (
          <p className="py-0.5 text-[11px] text-moon-600">No open issues.</p>
        ) : (
          <ul className="-mx-1.5 mt-0.5">
            {issueRows.map((item) => (
              <li key={item.iid}>
                <button
                  type="button"
                  onClick={() => onOpenIssue(String(item.iid))}
                  className="group flex w-full items-center gap-2 rounded-control px-1.5 py-1 text-left transition-colors hover:bg-ink-800/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lamp"
                >
                  <CircleDot size={12} className="shrink-0 text-moon-600" />
                  <span className="shrink-0 font-mono text-[11px] text-moon-600">#{item.iid}</span>
                  <span className="min-w-0 flex-1 truncate text-sm text-moon-200 group-hover:text-lamp">
                    {item.title}
                  </span>
                  {item.updated_at && (
                    <span className="shrink-0 text-[11px] text-moon-600">{relativeTime(item.updated_at)}</span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/** Render an open-count: the number returned (or `N+` when the page is partial),
 *  a spinner while loading, or an em dash on error. */
function Count({ result }: { result: UseQueryResult<GitLabItemPage> }) {
  if (result.isLoading) return <Spinner size={10} />;
  if (result.isError || !result.data) return <span className="text-moon-600">—</span>;
  const n = result.data.items.length;
  return <span className="text-moon-300">{result.data.next_page_token ? `${n}+` : n}</span>;
}
