import { useMemo, useState } from "react";
import { Link } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { Archive, ArchiveRestore, ArrowDownWideNarrow, ArrowUpWideNarrow, Trash2, X } from "lucide-react";
import { Page } from "@/components/Page";
import { Button } from "@/ui/Button";
import { Input } from "@/ui/Input";
import { Select } from "@/ui/Select";
import { IconButton } from "@/ui/IconButton";
import { Dialog } from "@/ui/Dialog";
import { EmptyState } from "@/ui/EmptyState";
import { StatusPill } from "@/ui/StatusPill";
import { Tooltip } from "@/ui/Tooltip";
import { PriorityChip } from "@/components/PriorityChip";
import { ProjectTag } from "@/components/ProjectDot";
import { useTicketPages, ticketsApi } from "@/api/tickets";
import { useRuns } from "@/api/runs";
import { useProjectMap, useProjects } from "@/api/projects";
import { useLabels } from "@/api/labels";
import { useProfiles } from "@/api/profiles";
import { qk } from "@/api";
import type { RunOut, TicketOut } from "@/api/types";
import { toast } from "@/ui/Toast";
import { formatUsd, runStatusKind } from "@/lib/status";
import { absoluteTime, relativeTime } from "@/lib/time";
import { useKeybinds } from "@/lib/keymap";
import { cn } from "@/lib/cn";
import { FilterBar } from "@/routes/tickets/FilterBar";
import {
  ARCHIVE_FILTER_KEYS,
  filterToQuery,
  parseFilter,
  type FilterContext,
} from "@/routes/tickets/filterModel";

const POLL = 5000;
const PAGE_SIZE = 50;

/** Sort keys the Archive exposes → the server `sort` value + column label. */
const SORT_OPTIONS = [
  { value: "recent", label: "Archived" },
  { value: "created", label: "Created" },
  { value: "priority", label: "Priority" },
  { value: "cost", label: "Cost" },
] as const;
type SortKey = (typeof SORT_OPTIONS)[number]["value"];
type SortDir = "asc" | "desc";

const SORT_STORAGE_KEY = "nightdesk:archive-sort";

function loadSort(): { key: SortKey; dir: SortDir } {
  const fallback = { key: "recent" as SortKey, dir: "desc" as SortDir };
  try {
    const raw = localStorage.getItem(SORT_STORAGE_KEY);
    if (!raw) return fallback;
    const [key, dir] = raw.split(":");
    const validKey = SORT_OPTIONS.some((o) => o.value === key) ? (key as SortKey) : fallback.key;
    const validDir: SortDir = dir === "asc" ? "asc" : "desc";
    return { key: validKey, dir: validDir };
  } catch {
    return fallback;
  }
}

function latestRunMap(runs: RunOut[]): Map<string, RunOut> {
  const m = new Map<string, RunOut>();
  for (const r of runs) if (!m.has(r.ticket_id)) m.set(r.ticket_id, r);
  return m;
}

export function ArchivePage() {
  const qc = useQueryClient();
  const projects = useProjectMap();
  const projectsQ = useProjects();
  const labelsQ = useLabels();
  const profilesQ = useProfiles();

  const [filter, setFilter] = useState("");
  const [sort, setSortState] = useState(() => loadSort());
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [deleting, setDeleting] = useState<TicketOut | null>(null);

  const setSort = (next: { key: SortKey; dir: SortDir }) => {
    setSortState(next);
    try {
      localStorage.setItem(SORT_STORAGE_KEY, `${next.key}:${next.dir}`);
    } catch {
      /* private mode — sort just won't persist */
    }
  };

  // "/" focuses the filter, mirroring the Tickets board so muscle memory
  // carries over. The FilterBar listens for this event.
  useKeybinds([
    {
      combo: "/",
      label: "Focus filter",
      group: "Archive",
      scope: "route",
      handler: () => window.dispatchEvent(new CustomEvent("nightdesk:focus-filter")),
    },
  ]);

  const ctx: FilterContext = {
    projects: projectsQ.data ?? [],
    labels: labelsQ.data ?? [],
    profiles: profilesQ.data ?? [],
  };

  // Parse the raw filter with the archive vocabulary (no `status:`, adds
  // `outcome:`) and resolve it to server query params so filtering happens
  // server-side and composes with limit/offset paging — the count and the page
  // stay honest instead of filtering only already-loaded rows.
  const parsed = useMemo(() => parseFilter(filter, ARCHIVE_FILTER_KEYS), [filter]);
  const serverFilter = useMemo(
    () => filterToQuery(parsed, ctx),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [parsed, projectsQ.data, labelsQ.data, profilesQ.data],
  );
  const filterActive = parsed.tokens.length > 0 || parsed.text.trim().length > 0;

  const ticketsQ = useTicketPages(
    { status: "archived", sort: sort.key, order: sort.dir, ...serverFilter },
    PAGE_SIZE,
    { refetchInterval: POLL },
  );
  const runsQ = useRuns(undefined, { refetchInterval: POLL });
  const latest = useMemo(() => latestRunMap(runsQ.data ?? []), [runsQ.data]);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: qk.tickets.all });
    qc.invalidateQueries({ queryKey: qk.runs.all });
  };

  // Rows arrive already filtered + sorted from the server; flatten the loaded
  // pages in order. No client-side filter or re-sort.
  const rows = useMemo(
    () => (ticketsQ.data?.pages ?? []).flatMap((p) => p.items),
    [ticketsQ.data],
  );
  const total = ticketsQ.data?.pages[0]?.total ?? 0;

  const allSelected = rows.length > 0 && rows.every((t) => selected.has(t.id));
  const toggleAll = () =>
    setSelected(allSelected ? new Set() : new Set(rows.map((t) => t.id)));
  const toggle = (id: string) => {
    const n = new Set(selected);
    n.has(id) ? n.delete(id) : n.add(id);
    setSelected(n);
  };

  async function unarchiveOne(t: TicketOut) {
    try {
      await ticketsApi.unarchive(t.id);
      invalidate();
      toast.success("Ticket restored");
    } catch (err) {
      toast.error("Restore failed", { error: err });
    }
  }

  async function bulkUnarchive() {
    const ids = [...selected];
    try {
      await ticketsApi.bulkUnarchive({ ticket_ids: ids });
      invalidate();
      setSelected(new Set());
      toast.success(`Restored ${ids.length} ticket${ids.length === 1 ? "" : "s"}`);
    } catch (err) {
      toast.error("Restore failed", { error: err });
    }
  }

  async function confirmDelete() {
    if (!deleting) return;
    const t = deleting;
    try {
      await ticketsApi.remove(t.id);
      invalidate();
      setSelected((s) => {
        const n = new Set(s);
        n.delete(t.id);
        return n;
      });
      toast.success("Ticket permanently deleted");
    } catch (err) {
      toast.error("Delete failed", { error: err });
    } finally {
      setDeleting(null);
    }
  }

  const isLoading = ticketsQ.isLoading;
  const empty = !isLoading && total === 0;
  const hasMore = ticketsQ.hasNextPage ?? false;
  const dateLabel = sort.key === "created" ? "Created" : "Archived";

  return (
    <Page
      bleed
      title="Archive"
      subtitle="Completed and declined tickets — filter, sort, and restore."
    >
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <div className="min-w-[16rem] flex-1">
          <FilterBar
            value={filter}
            onChange={setFilter}
            ctx={ctx}
            keys={ARCHIVE_FILTER_KEYS}
            placeholder="Filter — project: label: priority: profile: outcome: or free text"
          />
        </div>
        <SortControl sort={sort} onChange={setSort} />
      </div>

      {isLoading ? (
        <div className="py-20 text-center text-sm text-moon-600">Loading archive…</div>
      ) : empty ? (
        <div className="mx-auto max-w-2xl">
          <EmptyState
            icon={<Archive size={18} />}
            title={filterActive ? "No archived tickets match" : "Nothing archived"}
            description={
              filterActive
                ? "No archived ticket matches these filters. Clear them to see the full archive."
                : "Archived tickets land here — searchable and one click from restore."
            }
            action={
              filterActive ? (
                <Button variant="ghost" onClick={() => setFilter("")}>
                  Clear filters
                </Button>
              ) : undefined
            }
          />
        </div>
      ) : (
        <>
        <div className="overflow-hidden rounded-card border border-ink-700/50 shadow-[var(--shadow-raised)]">
          {/* Header */}
          <div className="flex items-center gap-3 border-b border-ink-700 bg-ink-900 px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-moon-600">
            <input
              type="checkbox"
              checked={allSelected}
              onChange={toggleAll}
              className="accent-lamp"
              aria-label="Select all"
            />
            <span className="flex-1">Title</span>
            <span className="hidden w-32 shrink-0 md:block">Project</span>
            <span className="hidden w-10 shrink-0 text-center sm:block">Pri</span>
            <span className="hidden w-24 shrink-0 text-right lg:block">Last run</span>
            <span className="hidden w-16 shrink-0 text-right sm:block">Cost</span>
            <span className="w-32 shrink-0 text-right">{dateLabel}</span>
            <span className="w-16 shrink-0" />
          </div>

          {rows.map((t) => {
            const run = latest.get(t.id);
            const isSel = selected.has(t.id);
            const project = t.project_id ? projects.get(t.project_id) : undefined;
            return (
              <div
                key={t.id}
                className={cn(
                  "group flex items-center gap-3 border-t border-ink-700/60 px-3 py-2.5 text-sm transition-colors hover:bg-ink-800",
                  isSel && "wash-selected",
                )}
              >
                <input
                  type="checkbox"
                  checked={isSel}
                  onChange={() => toggle(t.id)}
                  className="accent-lamp"
                  aria-label={`Select ${t.title}`}
                />
                <Link
                  to="/tickets/$id"
                  params={{ id: t.id }}
                  className="min-w-0 flex-1 truncate text-moon-100 hover:text-lamp"
                >
                  {t.title}
                </Link>
                <span className="hidden w-32 shrink-0 md:block">
                  <ProjectTag project={project} showNone />
                </span>
                <span className="hidden w-10 shrink-0 justify-center sm:flex">
                  <PriorityChip value={t.priority} />
                </span>
                <span className="hidden w-24 shrink-0 justify-end lg:flex">
                  {run ? (
                    <StatusPill status={runStatusKind(run.exit_status)} />
                  ) : (
                    <span className="text-[11px] text-moon-600">no runs</span>
                  )}
                </span>
                <span className="hidden w-16 shrink-0 text-right font-mono text-[11px] text-moon-400 sm:block">
                  {run?.cost_usd != null ? formatUsd(run.cost_usd) : "—"}
                </span>
                <Tooltip content={absoluteTime(sort.key === "created" ? t.created_at : t.updated_at)}>
                  <span className="w-32 shrink-0 text-right text-[11px] text-moon-600">
                    {relativeTime(sort.key === "created" ? t.created_at : t.updated_at)}
                  </span>
                </Tooltip>
                <span
                  className="flex w-16 shrink-0 items-center justify-end gap-0.5 opacity-0 transition-opacity group-hover:opacity-100"
                  onClick={(e) => e.stopPropagation()}
                >
                  <Tooltip content="Restore">
                    <button
                      onClick={() => unarchiveOne(t)}
                      className="rounded-control p-1.5 text-moon-400 hover:bg-ink-700 hover:text-success"
                      aria-label={`Restore ${t.title}`}
                    >
                      <ArchiveRestore size={14} />
                    </button>
                  </Tooltip>
                  <Tooltip content="Delete permanently">
                    <button
                      onClick={() => setDeleting(t)}
                      className="rounded-control p-1.5 text-moon-400 hover:bg-failed/15 hover:text-failed"
                      aria-label={`Delete ${t.title}`}
                    >
                      <Trash2 size={14} />
                    </button>
                  </Tooltip>
                </span>
              </div>
            );
          })}
        </div>

        {/* Footer: count + load more. The count is the server-side filtered
            total, so it stays honest across pages. */}
        <div className="mt-3 flex items-center justify-between gap-3 px-1 text-[12px] text-moon-600">
          <span>
            {filterActive ? (
              <>
                <span className="font-mono text-moon-400">{total}</span> match
                {total === 1 ? "" : "es"}
                {hasMore ? (
                  <>
                    {" "}— showing <span className="font-mono text-moon-400">{rows.length}</span>
                  </>
                ) : null}
              </>
            ) : (
              <>
                Showing <span className="font-mono text-moon-400">{rows.length}</span> of{" "}
                <span className="font-mono text-moon-400">{total}</span>
              </>
            )}
          </span>
          {hasMore ? (
            <Button
              size="sm"
              variant="subtle"
              onClick={() => ticketsQ.fetchNextPage()}
              disabled={ticketsQ.isFetchingNextPage}
            >
              {ticketsQ.isFetchingNextPage ? "Loading…" : `Load ${PAGE_SIZE} more`}
            </Button>
          ) : null}
        </div>
        </>
      )}

      <BulkBar
        count={selected.size}
        onClear={() => setSelected(new Set())}
        onUnarchive={bulkUnarchive}
      />

      <DeleteDialog ticket={deleting} onCancel={() => setDeleting(null)} onConfirm={confirmDelete} />
    </Page>
  );
}

function SortControl({
  sort,
  onChange,
}: {
  sort: { key: SortKey; dir: SortDir };
  onChange: (next: { key: SortKey; dir: SortDir }) => void;
}) {
  const nextDir: SortDir = sort.dir === "desc" ? "asc" : "desc";
  return (
    <div className="flex shrink-0 items-center gap-1.5">
      <label className="text-[11px] font-medium uppercase tracking-wide text-moon-600">
        Sort
      </label>
      <Select
        value={sort.key}
        onChange={(e) => onChange({ key: e.target.value as SortKey, dir: sort.dir })}
        className="h-9 w-32"
        aria-label="Sort archive by"
      >
        {SORT_OPTIONS.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </Select>
      <IconButton
        size="md"
        label={sort.dir === "desc" ? "Descending — switch to ascending" : "Ascending — switch to descending"}
        icon={sort.dir === "desc" ? <ArrowDownWideNarrow size={16} /> : <ArrowUpWideNarrow size={16} />}
        onClick={() => onChange({ key: sort.key, dir: nextDir })}
        className="border-ink-700 bg-ink-950"
      />
    </div>
  );
}

function BulkBar({
  count,
  onClear,
  onUnarchive,
}: {
  count: number;
  onClear: () => void;
  onUnarchive: () => void;
}) {
  if (count === 0) return null;
  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-6 z-30 flex justify-center px-4">
      <div className="pointer-events-auto flex items-center gap-2 rounded-card border border-ink-700 bg-ink-800 px-3 py-2 shadow-[var(--shadow-pop)]">
        <span className="pr-1 text-sm text-moon-100">
          <span className="font-mono">{count}</span> selected
        </span>
        <Button
          size="sm"
          variant="subtle"
          leadingIcon={<ArchiveRestore size={13} />}
          onClick={onUnarchive}
        >
          Restore
        </Button>
        <button
          onClick={onClear}
          className="ml-1 rounded-control p-1 text-moon-400 hover:bg-ink-700 hover:text-moon-100"
          aria-label="Clear selection"
        >
          <X size={15} />
        </button>
      </div>
    </div>
  );
}

function DeleteDialog({
  ticket,
  onCancel,
  onConfirm,
}: {
  ticket: TicketOut | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const [text, setText] = useState("");
  // Reset the typed confirmation whenever the target changes.
  const target = ticket?.id ?? "";
  const armed = text.trim().toUpperCase() === "DELETE";

  return (
    <Dialog
      open={ticket != null}
      onOpenChange={(o) => {
        if (!o) {
          setText("");
          onCancel();
        }
      }}
      size="sm"
      title="Delete permanently"
      description="This removes the ticket and its run history for good. It cannot be undone."
      footer={
        <>
          <Button
            variant="ghost"
            onClick={() => {
              setText("");
              onCancel();
            }}
          >
            Cancel
          </Button>
          <Button
            variant="danger"
            disabled={!armed}
            leadingIcon={<Trash2 size={14} />}
            onClick={() => {
              setText("");
              onConfirm();
            }}
          >
            Delete forever
          </Button>
        </>
      }
    >
      <div key={target} className="space-y-3">
        <p className="truncate rounded-control border border-ink-700 bg-ink-950 px-3 py-2 font-mono text-[13px] text-moon-100">
          {ticket?.title}
        </p>
        <p className="text-sm text-moon-400">
          Type <span className="font-mono font-semibold text-moon-100">DELETE</span> to confirm.
        </p>
        <Input
          autoFocus
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="DELETE"
          invalid={text.length > 0 && !armed}
          onKeyDown={(e) => {
            if (e.key === "Enter" && armed) {
              setText("");
              onConfirm();
            }
          }}
        />
      </div>
    </Dialog>
  );
}
