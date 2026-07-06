import { useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Copy, Download, KeyRound, Plus, ShieldCheck, Trash2 } from "lucide-react";
import { Button } from "@/ui/Button";
import { IconButton } from "@/ui/IconButton";
import { Input, Textarea, Field } from "@/ui/Input";
import { Select } from "@/ui/Select";
import { Badge } from "@/ui/Badge";
import { toast, describeError } from "@/ui/Toast";
import { profilesApi } from "@/api/profiles";
import { profileTransferApi } from "@/api/profileTransfer";
import { useProviders } from "@/api/providers";
import { useBackends } from "@/api/backends";
import { qk } from "@/api/keys";
import { cn } from "@/lib/cn";
import type {
  BackendConfigAgent,
  BackendOut,
  EndpointOut,
  ModelSlotOut,
  ProfileCreate,
  ProfileOut,
  ProviderOut,
} from "@/api/types";
import { SettingsCard } from "./parts/SettingsSection";
import { CollapsibleCard } from "./parts/CollapsibleCard";
import { SaveBar, useEditableForm } from "./parts/SaveBar";
import { ListEditor } from "./parts/ListEditor";
import { KeyValueEditor, type KvPair } from "./parts/KeyValueEditor";
import { ConfirmDialog } from "./parts/ConfirmDialog";

/** The three top-level segments a profile is organized into. Anchors for the
 *  sticky mini-nav below — order here is the scroll order on the page. */
const SEGMENTS = [
  { id: "profile-segment-identity", label: "Profile" },
  { id: "profile-segment-harness", label: "Harness" },
  { id: "profile-segment-sandbox", label: "Sandbox & run shape" },
] as const;

/** Highlights whichever segment is currently topmost in the viewport, so the
 *  sticky nav tracks scroll position instead of only reacting to clicks. */
function useScrollSpy(ids: readonly string[]): string {
  const [active, setActive] = useState<string>(ids[0]);
  useEffect(() => {
    const targets = ids.map((id) => document.getElementById(id)).filter((el): el is HTMLElement => !!el);
    if (targets.length === 0) return;
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries.filter((e) => e.isIntersecting);
        if (visible.length > 0) setActive(visible[0].target.id);
      },
      { rootMargin: "-72px 0px -70% 0px", threshold: 0 },
    );
    for (const el of targets) observer.observe(el);
    return () => observer.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ids.join(",")]);
  return active;
}

function SegmentNav() {
  const active = useScrollSpy(SEGMENTS.map((s) => s.id));
  return (
    <div className="sticky top-0 z-10 -mx-1 mb-5 flex gap-1 border-b border-ink-700/70 bg-ink-950/90 px-1 py-2 backdrop-blur">
      {SEGMENTS.map((s) => (
        <button
          key={s.id}
          type="button"
          onClick={() => document.getElementById(s.id)?.scrollIntoView({ behavior: "smooth", block: "start" })}
          className={cn(
            "rounded-control px-3 py-1.5 text-xs font-medium transition-colors",
            active === s.id ? "bg-ink-800 text-moon-100" : "text-moon-400 hover:bg-ink-800/60 hover:text-moon-100",
          )}
        >
          {s.label}
        </button>
      ))}
    </div>
  );
}

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

/** ``compatible(harness, endpoint) = protocol intersection AND lock match``.
 *  Client-side mirror of ``nightdesk.domain.providers.endpoint_compatible``;
 *  the server re-checks on save, this only drives UI resolution. See
 *  docs/design/providers-and-endpoints.md ("Compatibility is protocol
 *  intersection plus the lock"). */
function endpointCompatible(backend: BackendOut, ep: EndpointOut): boolean {
  if (!backend.protocol_kinds.includes(ep.protocol_kind)) return false;
  if (ep.harness_lock && ep.harness_lock !== backend.code) return false;
  return true;
}

function compatibleEndpoints(backend: BackendOut | null, provider: ProviderOut | null): EndpointOut[] {
  if (!backend || !provider) return [];
  return provider.endpoints.filter((ep) => endpointCompatible(backend, ep));
}

/** ``*_compat`` endpoints need every model position pinned or CC's own alias
 *  escalation falls back to a Claude model the endpoint can't serve — see
 *  "The CC alias-escape pitfall" in the design doc. First-party endpoints
 *  (and profiles with no endpoint yet) default to unpinned. */
function isCompatProtocol(ep: EndpointOut | null): boolean {
  return !!ep && ep.protocol_kind.endsWith("_compat");
}

function offMenuBadge(value: string, ep: EndpointOut | null) {
  if (!value.trim() || !ep || ep.models.length === 0 || ep.models.includes(value)) return null;
  return (
    <Badge tone="lamp" mono>
      off-menu
    </Badge>
  );
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
  endpoint_id: string | null;
  backend_config: Record<string, unknown>;
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
    endpoint_id: p.endpoint_id ?? null,
    backend_config: JSON.parse(JSON.stringify(p.backend_config ?? {})),
  };
}

/** Rows editor for a multi_endpoint backend's ``backend_config.agents[]``
 *  (opencode). Each row maps 1:1 onto an agent: name, model, an endpoint
 *  picker that defaults to the profile's primary endpoint, tools, and an
 *  optional prompt. */
function AgentsEditor({
  agents,
  onChange,
  backend,
  providers,
}: {
  agents: BackendConfigAgent[];
  onChange: (next: BackendConfigAgent[]) => void;
  backend: BackendOut;
  providers: ProviderOut[];
}) {
  const endpointOptions = useMemo(
    () =>
      providers.flatMap((p) =>
        p.endpoints
          .filter((ep) => endpointCompatible(backend, ep))
          .map((ep) => ({ id: ep.id, label: `${p.name} · ${ep.label || ep.protocol_kind}` })),
      ),
    [providers, backend],
  );

  function update(i: number, patch: Partial<BackendConfigAgent>) {
    onChange(agents.map((a, idx) => (idx === i ? { ...a, ...patch } : a)));
  }
  function remove(i: number) {
    onChange(agents.filter((_, idx) => idx !== i));
  }
  function add() {
    onChange([...agents, { name: `agent${agents.length + 1}`, model: "", endpoint_id: null, tools: [], prompt: "" }]);
  }

  return (
    <div className="space-y-3">
      {agents.map((agent, i) => (
        <div key={i} className="space-y-3 rounded-control border border-ink-700 p-3">
          <div className="flex items-center justify-between gap-2">
            <Input
              mono
              className="max-w-[12rem]"
              value={agent.name}
              onChange={(e) => update(i, { name: e.target.value })}
            />
            <IconButton label="Remove agent" icon={<Trash2 size={14} />} onClick={() => remove(i)} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Model">
              <Input mono value={agent.model ?? ""} onChange={(e) => update(i, { model: e.target.value })} />
            </Field>
            <Field label="Endpoint" hint="Defaults to the profile's primary endpoint.">
              <Select
                value={agent.endpoint_id ?? ""}
                onChange={(e) => update(i, { endpoint_id: e.target.value || null })}
              >
                <option value="">(same as primary)</option>
                {endpointOptions.map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.label}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
          <Field label="Tools">
            <ListEditor
              mono={false}
              value={agent.tools ?? []}
              onChange={(v) => update(i, { tools: v })}
              placeholder="webfetch"
              emptyHint="All tools allowed."
            />
          </Field>
          <Field label="System prompt" hint="Optional.">
            <Textarea
              className="min-h-[72px]"
              value={agent.prompt ?? ""}
              onChange={(e) => update(i, { prompt: e.target.value })}
            />
          </Field>
        </div>
      ))}
      <Button variant="ghost" size="sm" leadingIcon={<Plus size={13} />} onClick={add}>
        Add agent
      </Button>
    </div>
  );
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
  const backendsQ = useBackends();
  const providersQ = useProviders();
  const backends = useMemo(() => backendsQ.data ?? [], [backendsQ.data]);
  const providers = useMemo(() => providersQ.data ?? [], [providersQ.data]);

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [busy, setBusy] = useState(false);
  const [selectedProviderId, setSelectedProviderId] = useState<string | null>(null);
  const [customizeOverride, setCustomizeOverride] = useState<boolean | null>(null);
  // Gates the resolution effect below until the provider seeded from
  // profile.endpoint_id has actually landed in state. Without this, the
  // resolution effect (which also runs on the same initial commit, before
  // setSelectedProviderId's update is visible) sees a transient provider=null
  // and wipes a perfectly valid endpoint_id — stranding the radio selection
  // and diverging `form` from the useEditableForm baseline into a spurious
  // dirty state. Plain state (not a ref) matters here: it updates in the same
  // batched re-render as selectedProviderId, so the two are only ever read
  // together, never mid-transition.
  const [providerHydrated, setProviderHydrated] = useState(false);

  const { form, setForm, dirty, discard, commit } = useEditableForm<ProfileOut, ProfileForm>(
    profile,
    buildForm,
    profile.id + profile.updated_at,
  );

  const profileKey = profile.id + profile.updated_at;

  // Re-seed the provider selection and the customize-toggle override whenever
  // a different profile (or a fresh save) loads — otherwise a previous
  // profile's UI state would bleed into the next one. Waits for the
  // providers query so a fresh mount doesn't seed "no provider" before the
  // list has even loaded.
  useEffect(() => {
    setCustomizeOverride(null);
    setProviderHydrated(false);
    if (!providersQ.isSuccess) return;
    setSelectedProviderId(
      providers.find((p) => p.endpoints.some((e) => e.id === profile.endpoint_id))?.id ?? null,
    );
    setProviderHydrated(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profileKey, providersQ.isSuccess, providers]);

  const backend = backends.find((b) => b.code === form?.backend) ?? null;
  const provider = providers.find((p) => p.id === selectedProviderId) ?? null;
  const compatible = useMemo(() => compatibleEndpoints(backend, provider), [backend, provider]);
  const allEndpoints = useMemo(
    () => providers.flatMap((p) => p.endpoints),
    [providers],
  );
  const selectedEndpoint = allEndpoints.find((e) => e.id === form?.endpoint_id) ?? null;

  // Resolve the primary endpoint whenever the backend or the chosen provider
  // changes: auto-select on a single match, block on none, otherwise let the
  // radio list below decide. Also clears a stale endpoint_id after a backend
  // switch that no longer passes the compatibility gate. Gated on
  // providerHydrated so this never runs against the pre-hydration transient
  // state described above — a freshly loaded profile's endpoint_id is left
  // untouched as long as it still passes the compatibility gate.
  useEffect(() => {
    if (!form || !backend || !providerHydrated) return;
    if (!provider) {
      if (form.endpoint_id) setForm((f) => ({ ...f, endpoint_id: null }));
      return;
    }
    if (compatible.length === 1) {
      if (form.endpoint_id !== compatible[0].id) {
        setForm((f) => ({ ...f, endpoint_id: compatible[0].id }));
      }
    } else if (compatible.length === 0) {
      if (form.endpoint_id) setForm((f) => ({ ...f, endpoint_id: null }));
    } else if (form.endpoint_id && !compatible.some((e) => e.id === form.endpoint_id)) {
      setForm((f) => ({ ...f, endpoint_id: null }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [backend?.code, provider?.id, compatible.map((e) => e.id).join(","), providerHydrated]);

  if (!form) return null;
  const set = <K extends keyof ProfileForm>(k: K, v: ProfileForm[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  const staticSlots: ModelSlotOut[] = backend?.model_slots ?? [];
  const isCompat = isCompatProtocol(selectedEndpoint);
  const hasSlotOverrides = staticSlots.some((s) => {
    const v = form.backend_config[s.name];
    return typeof v === "string" && v.trim() !== "";
  });
  const customize = customizeOverride ?? hasSlotOverrides;
  const datalistId = `model-menu-${form.endpoint_id ?? "none"}`;
  const agents = (form.backend_config.agents as BackendConfigAgent[] | undefined) ?? [];

  function setCustomize(next: boolean) {
    setCustomizeOverride(next);
    if (!next) {
      setForm((f) => {
        const cfg = { ...f.backend_config };
        for (const s of staticSlots) delete cfg[s.name];
        return { ...f, backend_config: cfg };
      });
    }
  }

  function setSlotOverride(name: string, value: string) {
    setForm((f) => {
      const cfg = { ...f.backend_config };
      if (value.trim()) cfg[name] = value;
      else delete cfg[name];
      return { ...f, backend_config: cfg };
    });
  }

  function setAgents(next: BackendConfigAgent[]) {
    setForm((f) => ({ ...f, backend_config: { ...f.backend_config, agents: next } }));
  }

  async function save() {
    if (!form) return;
    if (!form.name.trim()) {
      setError("Name is required.");
      return;
    }
    if (provider && compatible.length === 0) {
      setError(`No endpoint on “${provider.name}” is compatible with ${backend?.label ?? form.backend}.`);
      return;
    }
    if (backend?.requires_provider && !form.endpoint_id) {
      setError(`${backend.label} requires a provider endpoint before it can run.`);
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
        endpoint_id: form.endpoint_id,
        backend_config: form.backend_config,
      };
      if (backend?.group_keys.includes("claude_auth")) {
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
      const saved = await profilesApi.update(profile.id, body);
      await qc.invalidateQueries({ queryKey: qk.profiles.all });
      commit();
      if (saved.warnings.length > 0) {
        // No dedicated "warning" toast variant — these are non-blocking, so
        // `info` (not `error`) is the right severity.
        for (const w of saved.warnings) toast.info(w);
      } else {
        toast.success("Profile saved");
      }
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

  const showAuth = backend?.group_keys.includes("claude_auth") ?? false;
  const showPermissionMode = backend?.group_keys.includes("permission_mode") ?? false;
  const showProvider = backend?.group_keys.includes("provider") ?? false;
  const showModels = staticSlots.length > 0;
  const secretOnFile = profile.claude_credentials?.value_set;

  // Baseline for the two collapsed-by-default cards below, so a closed card
  // can still surface a dirty dot instead of silently hiding edits.
  const envDirty =
    form.env_replace !== false || JSON.stringify(form.env_pairs) !== JSON.stringify([]);
  const scopesDirty =
    JSON.stringify([...form.run_token_scopes].sort()) !==
    JSON.stringify([...profile.run_token_scopes].sort());

  return (
    <div>
      <div className="mb-5 flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h2 className="truncate font-display text-lg font-semibold text-moon-100">
              {form.name || "Untitled profile"}
            </h2>
            <Badge tone="neutral" mono>
              {backend?.label ?? form.backend}
            </Badge>
            {backend && !backend.executable && (
              <Badge tone="failed" mono>
                not yet wired
              </Badge>
            )}
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

      <SegmentNav />

      <div className="space-y-8">
        {/* Segment 1 — Profile identity. Backend-independent, always present. */}
        <section id="profile-segment-identity" className="scroll-mt-16">
          <SettingsCard title="Identity" description="Name and description — the same for every harness.">
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-3">
                <Field label="Name">
                  <Input value={form.name} onChange={(e) => set("name", e.target.value)} invalid={!form.name.trim()} />
                </Field>
                <Field label="Description">
                  <Input value={form.description} placeholder="What this profile is for" onChange={(e) => set("description", e.target.value)} />
                </Field>
              </div>
            </div>
          </SettingsCard>
        </section>

        {/* Segment 2 — everything owned by the selected harness. The whole
            frame is keyed on backend.code so switching backends visibly
            swaps it (fade transition) instead of quietly re-rendering cards
            in place — the swap itself is the signal that these settings
            belong to the harness, not the profile. */}
        <section id="profile-segment-harness" className="scroll-mt-16">
          <div className="rounded-card border border-lamp/25 bg-lamp/[0.04] p-4">
            <div key={backend?.code ?? form.backend} className="fade-in space-y-5">
              <div className="flex flex-wrap items-start justify-between gap-4 border-b border-lamp/20 pb-4">
                <div className="min-w-0">
                  <p className="text-[11px] font-semibold uppercase tracking-wide text-lamp">Harness</p>
                  <div className="mt-0.5 flex items-center gap-2">
                    <h3 className="font-display text-base font-semibold text-moon-100">
                      {backend?.label ?? form.backend}
                    </h3>
                    {backend && !backend.executable && (
                      <Badge tone="failed" mono>
                        not yet wired
                      </Badge>
                    )}
                  </div>
                  {backend?.summary && (
                    <p className="mt-1 max-w-md text-xs text-moon-400">{backend.summary}</p>
                  )}
                </div>
                <Field label="Backend" className="w-60 shrink-0">
                  <Select value={form.backend} onChange={(e) => set("backend", e.target.value)}>
                    {backends.map((b) => (
                      <option key={b.code} value={b.code} disabled={!b.enabled}>
                        {b.label}
                        {!b.enabled ? " (not selectable)" : ""}
                      </option>
                    ))}
                  </Select>
                </Field>
              </div>

              {showPermissionMode && (
                <Field label="Permission mode" className="max-w-xs">
                  <Select value={form.permission_mode} onChange={(e) => set("permission_mode", e.target.value)}>
                    {PERMISSION_MODES.map((m) => (
                      <option key={m.value} value={m.value}>
                        {m.label}
                      </option>
                    ))}
                  </Select>
                </Field>
              )}

              {showProvider && (
                <SettingsCard
                  title="Provider & endpoint"
                  description={
                    backend?.requires_provider
                      ? "Required — this harness cannot run without a configured provider."
                      : "Optional — leave blank to use ambient/inherited credentials."
                  }
                >
                  <div className="space-y-3">
                    <Field label="Provider">
                      <Select
                        value={selectedProviderId ?? ""}
                        onChange={(e) => setSelectedProviderId(e.target.value || null)}
                      >
                        <option value="">— none —</option>
                        {providers.map((p) => (
                          <option key={p.id} value={p.id}>
                            {p.name} ({p.vendor})
                          </option>
                        ))}
                      </Select>
                    </Field>
                    {provider && backend && (
                      compatible.length === 0 ? (
                        <p className="text-xs text-failed">
                          No endpoint on “{provider.name}” speaks a protocol {backend.label} supports
                          (supports {backend.protocol_kinds.join(", ")}).
                        </p>
                      ) : compatible.length === 1 ? (
                        <p className="text-xs text-moon-400">
                          {provider.name} · <span className="font-mono">{compatible[0].protocol_kind}</span>
                          {compatible[0].label ? ` (${compatible[0].label})` : ""}
                        </p>
                      ) : (
                        <div className="space-y-1.5">
                          {compatible.map((ep) => (
                            <label key={ep.id} className="flex items-center gap-2 text-sm text-moon-100">
                              <input
                                type="radio"
                                name="primary-endpoint"
                                checked={form.endpoint_id === ep.id}
                                onChange={() => set("endpoint_id", ep.id)}
                                className="accent-lamp"
                              />
                              <span>
                                {ep.label || ep.protocol_kind}{" "}
                                <span className="font-mono text-xs text-moon-600">({ep.protocol_kind})</span>
                              </span>
                            </label>
                          ))}
                        </div>
                      )
                    )}
                    {backend?.requires_provider && !form.endpoint_id && (
                      <p className="text-xs text-failed">This harness requires a provider endpoint before it can run.</p>
                    )}
                  </div>
                </SettingsCard>
              )}

              {showModels && backend && (
                <SettingsCard title="Models" description="Model assignments this profile's harness resolves at launch.">
                  <div className="space-y-4">
                    {isCompat ? (
                      <>
                        <Field
                          label="Model"
                          hint={`Applied to every position — ${staticSlots.length} position${staticSlots.length === 1 ? "" : "s"}.`}
                        >
                          <div className="flex items-center gap-2">
                            <Input
                              mono
                              list={datalistId}
                              value={form.default_model}
                              placeholder="glm-5.2"
                              onChange={(e) => set("default_model", e.target.value)}
                            />
                            {offMenuBadge(form.default_model, selectedEndpoint)}
                          </div>
                        </Field>
                        <button
                          type="button"
                          className="text-xs text-lamp hover:underline"
                          onClick={() => setCustomize(!customize)}
                        >
                          {customize ? "Use one model for every position" : "Customize per position"}
                        </button>
                        {customize && (
                          <div className="grid grid-cols-2 gap-3">
                            {staticSlots.map((slot) => {
                              const value = (form.backend_config[slot.name] as string) ?? "";
                              return (
                                <Field key={slot.name} label={slot.label}>
                                  <div className="flex items-center gap-2">
                                    <Input
                                      mono
                                      list={datalistId}
                                      value={value}
                                      placeholder={form.default_model || "(unset)"}
                                      onChange={(e) => setSlotOverride(slot.name, e.target.value)}
                                    />
                                    {offMenuBadge(value, selectedEndpoint)}
                                  </div>
                                </Field>
                              );
                            })}
                          </div>
                        )}
                      </>
                    ) : (
                      <Field label="Model" hint="Leave empty to use the harness's own defaults.">
                        <div className="flex items-center gap-2">
                          <Input
                            mono
                            list={datalistId}
                            value={form.default_model}
                            placeholder="(unpinned)"
                            onChange={(e) => set("default_model", e.target.value)}
                          />
                          {offMenuBadge(form.default_model, selectedEndpoint)}
                        </div>
                      </Field>
                    )}
                    <datalist id={datalistId}>
                      {(selectedEndpoint?.models ?? []).map((m) => (
                        <option key={m} value={m} />
                      ))}
                    </datalist>

                    {backend.multi_endpoint && (
                      <div className="border-t border-ink-700/70 pt-4">
                        <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-moon-400">Agents</h4>
                        <AgentsEditor agents={agents} onChange={setAgents} backend={backend} providers={providers} />
                      </div>
                    )}
                  </div>
                </SettingsCard>
              )}

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

              {!showPermissionMode && !showProvider && !showModels && !showAuth && (
                <p className="text-xs text-moon-600">This harness has no additional configuration.</p>
              )}
            </div>
          </div>
        </section>

        {/* Segment 3 — shared sandbox/run shape. Backend-independent. */}
        <section id="profile-segment-sandbox" className="scroll-mt-16 space-y-5">
          <p className="text-xs text-moon-400">
            These settings apply regardless of harness — they describe the sandbox and run shape,
            not how the agent itself is invoked.
          </p>

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

          <CollapsibleCard
            title="Environment"
            description="Environment variables injected into the run. Values are write-only."
            dirty={envDirty}
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
          </CollapsibleCard>

          <SettingsCard title="System prompt" description="Prepended to the agent's system prompt for every run.">
            <Textarea
              className="min-h-[120px]"
              placeholder="Optional. e.g. house style, guardrails, repo conventions…"
              value={form.system_prompt}
              onChange={(e) => set("system_prompt", e.target.value)}
            />
          </SettingsCard>

          <CollapsibleCard
            title="Run-token scopes"
            description="Capabilities the run's scoped token may exercise via the API."
            dirty={scopesDirty}
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
          </CollapsibleCard>
        </section>
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
