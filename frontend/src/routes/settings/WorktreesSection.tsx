import { useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { FolderGit2 } from "lucide-react";
import { Input, Field } from "@/ui/Input";
import { Select } from "@/ui/Select";
import { Spinner } from "@/ui/Spinner";
import { Badge } from "@/ui/Badge";
import { toast } from "@/ui/Toast";
import { ApiError } from "@/api/client";
import { configApi, useConfig } from "@/api/config";
import { useProjects } from "@/api/projects";
import { qk } from "@/api/keys";
import { Tooltip } from "@/ui/Tooltip";
import { PathInput } from "@/components/PathInput";
import { worktreePreviewApi, type WorktreeNamePreview } from "@/api/worktreePreview";
import { SectionHeader, SettingsCard, SettingsSplit } from "./parts/SettingsSection";
import { SaveBar, useEditableForm } from "./parts/SaveBar";

interface WtForm {
  worktree_base_ref: string;
}

export function WorktreesSection() {
  const qc = useQueryClient();
  const config = useConfig();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const seed = config.data ? { worktree_base_ref: config.data.worktree_base_ref ?? "" } : undefined;
  const { form, setForm, dirty, discard, commit } = useEditableForm<WtForm, WtForm>(
    seed,
    (s) => s,
    config.data ? `${config.data.worktree_base_ref ?? ""}` : "loading",
  );

  if (config.isLoading || !form) {
    return (
      <div className="flex items-center gap-2 text-sm text-moon-400">
        <Spinner /> Loading…
      </div>
    );
  }
  if (config.isError) return <p className="text-sm text-failed">Could not load config.</p>;

  async function save() {
    if (!form) return;
    setSaving(true);
    setError(null);
    try {
      // Empty string clears the global default (branches from HEAD).
      await configApi.update({ worktree_base_ref: form.worktree_base_ref.trim() });
      await qc.invalidateQueries({ queryKey: qk.config });
      commit();
      toast.success("Worktree defaults saved");
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Could not save";
      setError(msg);
      toast.error(msg);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div>
      <SectionHeader
        title="Worktrees"
        description="Where isolated git worktrees are created, and how their branches are named."
      />

      <SettingsSplit aside={<WorktreePreview />}>
        <SettingsCard title="Defaults">
          <div className="space-y-4">
            <div className="flex items-baseline justify-between gap-4 py-1.5">
              <span className="shrink-0 text-xs text-moon-400">Worktree root</span>
              <Tooltip content={config.data!.worktree_root} mono>
                <span className="min-w-0 flex-1 truncate text-right font-mono text-[13px] text-moon-100">
                  {config.data!.worktree_root}
                </span>
              </Tooltip>
            </div>
            <p className="-mt-2 text-xs text-moon-600">
              Bootstrap-only. Set in <span className="font-mono">~/.config/nightdesk/config.toml</span>{" "}
              before launch — changing it at runtime is not supported.
            </p>
            <Field
              label="Default base ref"
              hint="Branch or SHA new git_worktree tickets branch from. Leave blank to branch from HEAD."
            >
              <Input
                mono
                placeholder="main"
                value={form.worktree_base_ref}
                onChange={(e) => setForm((f) => ({ ...f, worktree_base_ref: e.target.value }))}
              />
            </Field>
          </div>
        </SettingsCard>
      </SettingsSplit>

      <SaveBar dirty={dirty} saving={saving} onSave={save} onDiscard={discard} error={error} />
    </div>
  );
}

/** Live template preview: given a source repo, a worktree name, and a base ref,
 *  resolve the path the worker would create. Debounced against /preview. */
function WorktreePreview() {
  const projects = useProjects();
  const [sourcePath, setSourcePath] = useState("");
  const [name, setName] = useState("");
  const [baseRef, setBaseRef] = useState("");
  const [result, setResult] = useState<WorktreeNamePreview | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const projectOptions = projects.data ?? [];
  // Seed the source path from the first project once loaded, for a useful default.
  useEffect(() => {
    if (!sourcePath && projectOptions.length) setSourcePath(projectOptions[0].source_path);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectOptions.length]);

  const debounceKey = useMemo(
    () => `${sourcePath}|${name}|${baseRef}`,
    [sourcePath, name, baseRef],
  );

  useEffect(() => {
    if (!sourcePath.trim()) {
      setResult(null);
      setErr(null);
      return;
    }
    let cancelled = false;
    setPending(true);
    const t = window.setTimeout(async () => {
      try {
        const res = await worktreePreviewApi.preview({
          source_path: sourcePath.trim(),
          name: name.trim() || null,
          base_ref: baseRef.trim() || null,
        });
        if (!cancelled) {
          setResult(res);
          setErr(null);
        }
      } catch (e) {
        if (!cancelled) {
          setResult(null);
          setErr(e instanceof ApiError ? e.message : "Preview unavailable");
        }
      } finally {
        if (!cancelled) setPending(false);
      }
    }, 350);
    return () => {
      cancelled = true;
      window.clearTimeout(t);
    };
  }, [debounceKey, sourcePath, name, baseRef]);

  return (
    <SettingsCard
      title="Name preview"
      description="See the exact path and branch a worktree ticket would resolve to before you create it."
    >
      <div className="space-y-4">
        <div className="grid grid-cols-[1fr_180px] gap-3">
          <Field label="Source repository">
            {projectOptions.length > 0 ? (
              <Select value={sourcePath} onChange={(e) => setSourcePath(e.target.value)}>
                {projectOptions.map((p) => (
                  <option key={p.id} value={p.source_path}>
                    {p.name} — {p.source_path}
                  </option>
                ))}
                <option value="">Custom path…</option>
              </Select>
            ) : (
              <PathInput value={sourcePath} onChange={setSourcePath} />
            )}
          </Field>
          <Field label="Base ref">
            <Input mono placeholder="HEAD" value={baseRef} onChange={(e) => setBaseRef(e.target.value)} />
          </Field>
        </div>
        {projectOptions.length > 0 && sourcePath === "" && (
          <PathInput value={sourcePath} onChange={setSourcePath} placeholder="/home/you/repo" />
        )}
        <Field label="Worktree name" hint="Blank uses the auto-generated name from the ticket title.">
          <Input mono placeholder="feature/my-branch" value={name} onChange={(e) => setName(e.target.value)} />
        </Field>

        <div className="rounded-card border border-ink-700 bg-ink-950/50 px-3.5 py-3">
          {pending ? (
            <span className="flex items-center gap-2 text-sm text-moon-400">
              <Spinner size={13} /> Resolving…
            </span>
          ) : err ? (
            <span className="text-sm text-failed">{err}</span>
          ) : result ? (
            <div className="space-y-2">
              <div className="flex items-start gap-2">
                <FolderGit2 size={15} className="mt-0.5 shrink-0 text-lamp" />
                <code className="break-all font-mono text-[13px] text-moon-100">{result.path}</code>
              </div>
              <div className="flex flex-wrap items-center gap-2 pl-6 text-xs text-moon-400">
                <span>
                  via <span className="font-mono text-moon-100">{result.source}</span>
                </span>
                {result.base_ref && (
                  <Badge
                    tone={result.base_ref_status === "missing" ? "failed" : "neutral"}
                    mono
                  >
                    base {result.base_ref}
                    {result.base_ref_status ? ` · ${result.base_ref_status}` : ""}
                  </Badge>
                )}
              </div>
            </div>
          ) : (
            <span className="text-sm text-moon-600">Pick a source repository to preview.</span>
          )}
        </div>
      </div>
    </SettingsCard>
  );
}
