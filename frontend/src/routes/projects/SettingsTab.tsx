import { useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { ChevronDown, ExternalLink, Plus, X } from "lucide-react";
import { Button } from "@/ui/Button";
import { Input } from "@/ui/Input";
import { Switch } from "@/ui/Switch";
import { Spinner } from "@/ui/Spinner";
import { Tooltip } from "@/ui/Tooltip";
import { Badge } from "@/ui/Badge";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/ui/DropdownMenu";
import { toast, describeError } from "@/ui/Toast";
import { ApiError } from "@/api/client";
import { projectsApi } from "@/api/projects";
import { useLabels } from "@/api/labels";
import {
  useConnections,
  useProjectRepoLinks,
  useRepoLinks,
  useRepoSuggest,
  useToggleProjectRepoLink,
} from "@/api/integrations";
import { qk } from "@/api/keys";
import { PathInput } from "@/components/PathInput";
import { ColorPicker } from "@/routes/settings/parts/ColorPicker";
import { ListEditor } from "@/routes/settings/parts/ListEditor";
import { ConfirmDialog } from "@/routes/settings/parts/ConfirmDialog";
import { SaveBar, useEditableForm } from "@/routes/settings/parts/SaveBar";
import type { ProjectOut, RepoLinkOut, WorkspaceKind } from "@/api/types";
import { cn } from "@/lib/cn";

/**
 * Settings tab — every project-scoped setting in the project's own home
 * (docs/design/project-control-plane.md §Settings). A frontend lift: the
 * editable fields all round-trip through PATCH /projects/{id} (identity +
 * execution defaults), the repo-link toggle API, and the project archive
 * (DELETE) endpoint.
 *
 * The design mockup (project-control-plane-mockups.html, "Settings" stage)
 * also lists a few controls that have NO backend home on the Project entity
 * today — a default profile, commit-on-finish, default-labels-on-create, and
 * project-scoped crons. Rather than fake editors that silently don't persist
 * (which would violate "every setting editable here round-trips"), those render
 * as honest provenance notes pointing at where the setting actually lives
 * (per-ticket / instance-wide / Scheduled). When the backend grows those
 * fields, the rows become editors with no layout change.
 *
 * Layout: a sticky in-page section rail (~180px, click-to-jump + scroll-spy,
 * amber dot on dirty sections) sits left of a single-column ledger form
 * (uppercase section headers, hairline dividers, label-left/control-right
 * rows). Sections collapse via their chevron. A sticky Save/Revert bar arms
 * when the form is dirty, with the app-wide unsaved-edit guard.
 */
export function SettingsTab({ project }: { project: ProjectOut }) {
  const qc = useQueryClient();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmArchive, setConfirmArchive] = useState(false);
  const [archiving, setArchiving] = useState(false);

  const key = `${project.id}:${project.updated_at}`;
  const { form, setForm, dirty, discard, commit } = useEditableForm(
    project,
    buildForm,
    key,
  );

  // Per-section dirty flags drive the section-rail amber dots and the
  // per-row "edited" treatment. Computed against the server baseline so they
  // reflect genuine divergence, not just any form touch.
  const server = useMemo(() => buildForm(project), [project]);
  const identityDirty =
    !!form &&
    (form.name !== server.name ||
      form.slug !== server.slug ||
      (form.color ?? null) !== (server.color ?? null) ||
      form.source_path !== server.source_path);
  const execDirty =
    !!form &&
    (form.default_workspace_mode !== server.default_workspace_mode ||
      form.default_base_ref !== server.default_base_ref ||
      form.default_worktree_name_template !== server.default_worktree_name_template ||
      listsDiffer(form.default_toolchains, server.default_toolchains) ||
      listsDiffer(form.default_tool_paths, server.default_tool_paths));

  // Collapsible sections. Labels & Automation start collapsed (the mockup's
  // default) — they carry notes, not the primary editable surface.
  const [collapsed, setCollapsed] = useState<Record<SectionId, boolean>>({
    identity: false,
    execution: false,
    repo: false,
    labels: true,
    automation: true,
    danger: false,
  });
  const toggleSection = (id: SectionId) =>
    setCollapsed((c) => ({ ...c, [id]: !c[id] }));

  const rootRef = useRef<HTMLDivElement>(null);
  const activeSection = useScrollSpy(SECTION_IDS, rootRef);

  function jumpTo(id: SectionId) {
    setCollapsed((c) => ({ ...c, [id]: false }));
    // Wait a frame so the just-expanded section has layout before scrolling.
    requestAnimationFrame(() => {
      const el = document.getElementById(`pset-${id}`);
      el?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  async function save() {
    if (!form) return;
    if (!form.name.trim() || !form.source_path.trim()) {
      setError("Name and source path are required.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await projectsApi.update(project.id, {
        name: form.name.trim(),
        slug: form.slug.trim() || null,
        source_path: form.source_path.trim(),
        color: form.color || null,
        default_workspace_mode: form.default_workspace_mode,
        default_base_ref: form.default_base_ref.trim() || null,
        default_worktree_name_template: form.default_worktree_name_template.trim() || null,
        default_toolchains: form.default_toolchains,
        default_tool_paths: form.default_tool_paths,
      });
      await Promise.all([
        qc.invalidateQueries({ queryKey: qk.projects.detail(project.id) }),
        qc.invalidateQueries({ queryKey: qk.projects.all }),
      ]);
      commit();
      toast.success("Project settings saved");
    } catch (err) {
      setError(describeError(err));
      toast.error("Could not save project settings", { error: err });
    } finally {
      setSaving(false);
    }
  }

  async function archive() {
    setArchiving(true);
    try {
      await projectsApi.remove(project.id);
      await Promise.all([
        qc.invalidateQueries({ queryKey: qk.projects.detail(project.id) }),
        qc.invalidateQueries({ queryKey: qk.projects.all }),
        qc.invalidateQueries({ queryKey: qk.projects.attention }),
      ]);
      commit();
      toast.success(`Archived “${project.name}”`);
      setConfirmArchive(false);
    } catch (err) {
      toast.error("Could not archive project", { error: err });
    } finally {
      setArchiving(false);
    }
  }

  if (!form) {
    return (
      <div className="flex items-center gap-2 p-6 text-sm text-moon-400">
        <Spinner /> Loading settings…
      </div>
    );
  }

  const archived = !!project.archived_at;

  return (
    <div ref={rootRef} className="relative">
      <div className="flex items-start gap-8 px-4 pb-24 pt-6 sm:px-6">
        {/* In-page section rail: sticky, click-to-jump + scroll-spy + dirty dots */}
        <nav
          aria-label="Project settings sections"
          className="sticky top-0 hidden w-[176px] shrink-0 self-start md:block"
        >
          <ul className="flex flex-col gap-0.5 pt-2">
            {SECTIONS.map((s) => {
              const dot =
                (s.id === "identity" && identityDirty) || (s.id === "execution" && execDirty);
              return (
                <li key={s.id}>
                  <button
                    type="button"
                    onClick={() => jumpTo(s.id)}
                    aria-current={activeSection === s.id ? "true" : undefined}
                    className={cn(
                      "group flex w-full items-center gap-2 rounded-control border-l-2 px-2.5 py-1.5 text-left text-[11.5px] font-medium uppercase tracking-wide transition-colors",
                      activeSection === s.id
                        ? "border-lamp bg-ink-900 text-moon-100"
                        : "border-transparent text-moon-600 hover:bg-ink-900 hover:text-moon-100",
                    )}
                  >
                    <span className="flex-1">{s.label}</span>
                    {dot && (
                      <Tooltip content="Unsaved changes in this section">
                        <span className="h-1.5 w-1.5 rounded-full bg-warn shadow-[0_0_0_2px_rgba(217,161,61,0.18)]" />
                      </Tooltip>
                    )}
                  </button>
                </li>
              );
            })}
          </ul>
        </nav>

        {/* Ledger form */}
        <div className="min-w-0 flex-1">
          <p className="font-mono text-[11px] uppercase tracking-[0.06em] text-moon-600">
            Project · Settings
          </p>
          <h2 className="mt-1 font-display text-lg font-semibold tracking-tight text-moon-100">
            Every project-scoped setting, in the project&apos;s own home
          </h2>
          <div className="mt-3 rounded-card border border-azure/25 bg-azure/[0.06] px-3.5 py-2.5 text-[13px] text-azure">
            Project settings now live on the project. Global{" "}
            <Link to="/settings/$section" params={{ section: "projects" }} className="underline underline-offset-2">
              Settings → Projects
            </Link>{" "}
            is a thin list that deep-links here.
          </div>

          {/* 1. IDENTITY */}
          <Section
            id="identity"
            index={1}
            title="Identity"
            collapsed={collapsed.identity}
            onToggle={() => toggleSection("identity")}
            dirty={identityDirty}
          >
            <Row label="Name" dirty={form.name !== server.name}>
              <Input
                value={form.name}
                onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                className="max-w-[340px]"
              />
            </Row>
            <Row label="Slug" hint="URL-safe identifier. Auto-derived if blank." dirty={form.slug !== server.slug}>
              <Input
                mono
                value={form.slug}
                onChange={(e) => setForm((f) => ({ ...f, slug: e.target.value }))}
                className="max-w-[340px]"
              />
            </Row>
            <Row label="Color" dirty={(form.color ?? null) !== (server.color ?? null)}>
              <ColorPicker
                value={form.color}
                onChange={(hex) => setForm((f) => ({ ...f, color: hex }))}
              />
            </Row>
            <Row label="Source path" hint="The repository or directory tickets run against." dirty={form.source_path !== server.source_path}>
              <PathInput
                value={form.source_path}
                onChange={(v) => setForm((f) => ({ ...f, source_path: v }))}
                invalid={!form.source_path.trim()}
              />
            </Row>
            <Row
              label="Archived"
              hint={archived ? "Hidden from the strip, sidebar, and pickers." : "Hide this project everywhere."}
            >
              <ArchiveToggle
                archived={archived}
                onArchive={() => setConfirmArchive(true)}
              />
            </Row>
          </Section>

          {/* 2. EXECUTION DEFAULTS */}
          <Section
            id="execution"
            index={2}
            title="Execution defaults"
            hint="Tickets inherit these unless overridden per ticket."
            collapsed={collapsed.execution}
            onToggle={() => toggleSection("execution")}
            dirty={execDirty}
          >
            <Row label="Default profile">
              <ProvenanceNote>
                Chosen per ticket.{" "}
                <Link to="/settings/$section" params={{ section: "profiles" }} className="text-azure hover:underline">
                  Manage profiles ↗
                </Link>
              </ProvenanceNote>
            </Row>
            <Row
              label="Toolsets"
              hint="Toolchain preset names applied to this project's runs."
              dirty={listsDiffer(form.default_toolchains, server.default_toolchains)}
            >
              <ListEditor
                value={form.default_toolchains}
                onChange={(v) => setForm((f) => ({ ...f, default_toolchains: v }))}
                placeholder="rust-user-tools"
                emptyHint="No default toolsets."
              />
            </Row>
            <Row
              label="Workspace mode"
              dirty={form.default_workspace_mode !== server.default_workspace_mode}
            >
              <SegmentedMode
                value={form.default_workspace_mode}
                onChange={(m) => setForm((f) => ({ ...f, default_workspace_mode: m }))}
              />
            </Row>
            <Row
              label="Base ref"
              hint="Branch worktrees branch from."
              dirty={form.default_base_ref !== server.default_base_ref}
            >
              <Input
                mono
                value={form.default_base_ref}
                placeholder="main"
                onChange={(e) => setForm((f) => ({ ...f, default_base_ref: e.target.value }))}
                className="max-w-[260px]"
              />
            </Row>
            {form.default_workspace_mode === "git_worktree" && (
              <Row
                label="Worktree name template"
                hint="Optional. Blank auto-generates from the ticket title."
                dirty={form.default_worktree_name_template !== server.default_worktree_name_template}
              >
                <Input
                  mono
                  value={form.default_worktree_name_template}
                  placeholder="{project}/{slug}"
                  onChange={(e) => setForm((f) => ({ ...f, default_worktree_name_template: e.target.value }))}
                  className="max-w-[340px]"
                />
              </Row>
            )}
            <Row
              label="Tool paths"
              hint="Extra directories added to PATH in the sandbox."
              dirty={listsDiffer(form.default_tool_paths, server.default_tool_paths)}
            >
              <ListEditor
                value={form.default_tool_paths}
                onChange={(v) => setForm((f) => ({ ...f, default_tool_paths: v }))}
                placeholder="/home/you/tools"
                emptyHint="No default tool paths."
              />
            </Row>
            <Row label="Commit on finish">
              <ProvenanceNote>Set per ticket (opt in on the ticket composer).</ProvenanceNote>
            </Row>
            <p className="mt-2 px-1 text-[11.5px] text-moon-600">
              Effective config for any run is shown on its ticket — these are the project-wide
              defaults new tickets inherit.
            </p>
          </Section>

          {/* 3. REPO LINKS */}
          <Section
            id="repo"
            index={3}
            title="Repo links"
            hint="Attached repos surface their issues and MRs on this project's tickets."
            collapsed={collapsed.repo}
            onToggle={() => toggleSection("repo")}
          >
            <RepoLinksSection projectId={project.id} />
          </Section>

          {/* 4. LABELS & DEFAULTS */}
          <Section
            id="labels"
            index={4}
            title="Labels & defaults"
            collapsed={collapsed.labels}
            onToggle={() => toggleSection("labels")}
          >
            <LabelsDefaultsSection />
          </Section>

          {/* 5. AUTOMATION */}
          <Section
            id="automation"
            index={5}
            title="Automation"
            hint="Recurring jobs that create tickets."
            collapsed={collapsed.automation}
            onToggle={() => toggleSection("automation")}
          >
            <div className="px-1 py-1">
              <p className="text-[13px] text-moon-400">
                Cron jobs aren&apos;t scoped to a single project yet — they run against a source path
                and profile of their own. Manage them in one place:
              </p>
              <Link
                to="/scheduled"
                className="mt-2 inline-flex items-center gap-1.5 text-[13px] text-azure hover:underline"
              >
                Open Scheduled <ExternalLink size={12} />
              </Link>
            </div>
          </Section>

          {/* 6. DANGER */}
          <Section
            id="danger"
            index={6}
            title="Danger"
            collapsed={collapsed.danger}
            onToggle={() => toggleSection("danger")}
          >
            <DangerRow
              title="Archive project"
              description={
                archived
                  ? "This project is already archived."
                  : "Hides it from the strip, sidebar, and pickers. Tickets keep their full history and stay searchable. Archiving is permanent — there's no restore yet."
              }
            >
              {archived ? (
                <span className="text-xs text-moon-600">Already archived</span>
              ) : (
                <Button variant="danger" size="sm" onClick={() => setConfirmArchive(true)}>
                  Archive
                </Button>
              )}
            </DangerRow>
            <DangerRow
              title="Delete project"
              description="Permanent deletion isn't available yet — archiving is the destructive action, and it keeps the project's history searchable."
            >
              <Tooltip content="Permanent deletion isn't supported yet.">
                <span>
                  <Button variant="danger" size="sm" disabled>
                    Delete
                  </Button>
                </span>
              </Tooltip>
            </DangerRow>
          </Section>
        </div>
      </div>

      <SaveBar dirty={dirty} saving={saving} onSave={save} onDiscard={discard} error={error} />

      <ConfirmDialog
        open={confirmArchive}
        onOpenChange={(o) => !o && setConfirmArchive(false)}
        title={`Archive “${project.name}”?`}
        body="The project is hidden from the strip, sidebar, and pickers. Its tickets keep their history and stay searchable. Archiving is permanent — there's no restore yet."
        confirmLabel="Archive project"
        danger
        busy={archiving}
        onConfirm={archive}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Form shape + helpers
// ---------------------------------------------------------------------------

type WorkspaceMode = "directory" | "git_worktree" | "in_place";

interface ProjectForm {
  name: string;
  slug: string;
  color: string | null;
  source_path: string;
  default_workspace_mode: WorkspaceMode;
  default_base_ref: string;
  default_worktree_name_template: string;
  default_toolchains: string[];
  default_tool_paths: string[];
}

function buildForm(p: ProjectOut): ProjectForm {
  const raw = (p.default_workspace_mode ?? "directory") as WorkspaceKind;
  const mode: WorkspaceMode = raw === "git_worktree" || raw === "worktree" ? "git_worktree" : raw === "in_place" ? "in_place" : "directory";
  return {
    name: p.name,
    slug: p.slug,
    color: p.color ?? null,
    source_path: p.source_path,
    default_workspace_mode: mode,
    default_base_ref: p.default_base_ref ?? "",
    default_worktree_name_template: p.default_worktree_name_template ?? "",
    default_toolchains: p.default_toolchains ?? [],
    default_tool_paths: p.default_tool_paths ?? [],
  };
}

function listsDiffer(a: string[], b: string[]): boolean {
  return a.length !== b.length || a.some((v, i) => v !== b[i]);
}

// ---------------------------------------------------------------------------
// Section rail + collapsible section primitives
// ---------------------------------------------------------------------------

type SectionId = "identity" | "execution" | "repo" | "labels" | "automation" | "danger";

const SECTIONS: { id: SectionId; label: string }[] = [
  { id: "identity", label: "Identity" },
  { id: "execution", label: "Execution defaults" },
  { id: "repo", label: "Repo links" },
  { id: "labels", label: "Labels & defaults" },
  { id: "automation", label: "Automation" },
  { id: "danger", label: "Danger" },
];

const SECTION_IDS = SECTIONS.map((s) => s.id);

/** Scroll-spy over the project tab's scroll container (ProjectPage's
 *  overflow-y-auto wrapper, found by walking up from this tab's root).
 *  Highlights the rail item for the section whose header sits nearest the top.
 *  Uses IntersectionObserver rooted at the scroll container with a bottom
 *  rootMargin so a section becomes active as it crosses the upper quarter. */
function useScrollSpy(ids: string[], rootRef: RefObject<HTMLElement | null>) {
  const [active, setActive] = useState<string>(ids[0] ?? "");
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const scroller = findScroller(root);
    if (!scroller) return;
    const sections = ids
      .map((id) => document.getElementById(`pset-${id}`))
      .filter((el): el is HTMLElement => !!el);
    if (sections.length === 0) return;

    const io = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible.length > 0) setActive(visible[0].target.id.replace(/^pset-/, ""));
      },
      { root: scroller, rootMargin: "0px 0px -72% 0px", threshold: [0, 1] },
    );
    sections.forEach((s) => io.observe(s));
    return () => io.disconnect();
    // ids is a module-stable constant; rootRef is stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rootRef, ids.join(",")]);
  return active;
}

function findScroller(el: HTMLElement | null): HTMLElement | null {
  let node = el?.parentElement ?? null;
  while (node) {
    const ov = getComputedStyle(node).overflowY;
    if (ov === "auto" || ov === "scroll") return node;
    node = node.parentElement;
  }
  return null;
}

function Section({
  id,
  index,
  title,
  hint,
  collapsed,
  onToggle,
  dirty,
  children,
}: {
  id: SectionId;
  index: number;
  title: string;
  hint?: string;
  collapsed: boolean;
  onToggle: () => void;
  dirty?: boolean;
  children: React.ReactNode;
}) {
  return (
    <section
      id={`pset-${id}`}
      className={cn("scroll-mt-4 border-b border-ink-700/60 last:border-b-0", collapsed && "pb-2")}
    >
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={!collapsed}
        className="flex w-full items-baseline gap-2.5 py-3 text-left"
      >
        <span className="font-mono text-[11px] text-moon-600">{index}</span>
        <span className="text-[11.5px] font-bold uppercase tracking-[0.07em] text-moon-400">
          {title}
        </span>
        {dirty && (
          <span className="rounded-sm bg-warn/15 px-1.5 py-0.5 text-[9.5px] font-bold uppercase tracking-wide text-warn">
            edited
          </span>
        )}
        {hint && <span className="hidden text-[11.5px] text-moon-600 sm:inline">{hint}</span>}
        <ChevronDown
          size={14}
          className={cn("ml-auto text-moon-600 transition-transform", collapsed && "-rotate-90")}
        />
      </button>
      {!collapsed && <div className="pb-4">{children}</div>}
    </section>
  );
}

/** Label-left / control-right ledger row. `dirty` paints the amber edge + chip. */
function Row({
  label,
  hint,
  dirty,
  children,
}: {
  label: string;
  hint?: string;
  dirty?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div
      className={cn(
        "grid grid-cols-1 gap-1.5 border-t border-ink-800/70 px-1 py-3 first:border-t-0 sm:grid-cols-[14rem_minmax(0,1fr)] sm:items-center sm:gap-6",
        dirty && "border-t-0 -mx-2 border-l-2 border-l-warn bg-warn/[0.06] px-3",
      )}
    >
      <div className="flex items-center gap-2">
        <span className="text-[13px] text-moon-400">{label}</span>
        {dirty && (
          <span className="rounded-sm bg-warn/15 px-1.5 py-0.5 text-[9.5px] font-bold uppercase tracking-wide text-warn">
            edited
          </span>
        )}
        {hint && <span className="block text-[11.5px] text-moon-600 sm:hidden">{hint}</span>}
      </div>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

function ProvenanceNote({ children }: { children: React.ReactNode }) {
  return <p className="text-[12.5px] text-moon-600">{children}</p>;
}

function SegmentedMode({
  value,
  onChange,
}: {
  value: WorkspaceMode;
  onChange: (m: WorkspaceMode) => void;
}) {
  const opts: { v: WorkspaceMode; label: string }[] = [
    { v: "directory", label: "Directory" },
    { v: "git_worktree", label: "Git worktree" },
    { v: "in_place", label: "In place" },
  ];
  return (
    <div className="inline-flex overflow-hidden rounded-control border border-ink-700">
      {opts.map((o, i) => (
        <button
          key={o.v}
          type="button"
          onClick={() => onChange(o.v)}
          aria-pressed={value === o.v}
          className={cn(
            "px-3 py-1.5 text-xs transition-colors",
            i > 0 && "border-l border-ink-700",
            value === o.v
              ? "bg-lamp text-ink-950 font-semibold"
              : "bg-ink-900 text-moon-600 hover:text-moon-100",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** The archived control. Archiving round-trips (DELETE → archive); restoring
 *  has no endpoint, so the switch is one-way: it triggers the archive confirm
 *  when flipped on, and is disabled once archived with an honest tooltip. */
function ArchiveToggle({ archived, onArchive }: { archived: boolean; onArchive: () => void }) {
  if (archived) {
    return (
      <div className="flex items-center gap-2.5">
        <Tooltip content="Archived projects can't be restored from this view yet.">
          <span>
            <Switch checked onChange={() => {}} disabled />
          </span>
        </Tooltip>
        <span className="text-[12px] text-moon-600">on — restoration not available</span>
      </div>
    );
  }
  return (
    <div className="flex items-center gap-2.5">
      <Switch checked={false} onChange={(on) => on && onArchive()} />
      <span className="text-[12px] text-moon-600">off</span>
    </div>
  );
}

function DangerRow({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-center gap-4 border-t border-ink-800/70 py-3.5 first:border-t-0">
      <div className="min-w-0 flex-1">
        <div className="text-[13px] font-semibold text-moon-100">{title}</div>
        <p className="mt-0.5 text-[11.5px] leading-relaxed text-moon-600">{description}</p>
      </div>
      <div className="shrink-0">{children}</div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Repo links — attach/detach with connection health (reuses the toggle API
// that merges rather than replaces, so concurrent toggles stay consistent).
// ---------------------------------------------------------------------------

function RepoLinksSection({ projectId }: { projectId: string }) {
  const connections = useConnections();
  const attachedQuery = useProjectRepoLinks(projectId);
  const allRepoLinks = useRepoLinks();
  const suggest = useRepoSuggest(projectId);
  const toggle = useToggleProjectRepoLink();
  const [pendingIds, setPendingIds] = useState<Set<string>>(new Set());

  const attached = attachedQuery.data ?? [];
  const attachedIds = new Set(attached.map((r) => r.id));
  const available = (allRepoLinks.data ?? []).filter((r) => !attachedIds.has(r.id));
  const noConnections = (connections.data ?? []).length === 0;
  const connById = new Map((connections.data ?? []).map((c) => [c.id, c]));

  const suggestedRepo: RepoLinkOut | null =
    suggest.data?.matched_repo_link_id
      ? (allRepoLinks.data ?? []).find((r) => r.id === suggest.data?.matched_repo_link_id) ?? null
      : null;
  const showSuggestion = !!suggestedRepo && !attachedIds.has(suggestedRepo.id);

  async function toggleLink(repoLinkId: string, attach: boolean) {
    setPendingIds((prev) => new Set(prev).add(repoLinkId));
    try {
      await toggle.mutateAsync({ projectId, repoLinkId, attach });
      if (attach) toast.success("Repository attached");
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not update repository link");
    } finally {
      setPendingIds((prev) => {
        const next = new Set(prev);
        next.delete(repoLinkId);
        return next;
      });
    }
  }

  if (attachedQuery.isLoading) {
    return (
      <div className="flex items-center gap-2 text-xs text-moon-400">
        <Spinner size={12} /> Loading repo links…
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {showSuggestion && suggestedRepo && (
        <div className="flex items-center justify-between gap-2 rounded-control border border-lamp/30 bg-lamp/10 px-2.5 py-2">
          <span className="min-w-0 truncate text-xs text-moon-100">
            Detected <span className="font-mono text-[11px] text-moon-400">{suggest.data?.git_remote_url}</span> —
            attach {suggestedRepo.display_name || suggestedRepo.external_path}?
          </span>
          <Button
            size="sm"
            variant="ghost"
            className="shrink-0"
            loading={pendingIds.has(suggestedRepo.id)}
            onClick={() => toggleLink(suggestedRepo.id, true)}
          >
            Attach
          </Button>
        </div>
      )}

      {attached.length === 0 ? (
        <p className="text-xs text-moon-600">No repositories linked to this project.</p>
      ) : (
        <ul className="space-y-1.5">
          {attached.map((r) => {
            const conn = connById.get(r.connection_id);
            return (
              <li
                key={r.id}
                className="flex items-center gap-3 rounded-control border border-ink-700 bg-ink-950/40 px-2.5 py-2"
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate font-mono text-[12px] text-moon-100">
                    {r.display_name || r.external_path}
                  </div>
                  <div className="truncate font-mono text-[10px] text-moon-600">{r.external_path}</div>
                </div>
                {conn && <ConnectionHealthPill status={conn.status} />}
                <button
                  type="button"
                  onClick={() => toggleLink(r.id, false)}
                  disabled={pendingIds.has(r.id)}
                  className="shrink-0 text-moon-600 hover:text-failed disabled:opacity-50"
                  aria-label={`Detach ${r.external_path}`}
                >
                  {pendingIds.has(r.id) ? <Spinner size={12} /> : <X size={13} />}
                </button>
              </li>
            );
          })}
        </ul>
      )}

      {noConnections ? (
        <p className="text-xs text-moon-600">
          No connections yet.{" "}
          <Link
            to="/settings/$section"
            params={{ section: "connections" }}
            className="text-lamp hover:underline"
          >
            Add one in Connections
          </Link>{" "}
          to attach repositories.
        </p>
      ) : (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              leadingIcon={<Plus size={13} />}
              disabled={available.length === 0 && !allRepoLinks.isLoading}
            >
              Attach repository
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start">
            <DropdownMenuLabel>Available repositories</DropdownMenuLabel>
            {allRepoLinks.isLoading ? (
              <div className="px-2 py-1.5 text-xs text-moon-600">Loading…</div>
            ) : available.length === 0 ? (
              <div className="px-2 py-1.5 text-xs text-moon-600">All repositories already attached.</div>
            ) : (
              available.map((r) => (
                <DropdownMenuItem
                  key={r.id}
                  keepOpen
                  disabled={pendingIds.has(r.id)}
                  onSelect={() => toggleLink(r.id, true)}
                >
                  <span className="flex-1 truncate">{r.display_name || r.external_path}</span>
                  {pendingIds.has(r.id) && <Spinner size={12} />}
                </DropdownMenuItem>
              ))
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  );
}

function ConnectionHealthPill({ status }: { status: string }) {
  if (status === "ok")
    return (
      <Badge tone="success" className="shrink-0">
        connection ok
      </Badge>
    );
  if (status === "auth_failed" || status === "unreachable")
    return (
      <Badge tone="failed" className="shrink-0">
        {status.replace("_", " ")}
      </Badge>
    );
  return (
    <Badge tone="neutral" className="shrink-0">
      unchecked
    </Badge>
  );
}

// ---------------------------------------------------------------------------
// Labels & defaults — default-labels-on-create has no project field yet, so
// the section is an honest provenance note + a deep link to instance labels.
// ---------------------------------------------------------------------------

function LabelsDefaultsSection() {
  const labels = useLabels();
  const count = labels.data?.length ?? null;
  return (
    <div className="space-y-2 px-1 py-1">
      <p className="text-[13px] text-moon-400">
        Default labels applied to new tickets aren&apos;t project-scoped yet — labels are set per
        ticket at create time. The label set itself is instance-wide.
      </p>
      <Link
        to="/settings/$section"
        params={{ section: "labels" }}
        className="inline-flex items-center gap-1.5 text-[13px] text-azure hover:underline"
      >
        Manage labels{count != null ? ` (${count})` : ""} <ExternalLink size={12} />
      </Link>
    </div>
  );
}
