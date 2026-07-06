import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Copy, Download, KeyRound, ShieldCheck, Trash2 } from "lucide-react";
import { IconButton } from "@/ui/IconButton";
import { Input, Textarea, Field } from "@/ui/Input";
import { Select } from "@/ui/Select";
import { Badge } from "@/ui/Badge";
import { toast, describeError } from "@/ui/Toast";
import { profilesApi } from "@/api/profiles";
import { profileTransferApi } from "@/api/profileTransfer";
import { qk } from "@/api/keys";
import type { ProfileCreate, ProfileOut } from "@/api/types";
import { SettingsCard } from "./parts/SettingsSection";
import { SaveBar, useEditableForm } from "./parts/SaveBar";
import { ListEditor } from "./parts/ListEditor";
import { KeyValueEditor, type KvPair } from "./parts/KeyValueEditor";
import { ConfirmDialog } from "./parts/ConfirmDialog";

const BACKENDS = [
  { value: "claude_sdk", label: "Claude Code" },
  { value: "omp_rpc", label: "OMP / RPC" },
];
const PERMISSION_MODES = [
  { value: "", label: "Inherit (prompt)" },
  { value: "default", label: "Default (prompt)" },
  { value: "acceptEdits", label: "Accept edits" },
  { value: "bypassPermissions", label: "Bypass permissions" },
];
const CRED_SOURCES = [
  { value: "inherit", label: "Inherit (~/.claude credentials)" },
  { value: "api_key", label: "API key (ANTHROPIC_API_KEY)" },
  { value: "auth_token", label: "Auth token (ANTHROPIC_AUTH_TOKEN)" },
];
const MODEL_SUGGESTIONS = ["sonnet", "opus", "haiku"];

/** Backends that consume Claude credentials — only these show the auth group. */
function usesClaudeAuth(backend: string) {
  return backend === "claude_sdk";
}

interface ProfileForm {
  name: string;
  description: string;
  backend: string;
  default_model: string;
  permission_mode: string;
  fs_read: string[];
  fs_write: string[];
  allowed_tools: string[];
  denied_tools: string[];
  network_mode: string;
  network_allowlist: string[];
  system_prompt: string;
  run_token_scopes: string[];
  cred_source: "inherit" | "api_key" | "auth_token";
  cred_base_url: string;
  cred_value: string;
  env_replace: boolean;
  env_pairs: KvPair[];
}

function buildForm(p: ProfileOut): ProfileForm {
  return {
    name: p.name,
    description: p.description ?? "",
    backend: p.backend,
    default_model: p.default_model ?? "",
    permission_mode: p.permission_mode ?? "",
    fs_read: [...p.fs_read],
    fs_write: [...p.fs_write],
    allowed_tools: [...p.allowed_tools],
    denied_tools: [...p.denied_tools],
    network_mode: p.network_mode,
    network_allowlist: [...p.network_allowlist],
    system_prompt: p.system_prompt ?? "",
    run_token_scopes: [...p.run_token_scopes],
    cred_source: (p.claude_credentials?.source as ProfileForm["cred_source"]) ?? "inherit",
    cred_base_url: p.claude_credentials?.base_url ?? "",
    cred_value: "",
    env_replace: false,
    env_pairs: [],
  };
}

export function ProfileEditor({
  profile,
  onDeleted,
  onCopied,
}: {
  profile: ProfileOut;
  onDeleted: () => void;
  onCopied: (id: string) => void;
}) {
  const qc = useQueryClient();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [busy, setBusy] = useState(false);

  const { form, setForm, dirty, discard, commit } = useEditableForm<ProfileOut, ProfileForm>(
    profile,
    buildForm,
    profile.id + profile.updated_at,
  );

  if (!form) return null;
  const set = <K extends keyof ProfileForm>(k: K, v: ProfileForm[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  async function save() {
    if (!form) return;
    if (!form.name.trim()) {
      setError("Name is required.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const body: ProfileCreate & Record<string, unknown> = {
        name: form.name.trim(),
        description: form.description,
        backend: form.backend,
        default_model: form.default_model.trim() || null,
        permission_mode: (form.permission_mode || null) as ProfileCreate["permission_mode"],
        fs_read: form.fs_read,
        fs_write: form.fs_write,
        allowed_tools: form.allowed_tools,
        denied_tools: form.denied_tools,
        network_mode: form.network_mode,
        network_allowlist: form.network_allowlist,
        system_prompt: form.system_prompt.trim() || null,
        run_token_scopes: form.run_token_scopes,
      };
      if (usesClaudeAuth(form.backend)) {
        // Omit value unless the user typed one: env-based sources keep the
        // existing secret (rotation semantics); inherit needs no value.
        body.claude_credentials = {
          source: form.cred_source,
          base_url: form.cred_base_url.trim() || null,
          ...(form.cred_value.trim() ? { value: form.cred_value.trim() } : {}),
        };
      }
      // env is replace-only: send the whole map, and only when the user opts in.
      if (form.env_replace) {
        const env: Record<string, string> = {};
        for (const p of form.env_pairs) {
          const k = p.key.trim();
          if (k) env[k] = p.value;
        }
        body.env = env;
      }
      await profilesApi.update(profile.id, body);
      await qc.invalidateQueries({ queryKey: qk.profiles.all });
      commit();
      toast.success("Profile saved");
    } catch (err) {
      setError(describeError(err));
      toast.error("Could not save profile", { error: err });
    } finally {
      setSaving(false);
    }
  }

  async function copy() {
    setBusy(true);
    try {
      const created = await profileTransferApi.copy(profile.id);
      await qc.invalidateQueries({ queryKey: qk.profiles.all });
      toast.success(`Copied to “${created.name}”`);
      onCopied(created.id);
    } catch (err) {
      toast.error("Copy failed", { error: err });
    } finally {
      setBusy(false);
    }
  }

  async function exportProfile() {
    try {
      const data = await profileTransferApi.export(profile.id);
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${profile.name.replace(/[^a-z0-9-_]+/gi, "-")}.profile.json`;
      a.click();
      URL.revokeObjectURL(url);
      toast.success("Export downloaded", { description: "Secrets are redacted — re-enter on import." });
    } catch (err) {
      toast.error("Export failed", { error: err });
    }
  }

  async function remove() {
    setBusy(true);
    try {
      await profilesApi.remove(profile.id);
      await qc.invalidateQueries({ queryKey: qk.profiles.all });
      toast.success(`Deleted “${profile.name}”`);
      setConfirmDelete(false);
      onDeleted();
    } catch (err) {
      toast.error("Delete failed", { error: err });
    } finally {
      setBusy(false);
    }
  }

  const showAuth = usesClaudeAuth(form.backend);
  const secretOnFile = profile.claude_credentials?.value_set;

  return (
    <div>
      <div className="mb-5 flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h2 className="truncate font-display text-lg font-semibold text-moon-100">
              {form.name || "Untitled profile"}
            </h2>
            <Badge tone="neutral" mono>
              {BACKENDS.find((b) => b.value === form.backend)?.label ?? form.backend}
            </Badge>
          </div>
          <p className="mt-0.5 font-mono text-xs text-moon-600">{profile.id}</p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <IconButton label="Copy profile" icon={<Copy size={15} />} onClick={copy} disabled={busy} />
          <IconButton label="Export JSON" icon={<Download size={15} />} onClick={exportProfile} disabled={busy} />
          <IconButton
            label="Delete profile"
            icon={<Trash2 size={15} />}
            onClick={() => setConfirmDelete(true)}
            disabled={busy}
            className="hover:text-failed"
          />
        </div>
      </div>

      <div className="space-y-5">
        <SettingsCard title="Identity">
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3">
              <Field label="Name">
                <Input value={form.name} onChange={(e) => set("name", e.target.value)} invalid={!form.name.trim()} />
              </Field>
              <Field label="Backend">
                <Select value={form.backend} onChange={(e) => set("backend", e.target.value)}>
                  {BACKENDS.map((b) => (
                    <option key={b.value} value={b.value}>
                      {b.label}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
            <Field label="Description">
              <Input value={form.description} placeholder="What this profile is for" onChange={(e) => set("description", e.target.value)} />
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Default model" hint="Model alias or full id.">
                <Input
                  mono
                  list="model-suggestions"
                  placeholder="sonnet"
                  value={form.default_model}
                  onChange={(e) => set("default_model", e.target.value)}
                />
                <datalist id="model-suggestions">
                  {MODEL_SUGGESTIONS.map((m) => (
                    <option key={m} value={m} />
                  ))}
                </datalist>
              </Field>
              <Field label="Permission mode">
                <Select value={form.permission_mode} onChange={(e) => set("permission_mode", e.target.value)}>
                  {PERMISSION_MODES.map((m) => (
                    <option key={m.value} value={m.value}>
                      {m.label}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
          </div>
        </SettingsCard>

        <SettingsCard title="Filesystem" description="Paths the sandbox may read or write. Absolute paths only.">
          <div className="grid grid-cols-2 gap-4">
            <Field label="Readable paths">
              <ListEditor value={form.fs_read} onChange={(v) => set("fs_read", v)} placeholder="/usr" emptyHint="No read paths." />
            </Field>
            <Field label="Writable paths">
              <ListEditor value={form.fs_write} onChange={(v) => set("fs_write", v)} placeholder="/home/you/repo" emptyHint="No write paths." />
            </Field>
          </div>
        </SettingsCard>

        <SettingsCard title="Tools" description="Allow- and deny-lists for agent tools. Deny wins over allow.">
          <div className="grid grid-cols-2 gap-4">
            <Field label="Allowed tools">
              <ListEditor mono={false} value={form.allowed_tools} onChange={(v) => set("allowed_tools", v)} placeholder="Bash(git*)" emptyHint="All tools allowed." />
            </Field>
            <Field label="Denied tools">
              <ListEditor mono={false} value={form.denied_tools} onChange={(v) => set("denied_tools", v)} placeholder="WebFetch" emptyHint="Nothing denied." />
            </Field>
          </div>
        </SettingsCard>

        <SettingsCard title="Network">
          <div className="space-y-4">
            <Field label="Network mode">
              <Select value={form.network_mode} onChange={(e) => set("network_mode", e.target.value)} className="max-w-xs">
                <option value="off">Off — no network</option>
                <option value="on">On — network allowed</option>
              </Select>
            </Field>
            {form.network_mode === "on" && (
              <Field label="Allowlist" hint="Optional hostnames the sandbox may reach. Blank allows all when network is on.">
                <ListEditor value={form.network_allowlist} onChange={(v) => set("network_allowlist", v)} placeholder="api.anthropic.com" emptyHint="No restrictions." />
              </Field>
            )}
          </div>
        </SettingsCard>

        {showAuth && (
          <SettingsCard
            title="Credentials"
            description="How the run authenticates to Anthropic. Secrets are write-only."
          >
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-3">
                <Field label="Source">
                  <Select value={form.cred_source} onChange={(e) => set("cred_source", e.target.value as ProfileForm["cred_source"])}>
                    {CRED_SOURCES.map((s) => (
                      <option key={s.value} value={s.value}>
                        {s.label}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label="Base URL" hint="Optional non-Anthropic endpoint.">
                  <Input mono placeholder="https://api.anthropic.com" value={form.cred_base_url} onChange={(e) => set("cred_base_url", e.target.value)} />
                </Field>
              </div>
              {form.cred_source !== "inherit" && (
                <Field
                  label={form.cred_source === "api_key" ? "API key" : "Auth token"}
                  hint={
                    secretOnFile
                      ? "A secret is on file — leave blank to keep it, or enter a new value to rotate."
                      : "Enter the secret value. Stored encrypted."
                  }
                >
                  <div className="flex items-center gap-2">
                    <KeyRound size={15} className="shrink-0 text-moon-600" />
                    <Input
                      type="password"
                      mono
                      autoComplete="off"
                      placeholder={secretOnFile ? "•••••••• (kept)" : "sk-ant-…"}
                      value={form.cred_value}
                      onChange={(e) => set("cred_value", e.target.value)}
                    />
                    {secretOnFile && (
                      <Badge tone="success" dot>
                        on file
                      </Badge>
                    )}
                  </div>
                </Field>
              )}
            </div>
          </SettingsCard>
        )}

        <SettingsCard
          title="Environment"
          description="Environment variables injected into the run. Values are write-only."
        >
          <div className="space-y-3">
            {profile.env_keys.length > 0 ? (
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-xs text-moon-400">On file:</span>
                {profile.env_keys.map((k) => (
                  <Badge key={k} tone="neutral" mono>
                    {k}
                  </Badge>
                ))}
              </div>
            ) : (
              <p className="text-xs text-moon-600">No environment variables set.</p>
            )}
            <label className="flex items-center gap-2 text-sm text-moon-100">
              <input
                type="checkbox"
                checked={form.env_replace}
                onChange={(e) =>
                  setForm((f) => ({
                    ...f,
                    env_replace: e.target.checked,
                    env_pairs:
                      e.target.checked && f.env_pairs.length === 0
                        ? profile.env_keys.map((k) => ({ key: k, value: "" }))
                        : f.env_pairs,
                  }))
                }
                className="accent-lamp"
              />
              Replace environment
            </label>
            {form.env_replace && (
              <div className="rounded-control border border-warn/30 bg-warn/5 p-3">
                <p className="mb-2 text-xs text-warn">
                  Saving replaces the entire environment with the pairs below. Existing values are
                  not shown — re-enter every variable you want to keep.
                </p>
                <KeyValueEditor pairs={form.env_pairs} onChange={(p) => set("env_pairs", p)} />
              </div>
            )}
          </div>
        </SettingsCard>

        <SettingsCard title="System prompt" description="Prepended to the agent's system prompt for every run.">
          <Textarea
            className="min-h-[120px]"
            placeholder="Optional. e.g. house style, guardrails, repo conventions…"
            value={form.system_prompt}
            onChange={(e) => set("system_prompt", e.target.value)}
          />
        </SettingsCard>

        <SettingsCard
          title="Run-token scopes"
          description="Capabilities the run's scoped token may exercise via the API."
        >
          <label className="flex items-center gap-2.5 text-sm text-moon-100">
            <input
              type="checkbox"
              checked={form.run_token_scopes.includes("ticket.create")}
              onChange={(e) =>
                set(
                  "run_token_scopes",
                  e.target.checked
                    ? [...new Set([...form.run_token_scopes, "ticket.create"])]
                    : form.run_token_scopes.filter((s) => s !== "ticket.create"),
                )
              }
              className="accent-lamp"
            />
            <ShieldCheck size={15} className="text-moon-600" />
            <span>
              <span className="font-mono text-[13px]">ticket.create</span>
              <span className="ml-2 text-xs text-moon-600">Let the agent create new tickets</span>
            </span>
          </label>
        </SettingsCard>
      </div>

      <SaveBar dirty={dirty} saving={saving} onSave={save} onDiscard={discard} error={error} />

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={`Delete “${profile.name}”?`}
        body="Tickets referencing this profile will need a new one before they can run. This cannot be undone."
        confirmLabel="Delete profile"
        danger
        busy={busy}
        onConfirm={remove}
      />
    </div>
  );
}
