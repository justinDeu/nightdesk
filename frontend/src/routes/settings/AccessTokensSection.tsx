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
} from "@/api/tokens";
import { SectionHeader } from "./parts/SettingsSection";
import { ConfirmDialog } from "./parts/ConfirmDialog";

const BUNDLE_BLURB: Record<string, string> = {
  observer: "Read-only across the board, runs, and analytics.",
  reviewer: "Observer plus posting review comments.",
  "pm-agent": "Create, update, transition, and archive tickets. No run-now or config.",
  operator: "PM-agent plus immediate run-now and firing cron jobs.",
};

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
  const groups = useMemo(
    () => groupScopes(t.scopes, catalog?.scopes),
    [t.scopes, catalog?.scopes],
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
                {BUNDLE_BLURB[t.bundle] ? ` — ${BUNDLE_BLURB[t.bundle]}` : ""}
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
                        <li
                          key={scope}
                          className="rounded-control border border-ink-700 bg-ink-800 px-2 py-0.5 font-mono text-[11px] text-moon-100"
                        >
                          {scope}
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
    () => new Set(catalog.data?.human_only ?? []),
    [catalog.data],
  );

  // Until the user tweaks the checklist, the selection tracks the bundle.
  const effectiveScopes = useMemo(() => {
    if (touched) return selected;
    const preset = catalog.data?.bundles[bundle] ?? [];
    return new Set(preset);
  }, [touched, selected, bundle, catalog.data]);

  function toggle(scope: string) {
    if (humanOnly.has(scope)) return;
    const next = new Set(effectiveScopes);
    if (next.has(scope)) next.delete(scope);
    else next.add(scope);
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
              {Object.keys(catalog.data?.bundles ?? {}).map((b) => (
                <button
                  key={b}
                  type="button"
                  onClick={() => pickBundle(b)}
                  className={cn(
                    "rounded-card border px-3 py-2 text-left transition-colors",
                    bundle === b && !touched
                      ? "border-lamp bg-lamp/10"
                      : "border-ink-700 bg-ink-900 hover:border-ink-600",
                  )}
                >
                  <div className="text-sm font-medium text-moon-100">{b}</div>
                  <div className="mt-0.5 text-xs text-moon-600">{BUNDLE_BLURB[b]}</div>
                </button>
              ))}
            </div>
            <p className="mt-1.5 text-xs text-moon-600">
              Presets are starting points — tweak the scopes below and the token is minted
              with exactly what's checked.
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
            <div className="grid max-h-[280px] gap-1 overflow-y-auto rounded-card border border-ink-700 bg-ink-950 p-2 sm:grid-cols-2">
              {allScopes.map((scope) => {
                const disabled = humanOnly.has(scope);
                const checked = effectiveScopes.has(scope);
                const row = (
                  <label
                    key={scope}
                    className={cn(
                      "flex items-center gap-2 rounded-control px-2 py-1.5 text-sm",
                      disabled
                        ? "cursor-not-allowed text-moon-600"
                        : "cursor-pointer text-moon-200 hover:bg-ink-800",
                    )}
                  >
                    <input
                      type="checkbox"
                      checked={disabled ? false : checked}
                      disabled={disabled}
                      onChange={() => toggle(scope)}
                      className="h-3.5 w-3.5 accent-lamp"
                    />
                    <span className="font-mono text-xs">{scope}</span>
                    {disabled && <Lock size={11} className="ml-auto text-moon-600" />}
                  </label>
                );
                return disabled ? (
                  <Tooltip key={scope} content="Human-only: requires the admin session; never grantable to a token.">
                    {row}
                  </Tooltip>
                ) : (
                  row
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
