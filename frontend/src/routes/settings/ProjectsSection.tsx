import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { ArrowUpRight, Layers, Plus } from "lucide-react";
import { Button } from "@/ui/Button";
import { Input, Field } from "@/ui/Input";
import { Select } from "@/ui/Select";
import { Dialog } from "@/ui/Dialog";
import { Spinner } from "@/ui/Spinner";
import { EmptyState } from "@/ui/EmptyState";
import { Tooltip } from "@/ui/Tooltip";
import { toast } from "@/ui/Toast";
import { projectsApi, useProjects } from "@/api/projects";
import { qk } from "@/api/keys";
import { PathInput } from "@/components/PathInput";
import type { ProjectCreate, WorkspaceKind } from "@/api/types";
import { SectionHeader } from "./parts/SettingsSection";
import { ColorPicker, SWATCHES } from "./parts/ColorPicker";
import { ListEditor } from "./parts/ListEditor";

const WORKSPACE_MODES: { value: WorkspaceKind; label: string }[] = [
  { value: "directory", label: "Directory (work in place)" },
  { value: "git_worktree", label: "Git worktree (isolated branch)" },
  { value: "in_place", label: "In place" },
];

/**
 * Global Settings → Projects.
 *
 * Project *settings* (identity, execution defaults, repo links, danger) now
 * live on each project's own Settings tab (docs/design/project-control-plane.md
 * §Settings). This section is the thin index the design calls for: one row per
 * project (color dot, name, source path) that deep-links to that project's
 * Settings tab. Creation stays here — it's the only entry point for new
 * projects — but every per-project editor has moved off this page.
 */
export function ProjectsSection() {
  const qc = useQueryClient();
  const projects = useProjects();
  const [creating, setCreating] = useState(false);

  const invalidate = () => qc.invalidateQueries({ queryKey: qk.projects.all });

  return (
    <div>
      <SectionHeader
        title="Projects"
        description="Repositories you run tickets against. Each project's settings live on its own Settings tab now."
        actions={
          <Button variant="primary" size="sm" leadingIcon={<Plus size={14} />} onClick={() => setCreating(true)}>
            New project
          </Button>
        }
      />

      <p className="mb-4 text-sm text-moon-600">
        Project settings moved. Open a project to edit its identity, defaults, repo links, and more.
      </p>

      {projects.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-moon-400">
          <Spinner /> Loading projects…
        </div>
      ) : projects.data && projects.data.length > 0 ? (
        <ul className="divide-y divide-ink-800/70 overflow-hidden rounded-card border border-ink-700">
          {projects.data.map((p) => (
            <li key={p.id}>
              <Link
                to="/projects/$id"
                params={{ id: p.id }}
                search={{ tab: "settings" }}
                className="group flex items-center gap-3 bg-ink-900 px-4 py-3 transition-colors hover:bg-ink-800"
              >
                <span
                  className="h-3 w-3 shrink-0 rounded-full"
                  style={{ backgroundColor: p.color ?? "#66748c" }}
                />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-display text-sm font-semibold text-moon-100">{p.name}</span>
                    <span className="font-mono text-xs text-moon-600">{p.slug}</span>
                    {p.archived_at && (
                      <span className="rounded-full border border-ink-700 bg-ink-800 px-1.5 py-0.5 text-[10px] text-moon-600">
                        Archived
                      </span>
                    )}
                  </div>
                  <Tooltip content={p.source_path} mono>
                    <div className="mt-0.5 truncate font-mono text-xs text-moon-400">{p.source_path}</div>
                  </Tooltip>
                </div>
                <ArrowUpRight
                  size={15}
                  className="shrink-0 text-moon-600 transition-colors group-hover:text-lamp"
                />
              </Link>
            </li>
          ))}
        </ul>
      ) : (
        <EmptyState
          icon={<Layers size={18} />}
          title="No projects"
          description="Add a project to give tickets a default source path and workspace behavior."
          action={
            <Button variant="ghost" leadingIcon={<Plus size={14} />} onClick={() => setCreating(true)}>
              New project
            </Button>
          }
        />
      )}

      {creating && (
        <ProjectDialog
          onClose={() => setCreating(false)}
          onSaved={() => {
            invalidate();
            setCreating(false);
          }}
        />
      )}
    </div>
  );
}

function ProjectDialog({
  onClose,
  onSaved,
}: {
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [sourcePath, setSourcePath] = useState("");
  const [color, setColor] = useState(SWATCHES[0]);
  const [mode, setMode] = useState<WorkspaceKind>("directory");
  const [baseRef, setBaseRef] = useState("");
  const [wtTemplate, setWtTemplate] = useState("");
  const [toolchains, setToolchains] = useState<string[]>([]);
  const [toolPaths, setToolPaths] = useState<string[]>([]);
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
      await projectsApi.create(body);
      toast.success("Project created");
      onSaved();
    } catch (err) {
      toast.error("Could not create project", { error: err });
    } finally {
      setBusy(false);
    }
  }

  const isWorktree = mode === "git_worktree";

  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="New project"
      size="lg"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" loading={busy} disabled={!canSave} onClick={save}>
            Create project
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
          <Field label="Worktree name template" hint="Optional. Blank auto-generates from the ticket title.">
            <Input mono placeholder="{project}/{slug}" value={wtTemplate} onChange={(e) => setWtTemplate(e.target.value)} />
          </Field>
        )}

        <Field label="Default toolchains" hint="Toolset preset names applied to this project's runs.">
          <ListEditor value={toolchains} onChange={setToolchains} placeholder="rust-user-tools" emptyHint="None." />
        </Field>

        <Field label="Default tool paths" hint="Extra directories added to PATH in the sandbox.">
          <ListEditor value={toolPaths} onChange={setToolPaths} placeholder="/home/you/tools" emptyHint="None." />
        </Field>

        <p className="text-xs text-moon-600">
          You can fine-tune every setting — including linked repositories — on the project&apos;s
          Settings tab after creating it.
        </p>
      </div>
    </Dialog>
  );
}
