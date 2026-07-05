import { useMemo, useState } from "react";
import { Link } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { Archive, ArchiveRestore, Search, Trash2, X } from "lucide-react";
import { Page } from "@/components/Page";
import { Button } from "@/ui/Button";
import { Input } from "@/ui/Input";
import { Dialog } from "@/ui/Dialog";
import { EmptyState } from "@/ui/EmptyState";
import { StatusPill } from "@/ui/StatusPill";
import { Tooltip } from "@/ui/Tooltip";
import { PriorityChip } from "@/components/PriorityChip";
import { ProjectTag } from "@/components/ProjectDot";
import { useTicketPages, ticketsApi } from "@/api/tickets";
import { useRuns } from "@/api/runs";
import { useProjectMap } from "@/api/projects";
import { qk, ApiError } from "@/api";
import type { RunOut, TicketOut } from "@/api/types";
import { toast } from "@/ui/Toast";
import { formatUsd, runStatusKind } from "@/lib/status";
import { absoluteTime, relativeTime } from "@/lib/time";
import { cn } from "@/lib/cn";

const POLL = 5000;
const PAGE_SIZE = 50;

function latestRunMap(runs: RunOut[]): Map<string, RunOut> {
  const m = new Map<string, RunOut>();
  for (const r of runs) if (!m.has(r.ticket_id)) m.set(r.ticket_id, r);
  return m;
}

export function ArchivePage() {
  const qc = useQueryClient();
  const projects = useProjectMap();

  // Server-side pagination: pull the archive newest-first, 50 at a time, and
  // reveal more only on demand. This is the fix for the archive loading every
  // row up front. Search filters the rows loaded so far; older matches surface
  // as the user loads more pages (see the search hint below).
  const ticketsQ = useTicketPages({ status: "archived", sort: "recent" }, PAGE_SIZE, {
    refetchInterval: POLL,
  });
  const runsQ = useRuns(undefined, { refetchInterval: POLL });
  const latest = useMemo(() => latestRunMap(runsQ.data ?? []), [runsQ.data]);

  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [deleting, setDeleting] = useState<TicketOut | null>(null);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: qk.tickets.all });
    qc.invalidateQueries({ queryKey: qk.runs.all });
  };

  // Rows already arrive newest-first from the server (sort=recent); flatten the
  // loaded pages in order. No client-side re-sort needed.
  const loaded = useMemo(
    () => (ticketsQ.data?.pages ?? []).flatMap((p) => p.items),
    [ticketsQ.data],
  );
  const total = ticketsQ.data?.pages[0]?.total ?? 0;

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return loaded;
    return loaded.filter((t) => {
      const proj = t.project_id ? projects.get(t.project_id)?.name ?? "" : "";
      return (
        t.title.toLowerCase().includes(q) ||
        proj.toLowerCase().includes(q) ||
        t.prompt.toLowerCase().includes(q)
      );
    });
  }, [loaded, query, projects]);

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
      toast.error(err instanceof ApiError ? err.message : "Restore failed");
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
      toast.error(err instanceof ApiError ? err.message : "Restore failed");
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
      toast.error(err instanceof ApiError ? err.message : "Delete failed");
    } finally {
      setDeleting(null);
    }
  }

  const isLoading = ticketsQ.isLoading;
  const empty = !isLoading && rows.length === 0;
  // "More on the server" — the filter may still have unloaded matches.
  const hasMore = ticketsQ.hasNextPage ?? false;
  const filtering = query.trim().length > 0;

  return (
    <Page
      bleed
      title="Archive"
      subtitle="Completed and declined tickets — searchable and restorable."
      actions={
        <div className="relative">
          <Search
            size={14}
            aria-hidden
            className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-moon-600"
          />
          <Input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search archive…"
            className="h-9 w-56 pl-8"
            aria-label="Search archive"
            autoComplete="off"
            spellCheck={false}
          />
        </div>
      }
    >
      {isLoading ? (
        <div className="py-20 text-center text-sm text-moon-600">Loading archive…</div>
      ) : empty ? (
        <div className="mx-auto max-w-2xl">
          <EmptyState
            icon={<Archive size={18} />}
            title={query ? "No archived tickets match" : "Nothing archived"}
            description={
              query
                ? "Adjust or clear the search to see the full archive."
                : "Archived tickets land here — searchable and one click from restore."
            }
            action={
              query ? (
                <Button variant="ghost" onClick={() => setQuery("")}>
                  Clear search
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
            <span className="w-32 shrink-0 text-right">Archived</span>
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
                  isSel && "bg-lamp/[0.06]",
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
                <Tooltip content={absoluteTime(t.updated_at)}>
                  <span className="w-32 shrink-0 text-right text-[11px] text-moon-600">
                    {relativeTime(t.updated_at)}
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

        {/* Footer: count + load more */}
        <div className="mt-3 flex items-center justify-between gap-3 px-1 text-[12px] text-moon-600">
          <span>
            {filtering ? (
              <>
                <span className="font-mono text-moon-400">{rows.length}</span> match
                {rows.length === 1 ? "" : "es"} in{" "}
                <span className="font-mono text-moon-400">{loaded.length}</span> loaded
                {hasMore ? " — load more to search the rest" : ""}
              </>
            ) : (
              <>
                Showing <span className="font-mono text-moon-400">{loaded.length}</span> of{" "}
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
