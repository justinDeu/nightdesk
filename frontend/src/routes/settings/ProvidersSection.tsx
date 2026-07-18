import { useEffect, useMemo, useState } from "react";
import {
  Cable,
  ChevronDown,
  ChevronRight,
  KeyRound,
  Lock,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { Button } from "@/ui/Button";
import { Input, Textarea, Field } from "@/ui/Input";
import { Select } from "@/ui/Select";
import { Dialog } from "@/ui/Dialog";
import { Badge } from "@/ui/Badge";
import { Spinner } from "@/ui/Spinner";
import { EmptyState } from "@/ui/EmptyState";
import { Tooltip } from "@/ui/Tooltip";
import { toast } from "@/ui/Toast";
import { ApiError } from "@/api/client";
import {
  useProviders,
  useProviderCatalog,
  useProviderProtocols,
  useCreateProvider,
  useDeleteProvider,
  useRotateCredential,
  useCreateEndpoint,
  useUpdateEndpoint,
  useDeleteEndpoint,
  usePullModels,
} from "@/api/providers";
import type {
  CatalogEndpointOut,
  CatalogOfferingOut,
  CredentialSource,
  EndpointCreate,
  EndpointOut,
  ProtocolInfoOut,
  ProtocolKind,
  ProviderOut,
} from "@/api/types";
import { relativeTime } from "@/lib/time";
import { cn } from "@/lib/cn";
import { SectionHeader } from "./parts/SettingsSection";
import { ConfirmDialog } from "./parts/ConfirmDialog";
import { ListEditor } from "./parts/ListEditor";

const PROTOCOL_LABEL: Record<ProtocolKind, string> = {
  anthropic: "Anthropic",
  anthropic_compat: "Anthropic-compatible",
  openai: "OpenAI",
  openai_compat: "OpenAI-compatible",
  openai_codex: "OpenAI Codex (Responses)",
  openrouter: "OpenRouter",
  ollama: "Ollama",
};

const CREDENTIAL_SOURCE_LABEL: Record<CredentialSource, string> = {
  api_key: "API key",
  oauth_file: "OAuth file",
  subscription_file: "Subscription file",
  env_var: "Environment variable",
  none: "None",
};

const HARNESS_LABEL: Record<string, string> = {
  claude_sdk: "Claude Code",
};

/** Known default credential-file paths, keyed by credential_source — the
 *  catalog API doesn't carry these, so they're seeded client-side as hints. */
const DEFAULT_CREDENTIAL_PATH: Partial<Record<CredentialSource, string>> = {
  oauth_file: "~/.codex/auth.json",
  subscription_file: "~/.claude/.credentials.json",
};

function isPathCredential(source: CredentialSource): boolean {
  return source === "oauth_file" || source === "subscription_file";
}

/** Whether `kind` has a model-list operation to pull from, per the
 *  `/api/v1/providers/protocols` capability data — drives "Refresh models"
 *  disabling and manual-curation messaging instead of hard-coding protocol
 *  names in the UI. Defaults to `true` (enabled) while the capability data
 *  hasn't loaded yet, so the button doesn't flash disabled on first paint. */
function protocolSupportsModelList(protocols: ProtocolInfoOut[] | undefined, kind: ProtocolKind): boolean {
  const info = protocols?.find((p) => p.key === kind);
  return info ? info.supports_model_list : true;
}

/** Catalog-seeded model ids for a protocol with no list operation (e.g.
 *  Codex), so a fresh endpoint on that protocol doesn't start from a blank
 *  text box. Looks across every offering rather than one specific key, so it
 *  keeps working if the catalog grows another no-list protocol. */
function catalogModelSuggestion(
  catalog: CatalogOfferingOut[] | undefined,
  kind: ProtocolKind,
): CatalogEndpointOut | undefined {
  return catalog?.flatMap((o) => o.endpoints).find((e) => e.protocol_kind === kind && e.models.length > 0);
}

export function ProvidersSection() {
  const providers = useProviders();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const list = useMemo(
    () => [...(providers.data ?? [])].sort((a, b) => a.name.localeCompare(b.name)),
    [providers.data],
  );

  useEffect(() => {
    if (list.length === 0) {
      setSelectedId(null);
    } else if (!selectedId || !list.some((p) => p.id === selectedId)) {
      setSelectedId(list[0].id);
    }
  }, [list, selectedId]);

  const selected = list.find((p) => p.id === selectedId) ?? null;

  return (
    <div>
      <SectionHeader
        title="Providers"
        description="Vendor identities and the endpoints they expose. Configured once, reused by every compatible harness profile."
      />

      {providers.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-moon-400">
          <Spinner size={14} /> Loading…
        </div>
      ) : list.length === 0 ? (
        <EmptyState
          icon={<Cable size={18} />}
          title="No providers yet"
          description="A provider is a vendor account — ZAI, OpenAI, Anthropic — with the endpoints, credentials, and models a profile can run against."
          action={
            <Button variant="primary" leadingIcon={<Plus size={14} />} onClick={() => setCreating(true)}>
              Add provider
            </Button>
          }
        />
      ) : (
        <div className="grid grid-cols-[240px_1fr] gap-6">
          <div className="min-w-0">
            <div className="mb-3 flex items-center justify-between">
              <h3 className="text-xs font-medium uppercase tracking-wide text-moon-600">
                {list.length} provider{list.length === 1 ? "" : "s"}
              </h3>
              <Button variant="primary" size="sm" leadingIcon={<Plus size={13} />} onClick={() => setCreating(true)}>
                New
              </Button>
            </div>
            <nav className="space-y-0.5">
              {list.map((p) => {
                const active = p.id === selectedId;
                return (
                  <button
                    key={p.id}
                    type="button"
                    onClick={() => setSelectedId(p.id)}
                    className={cn(
                      "w-full rounded-control px-2.5 py-2 text-left transition-colors",
                      active ? "bg-ink-800" : "hover:bg-ink-800/60",
                    )}
                  >
                    <span className={cn("block truncate text-sm font-medium", active ? "text-moon-100" : "text-moon-400")}>
                      {p.name}
                    </span>
                    <div className="mt-1 flex items-center gap-1.5">
                      <Badge tone="neutral" mono>{p.vendor}</Badge>
                      <span className="text-[11px] text-moon-600">
                        {p.endpoints.length} endpoint{p.endpoints.length === 1 ? "" : "s"}
                      </span>
                    </div>
                  </button>
                );
              })}
            </nav>
          </div>

          <div className="min-w-0">
            {selected && (
              <ProviderDetail
                key={selected.id}
                provider={selected}
                onDeleted={() => setSelectedId(null)}
              />
            )}
          </div>
        </div>
      )}

      {creating && (
        <CreateProviderDialog
          onClose={() => setCreating(false)}
          onCreated={(id) => {
            setSelectedId(id);
            setCreating(false);
          }}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Provider detail: header + endpoint cards
// ---------------------------------------------------------------------------

function ProviderDetail({
  provider,
  onDeleted,
}: {
  provider: ProviderOut;
  onDeleted: () => void;
}) {
  const deleteProvider = useDeleteProvider();
  const rotateCredential = useRotateCredential();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [rotating, setRotating] = useState(false);
  const [addingEndpoint, setAddingEndpoint] = useState(false);
  const [editingEndpoint, setEditingEndpoint] = useState<EndpointOut | null>(null);

  const apiKeyEndpointCount = provider.endpoints.filter((e) => e.credential_source === "api_key").length;

  async function doDelete() {
    try {
      await deleteProvider.mutateAsync(provider.id);
      toast.success("Provider deleted");
      onDeleted();
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Could not delete provider";
      toast.error(msg);
    } finally {
      setConfirmDelete(false);
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-4 rounded-card border border-ink-700 bg-ink-900 px-4 py-3.5">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h3 className="font-display text-base font-semibold text-moon-100">{provider.name}</h3>
            <Badge tone="neutral" mono>{provider.vendor}</Badge>
          </div>
          <p className="mt-1 text-xs text-moon-600">
            {provider.endpoints.length} endpoint{provider.endpoints.length === 1 ? "" : "s"}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {apiKeyEndpointCount > 0 && (
            <Button
              variant="ghost"
              size="sm"
              leadingIcon={<KeyRound size={13} />}
              onClick={() => setRotating(true)}
            >
              Rotate credential
            </Button>
          )}
          <Button variant="ghost" size="sm" leadingIcon={<Plus size={13} />} onClick={() => setAddingEndpoint(true)}>
            Add endpoint
          </Button>
          <Button
            variant="danger"
            size="sm"
            leadingIcon={<Trash2 size={13} />}
            onClick={() => setConfirmDelete(true)}
          >
            Delete
          </Button>
        </div>
      </div>

      <div className="space-y-3">
        {provider.endpoints.map((ep) => (
          <EndpointCard key={ep.id} endpoint={ep} onEdit={() => setEditingEndpoint(ep)} />
        ))}
        {provider.endpoints.length === 0 && (
          <p className="text-sm text-moon-600">No endpoints on this provider yet.</p>
        )}
      </div>

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={`Delete ${provider.name}?`}
        body="This removes the provider and every endpoint on it. Blocked while any profile references an endpoint."
        confirmLabel="Delete"
        danger
        busy={deleteProvider.isPending}
        onConfirm={doDelete}
      />

      {rotating && (
        <RotateCredentialDialog
          provider={provider}
          onClose={() => setRotating(false)}
          onRotate={async (value) => {
            try {
              const result = await rotateCredential.mutateAsync({ id: provider.id, body: { credential_value: value } });
              toast.success(`Rotated ${result.rotated} endpoint${result.rotated === 1 ? "" : "s"}`);
              setRotating(false);
            } catch (err) {
              toast.error(err instanceof ApiError ? err.message : "Could not rotate credential");
            }
          }}
          busy={rotateCredential.isPending}
        />
      )}

      {addingEndpoint && (
        <EndpointFormDialog
          title="Add endpoint"
          providerId={provider.id}
          onClose={() => setAddingEndpoint(false)}
        />
      )}

      {editingEndpoint && (
        <EndpointFormDialog
          title="Edit endpoint"
          providerId={provider.id}
          endpoint={editingEndpoint}
          onClose={() => setEditingEndpoint(null)}
        />
      )}
    </div>
  );
}

function RotateCredentialDialog({
  provider,
  onClose,
  onRotate,
  busy,
}: {
  provider: ProviderOut;
  onClose: () => void;
  onRotate: (value: string) => void;
  busy: boolean;
}) {
  const [value, setValue] = useState("");
  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={`Rotate credential — ${provider.name}`}
      description="Updates every api_key endpoint on this provider that has a stored credential."
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" loading={busy} disabled={!value.trim()} onClick={() => onRotate(value.trim())}>
            Rotate
          </Button>
        </>
      }
    >
      <Field label="New API key">
        <Input mono type="password" autoFocus value={value} onChange={(e) => setValue(e.target.value)} placeholder="sk-…" />
      </Field>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Endpoint card
// ---------------------------------------------------------------------------

function EndpointCard({ endpoint, onEdit }: { endpoint: EndpointOut; onEdit: () => void }) {
  const pullModels = usePullModels();
  const deleteEndpoint = useDeleteEndpoint();
  const protocols = useProviderProtocols();
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [expanded, setExpanded] = useState(false);

  const canListModels = protocolSupportsModelList(protocols.data, endpoint.protocol_kind);

  async function refresh() {
    try {
      const result = await pullModels.mutateAsync(endpoint.id);
      toast.success(`Pulled ${result.models.length} model${result.models.length === 1 ? "" : "s"}`);
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not pull models");
    }
  }

  async function doDelete() {
    try {
      await deleteEndpoint.mutateAsync(endpoint.id);
      toast.success("Endpoint deleted");
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not delete endpoint");
    } finally {
      setConfirmDelete(false);
    }
  }

  return (
    <div className="rounded-card border border-ink-700 bg-ink-900">
      <div className="flex items-start justify-between gap-4 px-4 py-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-sm font-medium text-moon-100">{endpoint.label || PROTOCOL_LABEL[endpoint.protocol_kind]}</span>
            <Badge tone="neutral" mono>{PROTOCOL_LABEL[endpoint.protocol_kind]}</Badge>
            {endpoint.harness_lock && (
              <Tooltip content={`Restricted to ${HARNESS_LABEL[endpoint.harness_lock] ?? endpoint.harness_lock} by vendor terms.`}>
                <span>
                  <Badge tone="review" dot>
                    <Lock size={10} /> {HARNESS_LABEL[endpoint.harness_lock] ?? endpoint.harness_lock} only
                  </Badge>
                </span>
              </Tooltip>
            )}
          </div>
          {endpoint.base_url && (
            <p className="mt-1 truncate font-mono text-[12px] text-moon-400">{endpoint.base_url}</p>
          )}
          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-moon-600">
            <span>{CREDENTIAL_SOURCE_LABEL[endpoint.credential_source]}</span>
            <span className={endpoint.credential_set ? "text-success" : "text-moon-600"}>
              {endpoint.credential_source === "none" ? "no credential needed" : endpoint.credential_set ? "credential on file" : "no credential set"}
            </span>
            {endpoint.models_pulled_at && <span>models pulled {relativeTime(endpoint.models_pulled_at)}</span>}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {canListModels ? (
            <Button
              variant="ghost"
              size="sm"
              leadingIcon={pullModels.isPending ? <Spinner size={12} /> : <RefreshCw size={12} />}
              onClick={refresh}
              disabled={pullModels.isPending}
            >
              Refresh models
            </Button>
          ) : (
            <Tooltip content={`${PROTOCOL_LABEL[endpoint.protocol_kind]} has no model-list operation — its models are curated manually. Edit the endpoint to add or remove ids.`}>
              <span>
                <Button variant="ghost" size="sm" leadingIcon={<RefreshCw size={12} />} disabled>
                  Refresh models
                </Button>
              </span>
            </Tooltip>
          )}
          <Button variant="ghost" size="sm" onClick={onEdit}>Edit</Button>
          <Button variant="danger" size="sm" onClick={() => setConfirmDelete(true)}>
            <Trash2 size={13} />
          </Button>
        </div>
      </div>

      <div className="border-t border-ink-700/70 px-4 py-2">
        <button
          type="button"
          onClick={() => setExpanded((e) => !e)}
          disabled={endpoint.models.length === 0}
          className="flex items-center gap-1 text-xs text-moon-400 hover:text-moon-100 disabled:pointer-events-none disabled:opacity-50"
        >
          {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
          {endpoint.models.length} model{endpoint.models.length === 1 ? "" : "s"}
          {endpoint.default_model && <span className="text-moon-600">· default {endpoint.default_model}</span>}
        </button>
        {expanded && endpoint.models.length > 0 && (
          <ul className="mt-2 flex flex-wrap gap-1.5">
            {endpoint.models.map((m) => (
              <li key={m} className="rounded-control border border-ink-700 bg-ink-800 px-2 py-0.5 font-mono text-[11px] text-moon-100">
                {m}
              </li>
            ))}
          </ul>
        )}
      </div>

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={`Delete endpoint "${endpoint.label || PROTOCOL_LABEL[endpoint.protocol_kind]}"?`}
        confirmLabel="Delete"
        danger
        busy={deleteEndpoint.isPending}
        onConfirm={doDelete}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Endpoint add/edit dialog
// ---------------------------------------------------------------------------

function EndpointFormDialog({
  title,
  providerId,
  endpoint,
  onClose,
}: {
  title: string;
  providerId: string;
  endpoint?: EndpointOut;
  onClose: () => void;
}) {
  const createEndpoint = useCreateEndpoint();
  const updateEndpoint = useUpdateEndpoint();
  const protocols = useProviderProtocols();
  const catalog = useProviderCatalog();
  const [label, setLabel] = useState(endpoint?.label ?? "");
  const [protocolKind, setProtocolKind] = useState<ProtocolKind>(endpoint?.protocol_kind ?? "anthropic_compat");
  const [baseUrl, setBaseUrl] = useState(endpoint?.base_url ?? "");
  const [credentialSource, setCredentialSource] = useState<CredentialSource>(endpoint?.credential_source ?? "api_key");
  const [credentialValue, setCredentialValue] = useState("");
  const [harnessLock, setHarnessLock] = useState(endpoint?.harness_lock ?? "");
  const [defaultModel, setDefaultModel] = useState(endpoint?.default_model ?? "");
  const [models, setModels] = useState<string[]>(endpoint?.models ?? []);
  const [extra, setExtra] = useState("");
  const busy = createEndpoint.isPending || updateEndpoint.isPending;
  const canListModels = protocolSupportsModelList(protocols.data, protocolKind);

  // New endpoint, protocol with no list operation, nothing typed yet: seed
  // the manual model menu from the catalog instead of leaving it blank —
  // the user shouldn't have to type Codex model ids from memory. Never runs
  // when editing an existing endpoint (would clobber curated models).
  useEffect(() => {
    if (endpoint) return;
    if (protocolSupportsModelList(protocols.data, protocolKind)) return;
    if (models.length > 0) return;
    const suggestion = catalogModelSuggestion(catalog.data, protocolKind);
    if (!suggestion) return;
    setModels(suggestion.models);
    setDefaultModel((prev) => prev || suggestion.default_model || "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [protocolKind, protocols.data, catalog.data, endpoint]);

  async function save() {
    const fields: EndpointCreate = {
      label: label.trim(),
      protocol_kind: protocolKind,
      base_url: baseUrl.trim() || null,
      credential_source: credentialSource,
      harness_lock: harnessLock.trim() || null,
      default_model: defaultModel.trim() || null,
      models,
    };
    if (credentialValue.trim()) fields.credential_value = credentialValue.trim();
    if (extra.trim()) {
      try {
        fields.extra = JSON.parse(extra);
      } catch {
        toast.error("Extra must be valid JSON");
        return;
      }
    }
    try {
      if (endpoint) {
        await updateEndpoint.mutateAsync({ id: endpoint.id, body: fields });
        toast.success("Endpoint updated");
      } else {
        await createEndpoint.mutateAsync({ providerId, body: fields });
        toast.success("Endpoint added");
      }
      onClose();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not save endpoint");
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={title}
      size="md"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" loading={busy} onClick={save}>Save</Button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="grid grid-cols-2 gap-3">
          <Field label="Label">
            <Input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="Anthropic-compatible" />
          </Field>
          <Field label="Protocol">
            <Select value={protocolKind} onChange={(e) => setProtocolKind(e.target.value as ProtocolKind)}>
              {Object.entries(PROTOCOL_LABEL).map(([k, v]) => (
                <option key={k} value={k}>{v}</option>
              ))}
            </Select>
          </Field>
        </div>
        <Field label="Base URL" hint="Leave blank where the protocol implies it (e.g. OpenAI Codex).">
          <Input mono value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://api.z.ai/api/anthropic" />
        </Field>
        {!canListModels && (
          <div className="rounded-card border border-lamp/40 bg-lamp/5 px-3 py-2.5">
            <p className="text-xs font-medium text-moon-100">
              {PROTOCOL_LABEL[protocolKind]} has no model list
            </p>
            <p className="mt-0.5 text-xs text-moon-400">
              This protocol has no list operation to pull from, so its models are curated by hand. Edit the list
              below — sensible defaults are pre-filled where known.
            </p>
            <div className="mt-2.5">
              <Field label="Models">
                <ListEditor value={models} onChange={setModels} placeholder="gpt-5.4" />
              </Field>
            </div>
          </div>
        )}
        <div className="grid grid-cols-2 gap-3">
          <Field label="Credential source">
            <Select value={credentialSource} onChange={(e) => setCredentialSource(e.target.value as CredentialSource)}>
              {Object.entries(CREDENTIAL_SOURCE_LABEL).map(([k, v]) => (
                <option key={k} value={k}>{v}</option>
              ))}
            </Select>
          </Field>
          <Field label="Harness lock" hint="Restrict this endpoint to one harness (vendor ToS).">
            <Select value={harnessLock} onChange={(e) => setHarnessLock(e.target.value)}>
              <option value="">None</option>
              <option value="claude_sdk">Claude Code</option>
            </Select>
          </Field>
        </div>
        <Field
          label={isPathCredential(credentialSource) ? "Credential file path" : "Credential value"}
          hint={
            endpoint
              ? endpoint.credential_set
                ? "Credential is set. Leave blank to keep it, or enter a new value to replace it."
                : "No credential set."
              : credentialSource === "none"
                ? undefined
                : "Stored encrypted; never shown again."
          }
        >
          <Input
            mono
            type={isPathCredential(credentialSource) ? "text" : "password"}
            value={credentialValue}
            onChange={(e) => setCredentialValue(e.target.value)}
            placeholder={isPathCredential(credentialSource) ? DEFAULT_CREDENTIAL_PATH[credentialSource] : "sk-…"}
            disabled={credentialSource === "none"}
          />
        </Field>
        <Field label="Default model">
          <Input mono value={defaultModel} onChange={(e) => setDefaultModel(e.target.value)} placeholder="glm-5.2" />
        </Field>
        {canListModels && (
          <Field label="Models" hint="Curated manually, or filled by Refresh models where the protocol supports it.">
            <ListEditor value={models} onChange={setModels} placeholder="glm-5.2" />
          </Field>
        )}
        <Field
          label="Extra (JSON)"
          hint={endpoint?.extra_set ? "Currently set. Paste new JSON to replace it; leave blank to keep it." : "Vendor-quirk overrides (headers, env, options)."}
        >
          <Textarea mono className="min-h-[70px]" value={extra} onChange={(e) => setExtra(e.target.value)} placeholder='{ "env": { "X-Routing": "..." } }' />
        </Field>
      </div>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Create-provider wizard
// ---------------------------------------------------------------------------

// Two steps: pick the provider type, then fill in the detail form for that
// type only (credential + endpoints). Merging what used to be a separate
// "credential" step and "endpoints" step keeps the whole per-type form on
// one screen instead of forcing an extra click through fields that were
// already committed to a single offering.
type WizardStep = "type" | "detail";

interface DraftEndpoint {
  key: string;
  checked: boolean;
  label: string;
  protocol_kind: ProtocolKind;
  base_url: string;
  credential_source: CredentialSource;
  harness_lock: string | null;
  credential_override: string;
  default_model: string;
  models: string[];
}

function CreateProviderDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const catalog = useProviderCatalog();
  const protocols = useProviderProtocols();
  const createProvider = useCreateProvider();
  const [step, setStep] = useState<WizardStep>("type");
  const [offering, setOffering] = useState<CatalogOfferingOut | "custom" | null>(null);
  const [name, setName] = useState("");
  const [customVendorTag, setCustomVendorTag] = useState("");
  // The shared credential for a catalog offering: every endpoint under an
  // offering has the same credential_source, so there is exactly one input
  // here — never a choice between a pasted secret and a file path.
  const [sharedCredential, setSharedCredential] = useState("");
  const [endpoints, setEndpoints] = useState<DraftEndpoint[]>([]);

  function isSameOffering(a: CatalogOfferingOut | "custom" | null, b: CatalogOfferingOut | "custom") {
    if (a === "custom" || b === "custom") return a === b;
    return a !== null && a.key === b.key;
  }

  // Advances to the detail step. Re-picking the *same* type (e.g. hitting
  // Back from step 2 just to glance at the list, then re-selecting it) must
  // not clobber whatever the user already typed — only reset the draft when
  // the chosen type actually changed.
  function pickOffering(o: CatalogOfferingOut | "custom") {
    if (!isSameOffering(offering, o)) {
      setOffering(o);
      if (o === "custom") {
        setSharedCredential("");
        setName("");
        setCustomVendorTag("");
        setEndpoints([
          {
            key: "custom-0", checked: true, label: "Custom endpoint",
            protocol_kind: "openai_compat", base_url: "", credential_source: "api_key",
            harness_lock: null, credential_override: "", default_model: "", models: [],
          },
        ]);
      } else {
        // File-path credential offerings (oauth_file / subscription_file)
        // seed a REAL, editable value here — not just a greyed placeholder —
        // so an untouched "Create" click doesn't silently store a NULL
        // credential (see the create-route backstop for the belt-and-braces
        // server-side default of the same hint).
        setSharedCredential(
          isPathCredential(o.credential_source)
            ? o.credential_hint ?? DEFAULT_CREDENTIAL_PATH[o.credential_source] ?? ""
            : "",
        );
        setName(o.suggested_name || o.label);
        setEndpoints(
          o.endpoints.map((e, i) => ({
            key: `${o.key}-${i}`,
            checked: true,
            label: e.label,
            protocol_kind: e.protocol_kind,
            base_url: e.base_url ?? "",
            credential_source: o.credential_source,
            harness_lock: e.harness_lock,
            credential_override: "",
            default_model: e.default_model ?? "",
            models: e.models,
          })),
        );
      }
    }
    setStep("detail");
  }

  function updateEndpoint(key: string, patch: Partial<DraftEndpoint>) {
    setEndpoints((prev) => prev.map((e) => (e.key === key ? { ...e, ...patch } : e)));
  }

  // Switching a draft endpoint's protocol to one with no list operation
  // (Codex today): seed the manual model menu from the catalog instead of
  // leaving it blank, same as the standalone endpoint form. Never
  // overwrites models the user already entered.
  function setEndpointProtocol(key: string, kind: ProtocolKind) {
    setEndpoints((prev) =>
      prev.map((e) => {
        if (e.key !== key) return e;
        if (protocolSupportsModelList(protocols.data, kind) || e.models.length > 0) {
          return { ...e, protocol_kind: kind };
        }
        const suggestion = catalogModelSuggestion(catalog.data, kind);
        return suggestion
          ? { ...e, protocol_kind: kind, models: suggestion.models, default_model: e.default_model || suggestion.default_model || "" }
          : { ...e, protocol_kind: kind };
      }),
    );
  }

  // Custom offerings register exactly one endpoint from the wizard, so its
  // credential mode (radio + single input) lives on that one draft entry.
  const customEndpoint = offering === "custom" ? endpoints[0] : undefined;

  async function create() {
    if (!name.trim()) {
      toast.error("Name is required");
      return;
    }
    const chosen = endpoints.filter((e) => e.checked);
    if (chosen.length === 0) {
      toast.error("Select at least one endpoint");
      return;
    }
    try {
      const created = await createProvider.mutateAsync({
        name: name.trim(),
        vendor: offering === "custom" ? (customVendorTag.trim() || "custom") : offering!.vendor,
        endpoints: chosen.map((e) => ({
          label: e.label,
          protocol_kind: e.protocol_kind,
          base_url: e.base_url.trim() || null,
          credential_source: e.credential_source,
          harness_lock: e.harness_lock,
          credential_value:
            (offering === "custom" ? e.credential_override.trim() : sharedCredential.trim()) || undefined,
          default_model: e.default_model.trim() || null,
          models: e.models,
        })),
      });
      toast.success("Provider created", {
        description: "Refresh models on any capable endpoint to pull its model menu.",
      });
      onCreated(created.id);
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not create provider");
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="Add provider"
      description={
        step === "type"
          ? "Pick a known offering, or configure a custom one."
          : offering === "custom"
            ? "Manual protocol, credential, and endpoints."
            : "Every offering has exactly one credential mode — enter it once, then confirm its endpoints."
      }
      size="md"
      footer={
        step === "type" ? (
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
        ) : (
          <>
            <Button variant="ghost" onClick={() => setStep("type")}>Back</Button>
            <Button variant="primary" loading={createProvider.isPending} disabled={!name.trim()} onClick={create}>
              Create
            </Button>
          </>
        )
      }
    >
      {step === "type" && (
        <div className="space-y-1.5">
          {catalog.isLoading ? (
            <div className="flex items-center gap-2 text-sm text-moon-400"><Spinner size={14} /> Loading catalog…</div>
          ) : (
            <>
              {(catalog.data ?? []).map((o) => (
                <button
                  key={o.key}
                  type="button"
                  aria-pressed={isSameOffering(offering, o)}
                  onClick={() => pickOffering(o)}
                  className={cn(
                    "flex w-full items-center justify-between gap-4 rounded-card border px-4 py-3 text-left transition-colors",
                    isSameOffering(offering, o)
                      ? "border-lamp/50 bg-ink-800"
                      : "border-ink-700 bg-ink-900 hover:border-lamp/50 hover:bg-ink-800",
                  )}
                >
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-moon-100">{o.label}</div>
                    {o.description && <div className="mt-0.5 truncate text-xs text-moon-600">{o.description}</div>}
                  </div>
                  <Badge tone="neutral" mono className="shrink-0">{CREDENTIAL_SOURCE_LABEL[o.credential_source]}</Badge>
                </button>
              ))}
              <button
                type="button"
                aria-pressed={offering === "custom"}
                onClick={() => pickOffering("custom")}
                className={cn(
                  "flex w-full items-center justify-between gap-4 rounded-card border border-dashed px-4 py-3 text-left transition-colors",
                  offering === "custom"
                    ? "border-lamp/50 bg-ink-800"
                    : "border-ink-700 bg-ink-900/50 hover:border-lamp/50 hover:bg-ink-800",
                )}
              >
                <div className="min-w-0">
                  <div className="text-sm font-medium text-moon-100">Custom</div>
                  <div className="mt-0.5 text-xs text-moon-600">Manual protocol, URL, credential</div>
                </div>
              </button>
            </>
          )}
        </div>
      )}

      {step === "detail" && offering && offering !== "custom" && (
        <div className="space-y-4">
          <Field label="Name">
            <Input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder={offering.suggested_name} />
          </Field>

          {offering.credential_source === "none" ? (
            <div className="rounded-card border border-ink-700 bg-ink-900/40 px-3 py-2.5 text-xs text-moon-600">
              No credential needed for this offering.
            </div>
          ) : (
            <Field
              label={isPathCredential(offering.credential_source) ? "Credential file path" : CREDENTIAL_SOURCE_LABEL[offering.credential_source]}
              hint={`Seeded into ${endpoints.length} endpoint${endpoints.length === 1 ? "" : "s"}.`}
            >
              <Input
                mono
                type={isPathCredential(offering.credential_source) ? "text" : "password"}
                value={sharedCredential}
                onChange={(e) => setSharedCredential(e.target.value)}
                placeholder={
                  isPathCredential(offering.credential_source)
                    ? (offering.credential_hint ?? DEFAULT_CREDENTIAL_PATH[offering.credential_source])
                    : "sk-…"
                }
              />
            </Field>
          )}

          <EndpointsFieldset endpoints={endpoints} protocols={protocols.data} updateEndpoint={updateEndpoint} setEndpointProtocol={setEndpointProtocol} />
        </div>
      )}

      {step === "detail" && offering === "custom" && customEndpoint && (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Name">
              <Input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="My provider" />
            </Field>
            <Field label="Vendor tag" hint="Pricing key, e.g. openrouter">
              <Input mono value={customVendorTag} onChange={(e) => setCustomVendorTag(e.target.value)} placeholder="custom" />
            </Field>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <Field label="Credential mode" hint="Pick exactly one — the input below matches your choice.">
              <Select
                value={customEndpoint.credential_source}
                onChange={(e) =>
                  updateEndpoint(customEndpoint.key, {
                    credential_source: e.target.value as CredentialSource,
                    credential_override: "",
                  })
                }
              >
                {Object.entries(CREDENTIAL_SOURCE_LABEL).map(([k, v]) => (
                  <option key={k} value={k}>{v}</option>
                ))}
              </Select>
            </Field>

            {customEndpoint.credential_source === "none" ? (
              <div className="flex items-end pb-2.5 text-xs text-moon-600">
                No credential needed for this endpoint.
              </div>
            ) : (
              <Field label={isPathCredential(customEndpoint.credential_source) ? "Credential file path" : "Credential value"}>
                <Input
                  mono
                  type={isPathCredential(customEndpoint.credential_source) ? "text" : "password"}
                  value={customEndpoint.credential_override}
                  onChange={(e) => updateEndpoint(customEndpoint.key, { credential_override: e.target.value })}
                  placeholder={
                    isPathCredential(customEndpoint.credential_source)
                      ? DEFAULT_CREDENTIAL_PATH[customEndpoint.credential_source]
                      : "sk-…"
                  }
                />
              </Field>
            )}
          </div>

          <EndpointsFieldset endpoints={endpoints} protocols={protocols.data} updateEndpoint={updateEndpoint} setEndpointProtocol={setEndpointProtocol} />
        </div>
      )}
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Draft-endpoint checklist shared by both branches of the create-provider
// detail step (catalog offering and custom) — same row shape either way, so
// the field grid stays identical regardless of which type was picked.
// ---------------------------------------------------------------------------

function EndpointsFieldset({
  endpoints,
  protocols,
  updateEndpoint,
  setEndpointProtocol,
}: {
  endpoints: DraftEndpoint[];
  protocols: ProtocolInfoOut[] | undefined;
  updateEndpoint: (key: string, patch: Partial<DraftEndpoint>) => void;
  setEndpointProtocol: (key: string, kind: ProtocolKind) => void;
}) {
  return (
    <div className="space-y-2.5">
      <h4 className="text-xs font-medium uppercase tracking-wide text-moon-600">
        Endpoint{endpoints.length === 1 ? "" : "s"}
      </h4>
      {endpoints.map((e) => (
        <div key={e.key} className="rounded-card border border-ink-700 bg-ink-900 px-3.5 py-3">
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={e.checked}
              onChange={(ev) => updateEndpoint(e.key, { checked: ev.target.checked })}
              className="h-3.5 w-3.5 rounded border-ink-700 bg-ink-950 accent-lamp"
            />
            <span className="text-sm font-medium text-moon-100">{e.label}</span>
            {e.harness_lock && (
              <Badge tone="review" dot>
                <Lock size={10} /> {HARNESS_LABEL[e.harness_lock] ?? e.harness_lock} only
              </Badge>
            )}
          </label>
          {e.checked && (
            <div className="mt-2.5 space-y-2.5 pl-5.5">
              <div className="grid grid-cols-2 gap-2.5">
                <Field label="Protocol">
                  <Select value={e.protocol_kind} onChange={(ev) => setEndpointProtocol(e.key, ev.target.value as ProtocolKind)}>
                    {Object.entries(PROTOCOL_LABEL).map(([k, v]) => (
                      <option key={k} value={k}>{v}</option>
                    ))}
                  </Select>
                </Field>
                <Field label="Base URL">
                  <Input mono value={e.base_url} onChange={(ev) => updateEndpoint(e.key, { base_url: ev.target.value })} placeholder="(none)" />
                </Field>
              </div>
              {!protocolSupportsModelList(protocols, e.protocol_kind) && (
                <div className="rounded-card border border-lamp/40 bg-lamp/5 px-3 py-2.5">
                  <p className="text-xs font-medium text-moon-100">
                    {PROTOCOL_LABEL[e.protocol_kind]} has no model list
                  </p>
                  <p className="mt-0.5 text-xs text-moon-400">
                    This protocol has no list operation to pull from, so its models are curated by hand.
                    Sensible defaults are pre-filled below — add or remove ids as needed.
                  </p>
                  <div className="mt-2.5">
                    <Field label="Models">
                      <ListEditor
                        value={e.models}
                        onChange={(next) => updateEndpoint(e.key, { models: next })}
                        placeholder="gpt-5.4"
                      />
                    </Field>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
