import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Plus, Sparkles, Trash2 } from "lucide-react";
import { Button } from "@/ui/Button";
import { IconButton } from "@/ui/IconButton";
import { Input, Field } from "@/ui/Input";
import { Spinner } from "@/ui/Spinner";
import { toast, describeError } from "@/ui/Toast";
import { configApi, useConfig } from "@/api/config";
import { qk } from "@/api/keys";
import { SectionHeader, SettingsCard } from "./parts/SettingsSection";
import { SaveBar, useEditableForm } from "./parts/SaveBar";
import { ListEditor } from "./parts/ListEditor";

interface Preset {
  name: string;
  paths: string[];
}
interface ToolsetsForm {
  presets: Preset[];
}

function toPresets(record: Record<string, string[]>): Preset[] {
  return Object.entries(record).map(([name, paths]) => ({ name, paths: [...paths] }));
}

export function ToolsetsSection() {
  const qc = useQueryClient();
  const config = useConfig();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newPath, setNewPath] = useState("");

  const seed = config.data ? { presets: toPresets(config.data.toolchain_presets) } : undefined;
  const key = config.data ? JSON.stringify(config.data.toolchain_presets) : "loading";
  const { form, setForm, dirty, discard, commit } = useEditableForm<ToolsetsForm, ToolsetsForm>(
    seed,
    (s) => s,
    key,
  );

  if (config.isLoading || !form) {
    return (
      <div className="flex items-center gap-2 text-sm text-moon-400">
        <Spinner /> Loading…
      </div>
    );
  }
  if (config.isError) return <p className="text-sm text-failed">Could not load config.</p>;

  function patchPreset(i: number, patch: Partial<Preset>) {
    setForm((f) => ({ ...f, presets: f.presets.map((p, idx) => (idx === i ? { ...p, ...patch } : p)) }));
  }
  function addPreset() {
    if (!newPath.trim()) return;
    setForm((f) => ({ ...f, presets: [...f.presets, { name: newPath.trim(), paths: [] }] }));
    setNewPath("");
  }
  function removePreset(i: number) {
    setForm((f) => ({ ...f, presets: f.presets.filter((_, idx) => idx !== i) }));
  }

  const dupNames = (() => {
    const seen = new Set<string>();
    const dups = new Set<string>();
    for (const p of form.presets) {
      const n = p.name.trim();
      if (seen.has(n)) dups.add(n);
      seen.add(n);
    }
    return dups;
  })();

  async function save() {
    if (!form) return;
    const record: Record<string, string[]> = {};
    for (const p of form.presets) {
      const n = p.name.trim();
      if (!n) {
        setError("Every preset needs a name.");
        return;
      }
      record[n] = p.paths;
    }
    if (dupNames.size) {
      setError("Preset names must be unique.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await configApi.update({ toolchain_presets: record });
      await qc.invalidateQueries({ queryKey: qk.config });
      commit();
      toast.success("Toolsets saved");
    } catch (err) {
      setError(describeError(err));
      toast.error("Could not save toolsets", { error: err });
    } finally {
      setSaving(false);
    }
  }

  return (
    <div>
      <SectionHeader
        title="Toolsets"
        description="Named sets of tool directories added to a sandboxed run's PATH. Profiles and projects reference them by name."
      />

      <div className="space-y-4">
        {form.presets.length === 0 ? (
          <SettingsCard>
            <div className="flex flex-col items-center gap-3 py-6 text-center">
              <div className="flex h-11 w-11 items-center justify-center rounded-card border border-ink-700 bg-ink-800 text-moon-400">
                <Sparkles size={18} />
              </div>
              <div>
                <h3 className="font-display text-sm font-semibold text-moon-100">No toolsets</h3>
                <p className="mt-1 text-sm text-moon-400">
                  Group tool directories into a named preset to reuse across profiles.
                </p>
              </div>
            </div>
          </SettingsCard>
        ) : (
          <div className="grid gap-4 lg:grid-cols-2">
          {form.presets.map((p, i) => (
            <SettingsCard key={i}>
              <div className="space-y-3">
                <div className="flex items-center gap-2">
                  <Input
                    value={p.name}
                    placeholder="preset-name"
                    invalid={!p.name.trim() || dupNames.has(p.name.trim())}
                    onChange={(e) => patchPreset(i, { name: e.target.value })}
                    className="h-8 flex-1 font-medium"
                  />
                  <IconButton
                    label="Remove preset"
                    size="sm"
                    icon={<Trash2 size={14} />}
                    onClick={() => removePreset(i)}
                    className="hover:text-failed"
                  />
                </div>
                <Field label="Tool directories" hint="Absolute paths added to PATH inside the sandbox.">
                  <ListEditor
                    value={p.paths}
                    onChange={(paths) => patchPreset(i, { paths })}
                    placeholder="/home/you/tools/bin"
                    emptyHint="No directories yet."
                  />
                </Field>
              </div>
            </SettingsCard>
          ))}
          </div>
        )}

        <div className="flex items-center gap-2">
          <Input
            value={newPath}
            placeholder="New preset name"
            onChange={(e) => setNewPath(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && (e.preventDefault(), addPreset())}
            className="h-9 max-w-xs"
          />
          <Button variant="ghost" size="sm" leadingIcon={<Plus size={14} />} disabled={!newPath.trim()} onClick={addPreset}>
            Add preset
          </Button>
        </div>
      </div>

      <SaveBar dirty={dirty} saving={saving} onSave={save} onDiscard={discard} error={error} />
    </div>
  );
}
