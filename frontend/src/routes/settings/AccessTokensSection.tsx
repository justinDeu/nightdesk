import { useMemo, useState } from "react";
import { Check, ChevronDown, ChevronRight, Copy, KeyRound, Lock, Plus, Ban, Trash2 } from "lucide-react";
import { Button } from "@/ui/Button";
import { IconButton } from "@/ui/IconButton";
import { Input, Field } from "@/ui/Input";
import { Dialog } from "@/ui/Dialog";
import { Badge } from "@/ui/Badge";
import { Spinner } from "@/ui/Spinner";
import { EmptyState } from "@/ui/EmptyState";
import { Tooltip } from "@/ui/Tooltip";
import { toast } from "@/ui/Toast";
import { cn } from "@/lib/cn";
import {
  tokensApi,
  useTokens,
  useTokenCatalog,
  useRevokeToken,
  useDeleteToken,
  type TokenOut,
  type TokenMintResult,
  type TokenCatalog,
  type ScopeInfo,
} from "@/api/tokens";
import { SectionHeader } from "./parts/SettingsSection";
import { ConfirmDialog } from "./parts/ConfirmDialog";

// Fallback preset blurbs for historical bundle names the catalog might not
// carry (the live descriptions come from the API — see bundleDescription).
const BUNDLE_BLURB: Record<string, string> = {
  observer: "Read-only across the board, runs, and analytics.",
  reviewer: "Observer plus posting review comments.",
  "pm-agent": "Create, update, transition, and archive tickets. No run-now or config.",
  operator: "PM-agent plus immediate run-now and firing cron jobs.",
};

/** name → one-line scope description, from the mint catalog. */
function scopeDescriptions(catalog: TokenCatalog | undefined): Map<string, string> {
  const m = new Map<string, string>();
  for (const s of catalog?.scopes ?? []) m.set(s.name, s.description);
  return m;
}

/** Live preset description, falling back to the local blurb map. */
function bundleDescription(catalog: TokenCatalog | undefined, name: string): string {
  return catalog?.bundles.find((b) => b.name === name)?.description ?? BUNDLE_BLURB[name] ?? "";
}

/** A scope identifier chip; on hover it explains what the scope gates. */
function ScopeChip({ scope, description }: { scope: string; description?: string }) {
  const chip = (
    <span className="inline-block rounded-control border border-ink-700 bg-ink-800 px-2 py-0.5 font-mono text-[11px] text-moon-100">
      {scope}
    </span>
  );
  return description ? <Tooltip content={description}>{chip}</Tooltip> : chip;
}

// Mint-form scope sections, derived from the scope-name prefix (the part before
// the dot — the API contract; no backend field). Everyday agent permissions
// first, admin/human-only-heavy ones last. Any prefix not listed here falls
// into a trailing "Other" section rather than being dropped.
const SCOPE_SECTIONS: Array<{ prefix: string; label: string }> = [
  { prefix: "tickets", label: "Tickets" },
  { prefix: "runs", label: "Runs" },
  { prefix: "comments", label: "Comments" },
  { prefix: "projects", label: "Projects" },
  { prefix: "labels", label: "Labels" },
  { prefix: "analytics", label: "Analytics" },
  { prefix: "integrations", label: "Integrations" },
  { prefix: "fs", label: "Filesystem" },
  { prefix: "agents", label: "Agents" },
  { prefix: "cron", label: "Cron" },
  { prefix: "profiles", label: "Profiles" },
  { prefix: "providers", label: "Providers" },
  { prefix: "config", label: "Configuration" },
  { prefix: "tokens", label: "Token management" },
];

const _OTHER_KEY = "__other__";

/** Bucket catalog scopes into ordered sections by prefix, preserving catalog
 *  order within each section. Unknown prefixes collapse into one trailing
 *  "Other" section. */
function sectionizeScopes(scopes: ScopeInfo[]): Array<{ label: string; scopes: ScopeInfo[] }> {
  const rank = new Map(SCOPE_SECTIONS.map((s, i) => [s.prefix, i]));
  const labelOf = new Map(SCOPE_SECTIONS.map((s) => [s.prefix, s.label]));
  const buckets = new Map<string, ScopeInfo[]>();
  for (const info of scopes) {
    const prefix = info.name.split(".")[0] ?? info.name;
    const key = rank.has(prefix) ? prefix : _OTHER_KEY;
    const bucket = buckets.get(key);
    if (bucket) bucket.push(info);
    else buckets.set(key, [info]);
  }
  return [...buckets.entries()]
    .sort((a, b) => (rank.get(a[0]) ?? Infinity) - (rank.get(b[0]) ?? Infinity))
    .map(([key, s]) => ({ label: key === _OTHER_KEY ? "Other" : labelOf.get(key) ?? "Other", scopes: s }));
}

/** One scope checkbox row: identifier + inline description, with the human-only
 *  Lock + tooltip treatment. */
function ScopeCheckItem({
  info,
  checked,
  onToggle,
}: {
  info: ScopeInfo;
  checked: boolean;
  onToggle: () => void;
}) {
  const disabled = info.human_only;
  return (
    <label
      className={cn(
        "flex items-start gap-2 rounded-control px-2 py-1.5",
        disabled ? "cursor-not-allowed" : "cursor-pointer hover:bg-ink-800",
      )}
    >
      <input
        type="checkbox"
        checked={disabled ? false : checked}
        disabled={disabled}
        onChange={onToggle}
        className="mt-0.5 h-3.5 w-3.5 shrink-0 accent-lamp"
      />
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          <span className={cn("font-mono text-xs", disabled ? "text-moon-600" : "text-moon-100")}>
            {info.name}
          </span>
          {disabled && (
            <Tooltip content="Human-only: requires the admin session; never grantable to a token.">
              <Lock size={11} className="text-moon-600" />
            </Tooltip>
          )}
        </span>
        {info.description && (
          <span className="mt-0.5 block text-[11px] leading-snug text-moon-600">
            {info.description}
          </span>
        )}
      </span>
    </label>
  );
}

// Scope strings are "resource.action" — grouping by resource matches how the
// mint dialog's catalog is ordered (contiguous per resource; see
// domain/scopes.py ALL_SCOPES). Labels for the resources with non-obvious
// capitalization; everything else falls back to a capitalized first letter.
const RESOURCE_LABEL: Record<string, string> = {
  fs: "Filesystem",
};

function resourceOf(scope: string): string {
  return scope.split(".")[0] ?? scope;
}

function resourceLabel(resource: string): string {
  return RESOURCE_LABEL[resource] ?? resource.charAt(0).toUpperCase() + resource.slice(1);
}

/** Group scopes by resource, ordered per the catalog's ALL_SCOPES sequence
 *  (falling back to first-seen order for any scope the catalog hasn't loaded
 *  yet or doesn't recognize) so the layout matches the mint dialog. */
function groupScopes(
  scopes: string[],
  catalogOrder: string[] | undefined,
): Array<{ resource: string; scopes: string[] }> {
  const order = catalogOrder ?? scopes;
  const rank = new Map<string, number>();
  order.forEach((s, i) => rank.set(resourceOf(s), rank.get(resourceOf(s)) ?? i));

  const byResource = new Map<string, string[]>();
  for (const scope of scopes) {
    const resource = resourceOf(scope);
    const bucket = byResource.get(resource);
    if (bucket) bucket.push(scope);
    else byResource.set(resource, [scope]);
  }

  return [...byResource.entries()]
    .sort((a, b) => (rank.get(a[0]) ?? Infinity) - (rank.get(b[0]) ?? Infinity))
    .map(([resource, s]) => ({ resource, scopes: s }));
}

function relTime(iso: string | null): string {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  const secs = Math.round((Date.now() - then) / 1000);
  if (secs < 60) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.round(hrs / 24);
  return `${days}d ago`;
}

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export function AccessTokensSection() {
  const tokens = useTokens();
  const catalog = useTokenCatalog();
  const [minting, setMinting] = useState(false);
  const [minted, setMinted] = useState<TokenMintResult | null>(null);
  const [toRevoke, setToRevoke] = useState<TokenOut | null>(null);
  const [toDelete, setToDelete] = useState<TokenOut | null>(null);
  const revoke = useRevokeToken();
  const del = useDeleteToken();

  const active = (tokens.data ?? []).filter((t) => !t.revoked_at);
  const revoked = (tokens.data ?? []).filter((t) => t.revoked_at);

  async function confirmRevoke() {
    if (!toRevoke) return;
    try {
      await revoke.mutateAsync(toRevoke.id);
      toast.success(`Revoked “${toRevoke.name}”`);
      setToRevoke(null);
    } catch (err) {
      toast.error("Could not revoke token", { error: err });
    }
  }

  async function confirmDelete() {
    if (!toDelete) return;
    try {
      await del.mutateAsync(toDelete.id);
      toast.success(`Deleted “${toDelete.name}”`);
      setToDelete(null);
    } catch (err) {
      toast.error("Could not delete token", { error: err });
    }
  }

  return (
    <div>
      <SectionHeader
        title="Access tokens"
        description="Scoped API tokens for agents. Each token's blast radius is the scopes you grant it — never the admin bearer."
        actions={
          <Button variant="primary" size="sm" leadingIcon={<Plus size={14} />} onClick={() => setMinting(true)}>
            New token
          </Button>
        }
      />

      {tokens.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-moon-400">
          <Spinner /> Loading tokens…
        </div>
      ) : active.length === 0 && revoked.length === 0 ? (
        <EmptyState
          icon={<KeyRound size={18} />}
          title="No access tokens yet"
          description="Mint a scoped token an agent can export as NIGHTDESK_TOKEN — instead of reading the admin bearer."
          action={
            <Button variant="ghost" leadingIcon={<Plus size={14} />} onClick={() => setMinting(true)}>
              New token
            </Button>
          }
        />
      ) : (
        <div className="space-y-6">
          <TokenList
            tokens={active}
            catalog={catalog.data}
            onRevoke={setToRevoke}
            onDelete={setToDelete}
          />
          {revoked.length > 0 && (
            <div>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-moon-600">
                Revoked
              </h3>
              <TokenList
                tokens={revoked}
                catalog={catalog.data}
                onRevoke={setToRevoke}
                onDelete={setToDelete}
              />
            </div>
          )}
        </div>
      )}

      {minting && (
        <MintDialog
          onClose={() => setMinting(false)}
          onMinted={(m) => {
            setMinting(false);
            setMinted(m);
            tokens.refetch();
          }}
        />
      )}

      {minted && <RevealDialog token={minted} onClose={() => setMinted(null)} />}

      <ConfirmDialog
        open={!!toRevoke}
        onOpenChange={(o) => !o && setToRevoke(null)}
        title={`Revoke “${toRevoke?.name}”?`}
        body="The token stops working immediately. Any agent using it will start getting 401s. This cannot be undone."
        confirmLabel="Revoke token"
        danger
        busy={revoke.isPending}
        onConfirm={confirmRevoke}
      />

      <ConfirmDialog
        open={!!toDelete}
        onOpenChange={(o) => !o && setToDelete(null)}
        title={`Delete “${toDelete?.name}”?`}
        body="Removes the token row entirely, dropping its audit trail. Prefer revoking if you want to keep the record."
        confirmLabel="Delete token"
        danger
        busy={del.isPending}
        onConfirm={confirmDelete}
      />
    </div>
  );
}

function TokenList({
  tokens,
  catalog,
  onRevoke,
  onDelete,
}: {
  tokens: TokenOut[];
  catalog: TokenCatalog | undefined;
  onRevoke: (t: TokenOut) => void;
  onDelete: (t: TokenOut) => void;
}) {
  return (
    <ul className="space-y-2">
      {tokens.map((t) => (
        <TokenRow key={t.id} token={t} catalog={catalog} onRevoke={onRevoke} onDelete={onDelete} />
      ))}
    </ul>
  );
}

function TokenRow({
  token: t,
  catalog,
  onRevoke,
  onDelete,
}: {
  token: TokenOut;
  catalog: TokenCatalog | undefined;
  onRevoke: (t: TokenOut) => void;
  onDelete: (t: TokenOut) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const isRevoked = !!t.revoked_at;
  const expired = t.expires_at && new Date(t.expires_at).getTime() < Date.now();
  const catalogOrder = useMemo(() => catalog?.scopes.map((s) => s.name), [catalog]);
  const descriptions = useMemo(() => scopeDescriptions(catalog), [catalog]);
  const groups = useMemo(
    () => groupScopes(t.scopes, catalogOrder),
    [t.scopes, catalogOrder],
  );
  const profileAllowlist = asStringList(t.scope_data?.profile_allowlist);
  const projectAllowlist = asStringList(t.scope_data?.project_allowlist);

  return (
    <li
      className={cn(
        "rounded-card border border-ink-700 bg-ink-900",
        isRevoked && "opacity-60",
      )}
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span
              className={cn(
                "truncate text-sm font-medium text-moon-100",
                isRevoked && "line-through",
              )}
            >
              {t.name}
            </span>
            {t.bundle && <Badge tone="lamp">{t.bundle}</Badge>}
            {t.kind === "run" && <Badge tone="neutral">run</Badge>}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-moon-600">
            <span className="font-mono text-moon-400">{t.prefix_hint}…</span>
            <span>created {fmtDate(t.created_at)}</span>
            <span>
              {expired ? "expired" : "expires"} {t.expires_at ? fmtDate(t.expires_at) : "never"}
            </span>
            <span>last used {relTime(t.last_used_at)}</span>
          </div>
        </div>
        {!isRevoked && (
          <IconButton
            label="Revoke"
            size="sm"
            icon={<Ban size={14} />}
            onClick={() => onRevoke(t)}
            className="hover:text-failed"
          />
        )}
        <IconButton
          label="Delete"
          size="sm"
          icon={<Trash2 size={14} />}
          onClick={() => onDelete(t)}
          className="hover:text-failed"
        />
      </div>

      <div className="border-t border-ink-700/70 px-4 py-2">
        <button
          type="button"
          onClick={() => setExpanded((e) => !e)}
          aria-expanded={expanded}
          className="flex items-center gap-1 text-xs text-moon-400 hover:text-moon-100"
        >
          {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
          {t.scopes.length} scope{t.scopes.length === 1 ? "" : "s"}
          {profileAllowlist.length > 0 && (
            <span className="text-moon-600">
              · {profileAllowlist.length} profile{profileAllowlist.length === 1 ? "" : "s"} allowed
            </span>
          )}
          {projectAllowlist.length > 0 && (
            <span className="text-moon-600">
              · {projectAllowlist.length} project{projectAllowlist.length === 1 ? "" : "s"} allowed
            </span>
          )}
        </button>

        {expanded && (
          <div className="mt-3 space-y-3">
            {t.bundle && (
              <p className="text-xs text-moon-600">
                <span className="font-medium text-moon-400">{t.bundle}</span>
                {bundleDescription(catalog, t.bundle) ? ` — ${bundleDescription(catalog, t.bundle)}` : ""}
                <span className="text-moon-600"> · snapshot at mint, not a live link</span>
              </p>
            )}

            {t.scopes.length === 0 ? (
              <p className="text-xs text-moon-600">No scopes recorded.</p>
            ) : (
              <div className="grid gap-3 sm:grid-cols-2">
                {groups.map((g) => (
                  <div key={g.resource}>
                    <h4 className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-moon-600">
                      {resourceLabel(g.resource)}
                    </h4>
                    <ul className="flex flex-wrap gap-1.5">
                      {g.scopes.map((scope) => (
                        <li key={scope}>
                          <ScopeChip scope={scope} description={descriptions.get(scope)} />
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            )}

            {(profileAllowlist.length > 0 || projectAllowlist.length > 0) && (
              <div className="grid gap-3 sm:grid-cols-2">
                {profileAllowlist.length > 0 && (
                  <AllowlistGroup label="Profile allowlist" values={profileAllowlist} />
                )}
                {projectAllowlist.length > 0 && (
                  <AllowlistGroup label="Project allowlist" values={projectAllowlist} />
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </li>
  );
}

function AllowlistGroup({ label, values }: { label: string; values: string[] }) {
  return (
    <div>
      <h4 className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-moon-600">
        {label}
      </h4>
      <ul className="flex flex-wrap gap-1.5">
        {values.map((v) => (
          <li
            key={v}
            className="rounded-control border border-ink-700 bg-ink-800 px-2 py-0.5 font-mono text-[11px] text-moon-100"
          >
            {v}
          </li>
        ))}
      </ul>
    </div>
  );
}

function asStringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

function MintDialog({
  onClose,
  onMinted,
}: {
  onClose: () => void;
  onMinted: (m: TokenMintResult) => void;
}) {
  const catalog = useTokenCatalog();
  const [name, setName] = useState("");
  const [bundle, setBundle] = useState<string>("observer");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [expiryDays, setExpiryDays] = useState<string>("90");
  const [busy, setBusy] = useState(false);
  const [touched, setTouched] = useState(false);

  const humanOnly = useMemo(
    () => new Set((catalog.data?.scopes ?? []).filter((s) => s.human_only).map((s) => s.name)),
    [catalog.data],
  );
  const descriptions = useMemo(() => scopeDescriptions(catalog.data), [catalog.data]);

  const bundleScopesOf = (name: string): string[] =>
    catalog.data?.bundles.find((b) => b.name === name)?.scopes ?? [];

  // Until the user tweaks the checklist, the selection tracks the bundle.
  const effectiveScopes = useMemo(() => {
    if (touched) return selected;
    return new Set(bundleScopesOf(bundle));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [touched, selected, bundle, catalog.data]);

  function toggle(scope: string) {
    if (humanOnly.has(scope)) return;
    const next = new Set(effectiveScopes);
    if (next.has(scope)) next.delete(scope);
    else next.add(scope);
    setSelected(next);
    setTouched(true);
  }

  // Select/clear a whole section at once (skipping human-only scopes).
  function setSectionScopes(scopeNames: string[], on: boolean) {
    const next = new Set(effectiveScopes);
    for (const s of scopeNames) {
      if (humanOnly.has(s)) continue;
      if (on) next.add(s);
      else next.delete(s);
    }
    setSelected(next);
    setTouched(true);
  }

  function pickBundle(b: string) {
    setBundle(b);
    setTouched(false);
  }

  async function mint() {
    const scopes = [...effectiveScopes];
    if (!name.trim() || scopes.length === 0) return;
    setBusy(true);
    try {
      const body = {
        name: name.trim(),
        bundle: touched ? null : bundle,
        scopes: touched ? scopes : null,
        expires_in_days: expiryDays.trim() ? Number(expiryDays) : null,
      };
      const result = await tokensApi.mint(body);
      onMinted(result);
    } catch (err) {
      toast.error("Could not mint token", { error: err });
    } finally {
      setBusy(false);
    }
  }

  const allScopes = catalog.data?.scopes ?? [];
  const scopeSections = useMemo(() => sectionizeScopes(allScopes), [allScopes]);

  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="New access token"
      size="lg"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button
            variant="primary"
            loading={busy}
            disabled={!name.trim() || effectiveScopes.size === 0}
            onClick={mint}
          >
            Mint token
          </Button>
        </>
      }
    >
      {catalog.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-moon-400">
          <Spinner /> Loading scopes…
        </div>
      ) : (
        <div className="space-y-5">
          <Field label="Name">
            <Input
              autoFocus
              placeholder="pm-agent"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>

          <div>
            <label className="mb-1.5 block text-sm font-medium text-moon-100">Preset</label>
            <div className="grid gap-2 sm:grid-cols-2">
              {(catalog.data?.bundles ?? []).map((b) => (
                <button
                  key={b.name}
                  type="button"
                  onClick={() => pickBundle(b.name)}
                  className={cn(
                    "rounded-card border px-3 py-2 text-left transition-colors",
                    bundle === b.name && !touched
                      ? "border-lamp bg-lamp/10"
                      : "border-ink-700 bg-ink-900 hover:border-ink-600",
                  )}
                >
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-moon-100">{b.name}</span>
                    <span className="ml-auto font-mono text-[11px] text-moon-600">
                      {b.scopes.length} scope{b.scopes.length === 1 ? "" : "s"}
                    </span>
                  </div>
                  <div className="mt-0.5 text-xs text-moon-600">{b.description}</div>
                </button>
              ))}
            </div>
            {!touched && (
              <div className="mt-2 rounded-card border border-ink-700 bg-ink-900 px-3 py-2">
                <p className="text-[11px] text-moon-600">
                  <span className="font-medium text-moon-400">{bundle}</span> expands to:
                </p>
                <ul className="mt-1.5 flex flex-wrap gap-1.5">
                  {bundleScopesOf(bundle).map((scope) => (
                    <li key={scope}>
                      <ScopeChip scope={scope} description={descriptions.get(scope)} />
                    </li>
                  ))}
                </ul>
              </div>
            )}
            <p className="mt-1.5 text-xs text-moon-600">
              Presets are starting points — tweak the scopes below and the token is minted
              with exactly what's checked. A bundle is a snapshot taken at mint time, not a
              live link: editing a preset later never changes tokens already minted from it.
            </p>
          </div>

          <Field label="Expires in (days)">
            <Input
              type="number"
              min={1}
              placeholder="90 — leave blank for no expiry"
              value={expiryDays}
              onChange={(e) => setExpiryDays(e.target.value)}
              className="max-w-[220px]"
            />
          </Field>

          <div>
            <label className="mb-1.5 block text-sm font-medium text-moon-100">
              Scopes ({effectiveScopes.size})
            </label>
            <div className="max-h-[360px] space-y-2 overflow-y-auto rounded-card border border-ink-700 bg-ink-950 p-2">
              {scopeSections.map((section) => {
                const selectable = section.scopes.filter((s) => !s.human_only);
                const allOn =
                  selectable.length > 0 && selectable.every((s) => effectiveScopes.has(s.name));
                return (
                  <div
                    key={section.label}
                    className="rounded-control border border-ink-700/70 bg-ink-900 p-2"
                  >
                    <div className="mb-1 flex items-center justify-between gap-2 px-2">
                      <h4 className="text-[11px] font-semibold uppercase tracking-wide text-moon-600">
                        {section.label}
                      </h4>
                      {selectable.length >= 2 && (
                        <button
                          type="button"
                          onClick={() => setSectionScopes(selectable.map((s) => s.name), !allOn)}
                          className="text-[10px] font-medium uppercase tracking-wide text-moon-600 hover:text-moon-100"
                        >
                          {allOn ? "Clear" : "All"}
                        </button>
                      )}
                    </div>
                    <div className="space-y-0.5">
                      {section.scopes.map((info) => (
                        <ScopeCheckItem
                          key={info.name}
                          info={info}
                          checked={effectiveScopes.has(info.name)}
                          onToggle={() => toggle(info.name)}
                        />
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      )}
    </Dialog>
  );
}

function RevealDialog({ token, onClose }: { token: TokenMintResult; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  const exportLine = `export NIGHTDESK_TOKEN=${token.token}`;

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      toast.success("Copied to clipboard");
      setTimeout(() => setCopied(false), 1500);
    } catch (err) {
      toast.error("Could not copy", { error: err });
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={`Token “${token.name}” created`}
      size="md"
      footer={
        <Button variant="primary" onClick={onClose}>
          Done
        </Button>
      }
    >
      <div className="space-y-4">
        <div className="rounded-card border border-lamp/30 bg-lamp/10 px-3 py-2 text-sm text-moon-100">
          Copy this now — you will not be able to see it again.
        </div>

        <div>
          <label className="mb-1 block text-xs font-medium text-moon-400">Token</label>
          <div className="flex items-center gap-2">
            <code className="min-w-0 flex-1 truncate rounded-control border border-ink-700 bg-ink-950 px-3 py-2 font-mono text-xs text-moon-100">
              {token.token}
            </code>
            <IconButton
              label="Copy token"
              icon={copied ? <Check size={14} /> : <Copy size={14} />}
              onClick={() => copy(token.token)}
            />
          </div>
        </div>

        <div>
          <label className="mb-1 block text-xs font-medium text-moon-400">
            Shell export
          </label>
          <div className="flex items-center gap-2">
            <code className="min-w-0 flex-1 truncate rounded-control border border-ink-700 bg-ink-950 px-3 py-2 font-mono text-xs text-moon-400">
              {exportLine}
            </code>
            <IconButton
              label="Copy export line"
              icon={<Copy size={14} />}
              onClick={() => copy(exportLine)}
            />
          </div>
        </div>
      </div>
    </Dialog>
  );
}
