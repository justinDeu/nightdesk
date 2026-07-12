import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { CircleDot, Columns3, GitMerge, List as ListIcon, Plus } from "lucide-react";
import { Button } from "@/ui/Button";
import { EmptyState } from "@/ui/EmptyState";
import { toast } from "@/ui/Toast";
import { cn } from "@/lib/cn";
import { FilterBar } from "./FilterBar";
import { SavedViews, type ViewState } from "./SavedViews";
import { Board } from "./Board";
import { GroupedBoard } from "./GroupedBoard";
import { List } from "./List";
import { TicketPeek } from "./TicketPeek";
import { DisplayOptions } from "./DisplayOptions";
import { IntegrationLensPanel, type Lens, type LensScope } from "./IntegrationLens";
import { useProjectRepoLinks, useRepoLinks } from "@/api/integrations";
import {
  groupTickets,
  type DisplayContext,
  type GroupBy,
  type OrderBy,
} from "./displayModel";
import { BulkBar, type BulkMenu } from "@/components/BulkBar";
import { qk } from "@/api";
import { useTickets, ticketsApi } from "@/api/tickets";
import { useRuns, runsApi } from "@/api/runs";
import { useProjects } from "@/api/projects";
import { useProfiles } from "@/api/profiles";
import { useLabels } from "@/api/labels";
import type { ProjectOut, RunOut, TicketOut } from "@/api/types";
import { applyFilter, commitTrailingToken, parseFilter, type FilterContext } from "./filterModel";
import { useTicketActions } from "@/lib/ticketActions";
import { useKeybinds, type Keybind } from "@/lib/keymap";
import { openComposer } from "@/components/composerBus";

const POLL = 3000;

function latestRunMap(runs: RunOut[]): Map<string, RunOut> {
  const m = new Map<string, RunOut>();
  for (const r of runs) if (!m.has(r.ticket_id)) m.set(r.ticket_id, r);
  return m;
}

export function TicketsPage() {
  const navigate = useNavigate();
  const search = useSearch({ from: "/app/tickets" });
  const actions = useTicketActions();

  const view: "board" | "list" =
    search.view ?? (localStorage.getItem("nightdesk:tickets-view") === "list" ? "list" : "board");
  const filter = search.f ?? "";
  const activeViewId = search.viewId ?? null;

  const setSearch = (
    next: Partial<{ view: "board" | "list"; f: string; viewId?: string; lens?: "issues" | "mrs" }>,
  ) => {
    navigate({
      to: "/tickets",
      search: (prev) => ({ ...prev, ...next }),
      replace: true,
    });
  };

  const setView = (v: "board" | "list") => {
    localStorage.setItem("nightdesk:tickets-view", v);
    setSearch({ view: v });
  };
  const setFilter = (f: string) => setSearch({ f: f || undefined, viewId: undefined });

  // Display options (grouping / ordering) — persisted like the view toggle.
  const [displayOpen, setDisplayOpen] = useState(false);
  const [groupBy, setGroupByState] = useState<GroupBy>(
    () => (localStorage.getItem("nightdesk:tickets-groupby") as GroupBy) || "status",
  );
  const [orderBy, setOrderByState] = useState<OrderBy>(
    () => (localStorage.getItem("nightdesk:tickets-orderby") as OrderBy) || "manual",
  );
  const setGroupBy = (g: GroupBy) => {
    localStorage.setItem("nightdesk:tickets-groupby", g);
    setGroupByState(g);
  };
  const setOrderBy = (o: OrderBy) => {
    localStorage.setItem("nightdesk:tickets-orderby", o);
    setOrderByState(o);
  };

  // Data
  const ticketsQ = useTickets({ limit: 200 }, { refetchInterval: POLL });
  const runsQ = useRuns(undefined, { refetchInterval: POLL });
  const projectsQ = useProjects();
  const profilesQ = useProfiles();
  const labelsQ = useLabels();

  const ctx: FilterContext = {
    projects: projectsQ.data ?? [],
    labels: labelsQ.data ?? [],
    profiles: profilesQ.data ?? [],
  };

  const projectMap = useMemo(
    () => new Map((projectsQ.data ?? []).map((p) => [p.id, p])),
    [projectsQ.data],
  );

  // Integration lens (Issues / MRs). Toggles appear whenever the install has any
  // repo links at all; the panel scope depends on whether the filter resolves
  // exactly one *linked* project (§7). No single linked project → browse every
  // repo link; a single project with no links → action-directing empty state.
  const repoLinksQ = useRepoLinks();
  const allRepoLinks = repoLinksQ.data ?? [];

  // Distinct projects named by `project:` tokens in the current filter.
  const resolvedProjects = useMemo(() => {
    const toks = parseFilter(filter).tokens.filter((t) => t.key === "project");
    const projects = projectsQ.data ?? [];
    const seen = new Set<string>();
    const out: ProjectOut[] = [];
    for (const tok of toks) {
      const v = tok.value.toLowerCase();
      const p = projects.find(
        (pp) => pp.slug.toLowerCase() === v || pp.id === tok.value || pp.name.toLowerCase() === v,
      );
      if (p && !seen.has(p.id)) {
        seen.add(p.id);
        out.push(p);
      }
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter, projectsQ.data]);

  const singleProject = resolvedProjects.length === 1 ? resolvedProjects[0] : undefined;
  const projectReposQ = useProjectRepoLinks(singleProject?.id ?? "");
  const projectRepos = projectReposQ.data ?? [];

  const lensAvailable = allRepoLinks.length > 0;

  const lensScope: LensScope = singleProject
    ? projectRepos.length > 0
      ? { kind: "project", projectId: singleProject.id }
      : { kind: "unlinked", projectId: singleProject.id }
    : { kind: "global" };
  const lensRepos = lensScope.kind === "global" ? allRepoLinks : projectRepos;
  // Defer the empty state until the per-project repo-link list resolves, so a
  // linked project never flashes the "no repositories" state on the way in.
  const lensResolving =
    repoLinksQ.isLoading || (resolvedProjects.length === 1 && projectReposQ.isLoading);

  // Lens (Issues / MRs) is URL-driven so it survives refresh, deep links, and
  // middle-click. Absent param = the default Tickets lens. Toggle writes the
  // param with replace (same pattern as view / f / viewId), so it never pollutes
  // history. Falling back to Tickets when the lens is *genuinely* unavailable is
  // handled by the effect below — but only once repo links have resolved, so a
  // deep-linked Issues lens isn't bounced during the loading window.
  const lens: "tickets" | Lens = search.lens ?? "tickets";
  const setLens = (next: "tickets" | Lens) =>
    setSearch({ lens: next === "tickets" ? undefined : next });
  useEffect(() => {
    if (!repoLinksQ.isLoading && !lensAvailable && lens !== "tickets") setLens("tickets");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repoLinksQ.isLoading, lensAvailable]);
  const lensActive = lens !== "tickets" && lensAvailable;
  const latestRun = useMemo(() => latestRunMap(runsQ.data ?? []), [runsQ.data]);

  // Non-archived tickets, filtered.
  const filtered = useMemo(() => {
    const active = (ticketsQ.data ?? []).filter((t) => t.status !== "archived");
    return applyFilter(active, parseFilter(filter), ctx);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ticketsQ.data, filter, projectsQ.data, labelsQ.data, profilesQ.data]);

  // Grouped + ordered display of the filtered tickets — the single source of
  // truth for board columns, list groups, and the j/k cursor order.
  const displayCtx: DisplayContext = {
    projects: projectsQ.data ?? [],
    labels: labelsQ.data ?? [],
    profiles: profilesQ.data ?? [],
    latestRun,
  };
  const groups = useMemo(
    () => groupTickets(filtered, groupBy, orderBy, displayCtx),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [filtered, groupBy, orderBy, projectsQ.data, labelsQ.data, profilesQ.data, latestRun],
  );
  const visible = useMemo(() => groups.flatMap((g) => g.tickets), [groups]);

  // Selection + cursor + side peek
  const qc = useQueryClient();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  // Which bulk-bar menu the keyboard opened (p / Shift+P / s / Shift+L on a
  // selection), or null. Lives here so the binds can drive the BulkBar.
  const [bulkMenu, setBulkMenu] = useState<BulkMenu | null>(null);
  const [cursor, setCursor] = useState(0);
  // The keyboard cursor ring stays invisible until the user actually navigates
  // with the keyboard — otherwise the first card wears a full lamp ring on load
  // (which reads as broken/failed styling). Mouse selection uses the peek ring.
  const [cursorActive, setCursorActive] = useState(false);
  const [peekId, setPeekId] = useState<string | null>(null);
  const anchorRef = useRef<string | null>(null);
  const desiredRowRef = useRef(0);
  useEffect(() => {
    if (cursor >= visible.length) setCursor(Math.max(0, visible.length - 1));
  }, [visible.length, cursor]);
  const focused: TicketOut | undefined = visible[cursor];
  const hasSel = selected.size > 0;

  // Full navigation — the explicit "open" action (Enter / Open button / cmd-click).
  const open = (id: string) => navigate({ to: "/tickets/$id", params: { id } });

  // Plain click / keyboard selection — open the peek and prefetch the detail so
  // a subsequent full open is instant.
  const select = (id: string) => {
    const idx = visible.findIndex((t) => t.id === id);
    if (idx >= 0) setCursor(idx);
    setPeekId(id);
    rememberRow(id);
    qc.prefetchQuery({ queryKey: qk.tickets.detail(id), queryFn: () => ticketsApi.get(id) });
    qc.prefetchQuery({ queryKey: qk.runs.list(id), queryFn: () => runsApi.list({ ticket_id: id }) });
  };

  // --- Multi-selection ---------------------------------------------------------
  const toggleSelect = (id: string) => {
    setSelected((s) => {
      const n = new Set(s);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });
    anchorRef.current = id;
  };
  const rangeSelect = (id: string) => {
    const a = anchorRef.current;
    const bIdx = visible.findIndex((t) => t.id === id);
    const aIdx = a ? visible.findIndex((t) => t.id === a) : -1;
    if (aIdx < 0 || bIdx < 0) {
      toggleSelect(id);
      return;
    }
    const [lo, hi] = aIdx < bIdx ? [aIdx, bIdx] : [bIdx, aIdx];
    setSelected((s) => {
      const n = new Set(s);
      for (let i = lo; i <= hi; i++) n.add(visible[i].id);
      return n;
    });
    anchorRef.current = id;
  };
  const selectAll = () => setSelected(new Set(visible.map((t) => t.id)));
  const clearSelect = () => {
    setSelected(new Set());
    setBulkMenu(null);
    anchorRef.current = null;
  };

  const focusTo = (id: string) => {
    const idx = visible.findIndex((t) => t.id === id);
    if (idx < 0) return;
    setCursor(idx);
    if (peekId !== null) setPeekId(id);
  };
  // Which display group holds a ticket, and its row within it — for h/l column
  // navigation and the Vim-style "which row am I on" memory.
  const groupIndexOf = (id: string) => groups.findIndex((g) => g.tickets.some((t) => t.id === id));
  const rememberRow = (id: string) => {
    const gi = groupIndexOf(id);
    if (gi < 0) return;
    const idx = groups[gi].tickets.findIndex((t) => t.id === id);
    if (idx >= 0) desiredRowRef.current = idx;
  };

  // While the peek is open, j/k move the selection AND the peek follows. Moving
  // vertically updates the desired column row (Vim-style memory).
  const moveCursor = (delta: number) => {
    setCursorActive(true);
    const next = Math.max(0, Math.min(visible.length - 1, cursor + delta));
    const t = visible[next];
    if (!t) return;
    focusTo(t.id);
    rememberRow(t.id);
  };
  const extendSelect = (delta: number) => {
    setCursorActive(true);
    const next = Math.max(0, Math.min(visible.length - 1, cursor + delta));
    setSelected((s) => {
      const n = new Set(s);
      if (visible[cursor]) n.add(visible[cursor].id);
      if (visible[next]) n.add(visible[next].id);
      return n;
    });
    anchorRef.current = visible[next]?.id ?? null;
    const nid = visible[next]?.id;
    if (nid) {
      focusTo(nid);
      rememberRow(nid);
    }
  };

  const moveColumn = (delta: number) => {
    setCursorActive(true);
    if (!focused) return;
    const ci = groupIndexOf(focused.id);
    if (ci < 0) return;
    // Skip empty columns in the travel direction; never fail, never wrap.
    let ni = ci + delta;
    while (ni >= 0 && ni < groups.length && groups[ni].tickets.length === 0) ni += delta;
    if (ni < 0 || ni >= groups.length) return;
    const targetCol = groups[ni].tickets;
    // Land on the same row, clamped to the target's last card. desiredRow is not
    // overwritten here, so draft[6] → review[2] → draft lands back at draft[6].
    const landIdx = Math.min(desiredRowRef.current, targetCol.length - 1);
    const target = targetCol[Math.max(0, landIdx)];
    if (target) focusTo(target.id);
  };

  // Bulk-fallback: apply to the whole selection when one exists, else the focus.
  const bulkOrSingle = async (
    bulk: (ids: string[]) => Promise<unknown>,
    single: (t: TicketOut) => void,
  ) => {
    if (hasSel) {
      try {
        await bulk([...selected]);
        qc.invalidateQueries({ queryKey: qk.tickets.all });
        clearSelect();
      } catch (err) {
        toast.error("Bulk action failed", { error: err });
      }
    } else if (focused) {
      single(focused);
    }
  };
  const setPrio = (n: number) =>
    bulkOrSingle(
      (ids) => ticketsApi.bulkPriority({ ticket_ids: ids, priority: n }),
      (t) => actions.setPriority(t, n),
    );
  const runOrRequeue = () =>
    bulkOrSingle(
      (ids) => Promise.all(ids.map((id) => ticketsApi.runNow(id).catch(() => ticketsApi.requeue(id)))),
      (t) => (t.status === "review" ? actions.requeue(t) : actions.runNow(t)),
    );

  // Send draft(s) to the inbox — draft-only (the endpoint 409s otherwise).
  const sendToInboxAction = async () => {
    const pool = hasSel
      ? [...selected].map((id) => visible.find((t) => t.id === id)).filter(Boolean)
      : focused
        ? [focused]
        : [];
    const drafts = (pool as TicketOut[]).filter((t) => t.status === "draft");
    if (drafts.length === 0) {
      toast.error("Only draft tickets can be sent to the inbox");
      return;
    }
    try {
      await Promise.all(drafts.map((t) => ticketsApi.sendToInbox(t.id)));
      qc.invalidateQueries({ queryKey: qk.tickets.all });
      qc.invalidateQueries({ queryKey: ["inbox"] });
      if (hasSel) clearSelect();
      toast.success(`Sent ${drafts.length} to inbox`);
    } catch (err) {
      toast.error("Could not send to inbox", { error: err });
    }
  };

  const escChain = () => {
    if (hasSel) clearSelect();
    else if (peekId) setPeekId(null);
  };

  const binds: Keybind[] = [
    { combo: "j", label: "Cursor down", group: "Tickets", handler: () => moveCursor(1) },
    { combo: "k", label: "Cursor up", group: "Tickets", handler: () => moveCursor(-1) },
    { combo: "shift+j", label: "Extend selection down", group: "Tickets", handler: () => extendSelect(1) },
    { combo: "shift+k", label: "Extend selection up", group: "Tickets", handler: () => extendSelect(-1) },
    { combo: "h", label: "Previous column", group: "Board", handler: () => moveColumn(-1) },
    { combo: "l", label: "Next column", group: "Board", handler: () => moveColumn(1) },
    { combo: "Enter", label: "Open ticket", group: "Tickets", handler: () => focused && open(focused.id) },
    { combo: "o", label: "Open ticket", group: "Tickets", handler: () => focused && open(focused.id) },
    { combo: "space", label: "Open peek", group: "Tickets", handler: () => focused && select(focused.id) },
    { combo: "x", label: "Toggle select (or click the ✓ corner)", group: "Tickets", handler: () => focused && toggleSelect(focused.id) },
    { combo: "mod+a", label: "Select all", group: "Tickets", handler: selectAll },
    { combo: "Escape", label: "Clear selection / close peek", group: "Tickets", handler: escChain },
    { combo: "mod+b", label: "Toggle board / list", group: "Tickets", handler: () => setView(view === "board" ? "list" : "board") },
    { combo: "shift+v", label: "Display options", group: "Board", handler: () => setDisplayOpen((o) => !o) },
    { combo: "/", label: "Focus filter", group: "Tickets", handler: () => window.dispatchEvent(new CustomEvent("nightdesk:focus-filter")) },
    { combo: "a", label: hasSel ? "Archive selected" : "Archive", group: "Tickets", handler: () => bulkOrSingle((ids) => ticketsApi.bulkArchive({ ticket_ids: ids }), (t) => actions.archive(t)) },
    { combo: "r", label: hasSel ? "Run / requeue selected" : "Run now / requeue", group: "Tickets", handler: runOrRequeue },
    { combo: "e", label: "Edit (open detail)", group: "Tickets", handler: () => focused && open(focused.id) },
    { combo: "i", label: "Send draft to inbox", group: "Board", handler: sendToInboxAction },
    { combo: "p", label: hasSel ? "Set project on selected" : "Edit project (peek)", group: "Tickets", handler: () => (hasSel ? setBulkMenu("project") : focused && select(focused.id)) },
    { combo: "shift+p", label: hasSel ? "Set priority on selected" : "Edit priority (peek)", group: "Tickets", handler: () => (hasSel ? setBulkMenu("priority") : focused && select(focused.id)) },
    { combo: "s", label: hasSel ? "Set status on selected" : "Edit status (peek)", group: "Tickets", handler: () => (hasSel ? setBulkMenu("status") : focused && select(focused.id)) },
    { combo: "shift+l", label: hasSel ? "Set labels on selected" : "Edit labels (peek)", group: "Tickets", handler: () => (hasSel ? setBulkMenu("label") : focused && select(focused.id)) },
    { combo: "1", label: "Priority: Low", group: "Tickets", handler: () => setPrio(1) },
    { combo: "2", label: "Priority: Medium", group: "Tickets", handler: () => setPrio(2) },
    { combo: "3", label: "Priority: High", group: "Tickets", handler: () => setPrio(3) },
    { combo: "4", label: "Priority: Urgent", group: "Tickets", handler: () => setPrio(4) },
    { combo: "0", label: "Priority: None", group: "Tickets", handler: () => setPrio(0) },
  ];
  // Route-scoped: these shadow same-combo global binds (e.g. "/" focuses the
  // filter here instead of opening the command palette) while Tickets is active.
  useKeybinds(binds.map((b) => ({ ...b, scope: "route" as const })));

  const peekTicket = peekId ? visible.find((t) => t.id === peekId) : undefined;
  // If the peeked ticket left the filtered set, drop the peek.
  useEffect(() => {
    if (peekId && !visible.some((t) => t.id === peekId)) setPeekId(null);
  }, [peekId, visible]);

  // One shared signal that *either* side-peek (ticket or issue/MR) is open, so a
  // single right-padding rule keeps the header controls and the work surface out
  // from under the rail. The issue/MR peek state lives inside IntegrationLensPanel
  // and is mirrored here; when the lens switches off the panel unmounts and takes
  // its peek with it, so clear the mirror rather than keep reserving the width.
  const [lensPeekOpen, setLensPeekOpen] = useState(false);
  useEffect(() => {
    if (!lensActive) setLensPeekOpen(false);
  }, [lensActive]);
  const anyPeekOpen = !!peekTicket || lensPeekOpen;

  const currentViewState: ViewState = { filter, view };

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Control bar — title, saved views, filter, view, new. Below md it wraps
          to two rows: title + controls on top, the full-width filter beneath
          (order-last), so a narrow phone never overlaps the filter and header. */}
      <header
        className={cn(
          "relative z-40 flex shrink-0 flex-wrap items-center gap-2.5 border-b border-ink-700 bg-ink-950 px-4 py-2 transition-[padding] duration-150 md:h-12 md:flex-nowrap md:py-0",
          anyPeekOpen && "lg:pr-[440px]",
        )}
      >
        <span className="shrink-0 font-display text-sm font-semibold tracking-tight text-moon-100">
          Tickets
        </span>
        <div className="hidden min-w-0 max-w-[34%] shrink items-center lg:flex">
          <SavedViews
            current={currentViewState}
            activeId={activeViewId}
            onApply={(state, id) => setSearch({ view: state.view, f: commitTrailingToken(state.filter) || undefined, viewId: id })}
            onClearActive={() => setSearch({ viewId: undefined })}
          />
        </div>
        <div className="order-last w-full min-w-0 md:order-none md:flex-1">
          <FilterBar value={filter} onChange={setFilter} ctx={ctx} />
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-2.5 md:ml-0">
          <DisplayOptions
            open={displayOpen}
            onOpenChange={setDisplayOpen}
            groupBy={groupBy}
            orderBy={orderBy}
            onGroupBy={setGroupBy}
            onOrderBy={setOrderBy}
          />
          {lensAvailable && (
            <div className="flex shrink-0 items-center rounded-control border border-ink-700 bg-ink-900 p-0.5">
              <ViewToggle active={lens === "tickets"} onClick={() => setLens("tickets")} icon={<ListIcon size={15} />} label="Tickets" />
              <ViewToggle active={lens === "issues"} onClick={() => setLens("issues")} icon={<CircleDot size={15} />} label="Issues" />
              <ViewToggle active={lens === "mrs"} onClick={() => setLens("mrs")} icon={<GitMerge size={15} />} label="MRs" />
            </div>
          )}
          <div className="flex shrink-0 items-center rounded-control border border-ink-700 bg-ink-900 p-0.5">
            <ViewToggle active={view === "board"} onClick={() => setView("board")} icon={<Columns3 size={15} />} label="Board" />
            <ViewToggle active={view === "list"} onClick={() => setView("list")} icon={<ListIcon size={15} />} label="List" />
          </div>
          <Button
            size="sm"
            variant="primary"
            leadingIcon={<Plus size={14} />}
            onClick={() => openComposer()}
            className="shrink-0"
            aria-label="New ticket"
          >
            <span className="hidden sm:inline">New ticket</span>
          </Button>
        </div>
      </header>

      {/* Work surface — fills the viewport; columns/list scroll internally. */}
      <div
        className={cn(
          "min-h-0 flex-1 transition-[padding] duration-150",
          anyPeekOpen && "lg:pr-[440px]",
        )}
      >
        {lensActive ? (
          <IntegrationLensPanel
            scope={lensScope}
            lens={lens as Lens}
            repos={lensRepos}
            projects={projectsQ.data ?? []}
            loading={lensResolving}
            onPeekActiveChange={setLensPeekOpen}
          />
        ) : ticketsQ.isLoading ? (
          <div className="grid h-full place-items-center text-sm text-moon-600">Loading tickets…</div>
        ) : visible.length === 0 ? (
          <div className="grid h-full place-items-center px-4">
            <EmptyState
              icon={<ListIcon size={18} />}
              title={filter ? "No tickets match this filter" : "No tickets yet"}
              description={
                filter
                  ? "Adjust or clear the filter to see more."
                  : "Create your first ticket to seed the stream."
              }
              action={
                filter ? (
                  <Button variant="ghost" onClick={() => setFilter("")}>
                    Clear filter
                  </Button>
                ) : (
                  <Button variant="primary" onClick={() => openComposer()}>
                    New ticket
                  </Button>
                )
              }
            />
          </div>
        ) : view === "board" ? (
          <div className="h-full px-4 pb-3 pt-3">
            {groupBy === "status" ? (
              <Board
                tickets={filtered}
                projects={projectMap}
                latestRun={latestRun}
                orderBy={orderBy}
                onSelect={select}
                onRangeSelect={rangeSelect}
                onToggleSelect={toggleSelect}
                selected={selected}
                focusedId={cursorActive ? focused?.id : undefined}
                peekedId={peekId ?? undefined}
                peekOpen={!!peekTicket}
              />
            ) : (
              <GroupedBoard
                groups={groups}
                groupBy={groupBy}
                projects={projectMap}
                latestRun={latestRun}
                onSelect={select}
                onRangeSelect={rangeSelect}
                onToggleSelect={toggleSelect}
                selected={selected}
                focusedId={cursorActive ? focused?.id : undefined}
                peekedId={peekId ?? undefined}
                peekOpen={!!peekTicket}
              />
            )}
          </div>
        ) : (
          <div className="h-full overflow-y-auto px-4 pb-3 pt-3">
            <List
              groups={groups}
              projects={projectsQ.data ?? []}
              profiles={profilesQ.data ?? []}
              labels={labelsQ.data ?? []}
              latestRun={latestRun}
              onSelect={select}
              onToggleSelect={toggleSelect}
              onRangeSelect={rangeSelect}
              focusedId={cursorActive ? focused?.id : undefined}
              peekedId={peekId ?? undefined}
              selected={selected}
            />
          </div>
        )}
      </div>

      {hasSel && (
        <BulkBar
          ids={[...selected]}
          projects={projectsQ.data ?? []}
          labels={labelsQ.data ?? []}
          openMenu={bulkMenu}
          onOpenMenuChange={setBulkMenu}
          onClear={clearSelect}
          onDone={() => qc.invalidateQueries({ queryKey: qk.tickets.all })}
        />
      )}

      {peekTicket && (
        <TicketPeek
          key={peekTicket.id}
          ticket={peekTicket}
          project={peekTicket.project_id ? projectMap.get(peekTicket.project_id) : undefined}
          latestRun={latestRun.get(peekTicket.id)}
          projects={projectsQ.data ?? []}
          labels={labelsQ.data ?? []}
          onClose={() => setPeekId(null)}
          onOpenFull={() => open(peekTicket.id)}
        />
      )}
    </div>
  );
}

function ViewToggle({
  active,
  onClick,
  icon,
  label,
}: {
  active: boolean;
  onClick: () => void;
  icon: ReactNode;
  label: string;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-[6px] px-2.5 py-1 text-xs font-medium transition-colors",
        active ? "bg-ink-800 text-moon-100" : "text-moon-400 hover:text-moon-100",
      )}
    >
      {icon}
      <span className="hidden sm:inline">{label}</span>
    </button>
  );
}
