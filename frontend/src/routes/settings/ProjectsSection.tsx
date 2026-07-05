import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Archive, Layers, Pencil, Plus } from "lucide-react";
import { Button } from "@/ui/Button";
import { IconButton } from "@/ui/IconButton";
import { Input, Field } from "@/ui/Input";
import { Select } from "@/ui/Select";
import { Dialog } from "@/ui/Dialog";
import { Spinner } from "@/ui/Spinner";
import { EmptyState } from "@/ui/EmptyState";
import { Tooltip } from "@/ui/Tooltip";
import { toast } from "@/ui/Toast";
import { ApiError } from "@/api/client";
import { projectsApi, useProjects } from "@/api/projects";
import { useProjectActivity } from "@/api/projectActivity";
import { qk } from "@/api/keys";
import { PathInput } from "@/components/PathInput";
import type { ProjectCreate, ProjectOut, WorkspaceKind } from "@/api/types";
import { relativeTime, formatDuration } from "@/lib/time";
import { cn } from "@/lib/cn";
import { SectionHeader } from "./parts/SettingsSection";
import { ColorPicker, SWATCHES } from "./parts/ColorPicker";
import { ListEditor } from "./parts/ListEditor";
import { ConfirmDialog } from "./parts/ConfirmDialog";

const WORKSPACE_MODES: { value: WorkspaceKind; label: string }[] = [
  { value: "directory", label: "Directory (work in place)" },
  { value: "git_worktree", label: "Git worktree (isolated branch)" },
  { value: "in_place", label: "In place" },
];

export function ProjectsSection() {
  const qc = useQueryClient();
  const projects = useProjects();
  const [editing, setEditing] = useState<ProjectOut | "new" | null>(null);
  const [toArchive, setToArchive] = useState<ProjectOut | null>(null);
  const [busy, setBusy] = useState(false);

  const invalidate = () => qc.invalidateQueries({ queryKey: qk.projects.all });

  async function archive() {
    if (!toArchive) return;
    setBusy(true);
    try {
      await projectsApi.remove(toArchive.id);
      invalidate();
      toast.success(`Archived “${toArchive.name}”`);
      setToArchive(null);
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not archive project");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <SectionHeader
        title="Projects"
        description="Repositories you run tickets against, with default workspace behavior."
        actions={
          <Button variant="primary" size="sm" leadingIcon={<Plus size={14} />} onClick={() => setEditing("new")}>
            New project
          </Button>
        }
      />

      {projects.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-moon-400">
          <Spinner /> Loading projects…
        </div>
      ) : projects.data && projects.data.length > 0 ? (
        <ul className="grid gap-3 xl:grid-cols-2">
          {projects.data.map((p) => (
            <li key={p.id} className="rounded-card border border-ink-700 bg-ink-900 px-4 py-3.5">
              <div className="flex items-start gap-3">
                <span
                  className="mt-1 h-3 w-3 shrink-0 rounded-full"
                  style={{ backgroundColor: p.color ?? "#66748c" }}
                />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-display text-sm font-semibold text-moon-100">{p.name}</span>
                    <span className="font-mono text-xs text-moon-600">{p.slug}</span>
                  </div>
                  <div className="mt-0.5 truncate font-mono text-xs text-moon-400">{p.source_path}</div>
                  <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-moon-600">
                    <span>{WORKSPACE_MODES.find((m) => m.value === p.default_workspace_mode)?.label ?? "no default mode"}</span>
                    {p.default_base_ref && <span className="font-mono">base {p.default_base_ref}</span>}
                    {p.default_toolchains.length > 0 && <span>{p.default_toolchains.length} toolchain(s)</span>}
                  </div>
                  <ActivityStrip projectId={p.id} />
                </div>
                <div className="flex shrink-0 items-center gap-1">
                  <IconButton label="Edit" size="sm" icon={<Pencil size={14} />} onClick={() => setEditing(p)} />
                  <IconButton
                    label="Archive"
                    size="sm"
                    icon={<Archive size={14} />}
                    onClick={() => setToArchive(p)}
                    className="hover:text-failed"
                  />
                </div>
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <EmptyState
          icon={<Layers size={18} />}
          title="No projects"
          description="Add a project to give tickets a default source path and workspace behavior."
          action={
            <Button variant="ghost" leadingIcon={<Plus size={14} />} onClick={() => setEditing("new")}>
              New project
            </Button>
          }
        />
      )}

      {editing && (
        <ProjectDialog
          project={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            invalidate();
            setEditing(null);
          }}
        />
      )}

      <ConfirmDialog
        open={!!toArchive}
        onOpenChange={(o) => !o && setToArchive(null)}
        title={`Archive “${toArchive?.name}”?`}
        body="The project is hidden from pickers and filters. Its tickets keep their history and stay searchable."
        confirmLabel="Archive project"
        danger
        busy={busy}
        onConfirm={archive}
      />
    </div>
  );
}

function outcomeColor(outcome: string): string {
  const o = outcome.toLowerCase();
  if (["success", "completed", "ok", "clean"].includes(o)) return "bg-success";
  if (o === "running") return "bg-lamp motion-safe:animate-pulse";
  if (["failed", "error", "crashed"].includes(o)) return "bg-failed";
  return "bg-moon-600";
}

function ActivityStrip({ projectId }: { projectId: string }) {
  const activity = useProjectActivity(projectId);
  if (activity.isLoading) return null;
  const rows = activity.data ?? [];
  if (rows.length === 0) {
    return <div className="mt-2 text-[11px] text-moon-600">No runs yet</div>;
  }
  return (
    <div className="mt-2 flex items-center gap-1.5">
      {rows.slice(0, 16).map((r) => (
        <Tooltip
          key={r.run_id}
          content={
            <span className="font-mono text-[11px]">
              {r.ticket_title} · {r.outcome}
              {r.duration_seconds != null ? ` · ${formatDuration(r.duration_seconds * 1000)}` : ""}
              {r.tokens != null ? ` · ${r.tokens.toLocaleString()} tok` : ""}
              {r.started_at ? ` · ${relativeTime(r.started_at)}` : ""}
            </span>
          }
        >
          <span className={cn("h-2 w-2 rounded-full", outcomeColor(r.outcome))} />
        </Tooltip>
      ))}
      <span className="ml-1 text-[11px] text-moon-600">last {Math.min(rows.length, 16)} runs</span>
    </div>
  );
}

function ProjectDialog({
  project,
  onClose,
  onSaved,
}: {
  project: ProjectOut | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(project?.name ?? "");
  const [slug, setSlug] = useState(project?.slug ?? "");
  const [sourcePath, setSourcePath] = useState(project?.source_path ?? "");
  const [color, setColor] = useState(project?.color ?? SWATCHES[0]);
  const [mode, setMode] = useState<WorkspaceKind>(
    (project?.default_workspace_mode as WorkspaceKind) ?? "directory",
  );
  const [baseRef, setBaseRef] = useState(project?.default_base_ref ?? "");
  const [wtTemplate, setWtTemplate] = useState(project?.default_worktree_name_template ?? "");
  const [toolchains, setToolchains] = useState<string[]>(project?.default_toolchains ?? []);
  const [toolPaths, setToolPaths] = useState<string[]>(project?.default_tool_paths ?? []);
  const [busy, setBusy] = useState(false);

  const canSave = name.trim() && sourcePath.trim();

  async function save() {
    if (!canSave) return;
    setBusy(true);
    try {
      const body: ProjectCreate = {
        name: name.trim(),
        slug: slug.trim() || null,
        source_path: sourcePath.trim(),
        color,
        default_workspace_mode: mode,
        default_base_ref: baseRef.trim() || null,
        default_worktree_name_template: wtTemplate.trim() || null,
        default_toolchains: toolchains,
        default_tool_paths: toolPaths,
      };
      if (project) {
        await projectsApi.update(project.id, body);
      } else {
        await projectsApi.create(body);
      }
      toast.success(project ? "Project updated" : "Project created");
      onSaved();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not save project");
    } finally {
      setBusy(false);
    }
  }

  const isWorktree = mode === "git_worktree";

  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={project ? "Edit project" : "New project"}
      size="lg"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" loading={busy} disabled={!canSave} onClick={save}>
            {project ? "Save changes" : "Create project"}
          </Button>
        </>
      }
    >
      <div className="max-h-[65vh] space-y-4 overflow-y-auto pr-1">
        <div className="grid grid-cols-[1fr_160px] gap-3">
          <Field label="Name">
            <Input autoFocus placeholder="nightdesk" value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="Slug" hint="Auto if blank">
            <Input mono placeholder="nightdesk" value={slug} onChange={(e) => setSlug(e.target.value)} />
          </Field>
        </div>

        <Field label="Source path" hint="The repository or directory tickets run against.">
          <PathInput value={sourcePath} onChange={setSourcePath} invalid={!sourcePath.trim()} />
        </Field>

        <Field label="Color">
          <ColorPicker value={color} onChange={setColor} />
        </Field>

        <div className="grid grid-cols-2 gap-3">
          <Field label="Default workspace mode">
            <Select value={mode} onChange={(e) => setMode(e.target.value as WorkspaceKind)}>
              {WORKSPACE_MODES.map((m) => (
                <option key={m.value} value={m.value}>
                  {m.label}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Default base ref" hint="For worktree tickets.">
            <Input mono placeholder="main" value={baseRef} onChange={(e) => setBaseRef(e.target.value)} />
          </Field>
        </div>

        {isWorktree && (
          <Field
            label="Worktree name template"
            hint="Optional. Blank auto-generates from the ticket title."
          >
            <Input mono placeholder="{project}/{slug}" value={wtTemplate} onChange={(e) => setWtTemplate(e.target.value)} />
          </Field>
        )}

        <Field label="Default toolchains" hint="Toolset preset names applied to this project's runs.">
          <ListEditor value={toolchains} onChange={setToolchains} placeholder="rust-user-tools" emptyHint="None." />
        </Field>

        <Field label="Default tool paths" hint="Extra directories added to PATH in the sandbox.">
          <ListEditor value={toolPaths} onChange={setToolPaths} placeholder="/home/you/tools" emptyHint="None." />
        </Field>

        {project?.default_linked_workspaces && project.default_linked_workspaces.length > 0 && (
          <div className="rounded-control border border-ink-700 bg-ink-950/40 px-3 py-2">
            <p className="text-xs text-moon-400">
              {project.default_linked_workspaces.length} linked workspace default(s) are preserved.
              Edit them from the ticket composer.
            </p>
          </div>
        )}
      </div>
    </Dialog>
  );
}
