import { useEffect, useMemo, useState } from "react";
import { GitMerge, Plus, RefreshCw, Trash2, ExternalLink as ExternalLinkIcon } from "lucide-react";
import { Button } from "@/ui/Button";
import { Input, Field } from "@/ui/Input";
import { Dialog } from "@/ui/Dialog";
import { Badge } from "@/ui/Badge";
import { Spinner } from "@/ui/Spinner";
import { EmptyState } from "@/ui/EmptyState";
import { Tooltip } from "@/ui/Tooltip";
import { toast } from "@/ui/Toast";
import { ApiError } from "@/api/client";
import {
  integrationsApi,
  useConnections,
  useCreateConnection,
  useDeleteConnection,
  useUpdateConnection,
  useTestConnection,
  useRepoLinks,
  useCreateRepoLink,
  useDeleteRepoLink,
} from "@/api/integrations";
import type {
  ConnectionOut,
  ConnectionStatus,
  ProviderProjectOut,
  RepoLinkOut,
} from "@/api/types";
import { relativeTime } from "@/lib/time";
import { cn } from "@/lib/cn";
import { SectionHeader } from "./parts/SettingsSection";
import { ConfirmDialog } from "./parts/ConfirmDialog";

const STATUS_META: Record<ConnectionStatus, { label: string; dot: string; text: string }> = {
  ok: { label: "OK", dot: "bg-success", text: "text-success" },
  auth_failed: { label: "Auth failed", dot: "bg-failed", text: "text-failed" },
  unreachable: { label: "Unreachable", dot: "bg-failed", text: "text-failed" },
  unchecked: { label: "Unchecked", dot: "bg-moon-600", text: "text-moon-600" },
};

export function ConnectionsSection() {
  const connections = useConnections();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const list = useMemo(
    () => [...(connections.data ?? [])].sort((a, b) => a.name.localeCompare(b.name)),
    [connections.data],
  );

  useEffect(() => {
    if (list.length === 0) setSelectedId(null);
    else if (!selectedId || !list.some((c) => c.id === selectedId)) setSelectedId(list[0].id);
  }, [list, selectedId]);

  const selected = list.find((c) => c.id === selectedId) ?? null;

  return (
    <div>
      <SectionHeader
        title="Connections"
        description="Connect a GitLab instance, then browse its issues and merge requests inside nightdesk. Credentials are stored encrypted and never leave the server."
      />

      {connections.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-moon-400">
          <Spinner size={14} /> Loading…
        </div>
      ) : list.length === 0 ? (
        <EmptyState
          icon={<GitMerge size={18} />}
          title="No connections yet"
          description="A connection is one GitLab instance authenticated with a personal (or project/group) access token. Add one, attach its repos to projects, then view issues and MRs."
          action={
            <Button variant="primary" leadingIcon={<Plus size={14} />} onClick={() => setCreating(true)}>
              Add connection
            </Button>
          }
        />
      ) : (
        <div className="grid grid-cols-[240px_1fr] gap-6">
          <div className="min-w-0">
            <div className="mb-3 flex items-center justify-between">
              <h3 className="text-xs font-medium uppercase tracking-wide text-moon-600">
                {list.length} connection{list.length === 1 ? "" : "s"}
              </h3>
              <Button variant="primary" size="sm" leadingIcon={<Plus size={13} />} onClick={() => setCreating(true)}>
                New
              </Button>
            </div>
            <nav className="space-y-0.5">
              {list.map((c) => {
                const active = c.id === selectedId;
                const meta = STATUS_META[c.status];
                return (
                  <button
                    key={c.id}
                    type="button"
                    onClick={() => setSelectedId(c.id)}
                    className={cn(
                      "w-full rounded-control px-2.5 py-2 text-left transition-colors",
                      active ? "bg-ink-800" : "hover:bg-ink-800/60",
                    )}
                  >
                    <span className="flex items-center gap-2">
                      <span className={cn("h-1.5 w-1.5 shrink-0 rounded-full", meta.dot)} />
                      <span className={cn("block truncate text-sm font-medium", active ? "text-moon-100" : "text-moon-400")}>
                        {c.name}
                      </span>
                    </span>
                    <div className="mt-1 flex items-center gap-1.5 pl-3.5">
                      <Badge tone="neutral" mono>{c.provider}</Badge>
                      <span className="text-[11px] text-moon-600">
                        {c.repo_link_count} repo{c.repo_link_count === 1 ? "" : "s"}
                      </span>
                    </div>
                  </button>
                );
              })}
            </nav>
          </div>

          <div className="min-w-0">
            {selected && (
              <ConnectionDetail key={selected.id} connection={selected} onDeleted={() => setSelectedId(null)} />
            )}
          </div>
        </div>
      )}

      {creating && (
        <CreateConnectionDialog
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
// Connection detail
// ---------------------------------------------------------------------------

function ConnectionDetail({ connection, onDeleted }: { connection: ConnectionOut; onDeleted: () => void }) {
  const deleteConnection = useDeleteConnection();
  const testConnection = useTestConnection();
  const updateConnection = useUpdateConnection();
  const repoLinks = useRepoLinks(connection.id);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [rotating, setRotating] = useState(false);
  const [addingRepo, setAddingRepo] = useState(false);
  const meta = STATUS_META[connection.status];

  async function doTest() {
    try {
      const res = await testConnection.mutateAsync(connection.id);
      if (res.status === "ok") toast.success("Connection OK");
      else toast.error(res.status_detail ?? `Test failed: ${res.status}`);
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Test failed");
    }
  }

  async function doDelete() {
    try {
      await deleteConnection.mutateAsync(connection.id);
      toast.success("Connection deleted");
      onDeleted();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not delete connection");
    } finally {
      setConfirmDelete(false);
    }
  }

  return (
    <div className="space-y-5">
      <div className="rounded-card border border-ink-700 bg-ink-900 px-4 py-3.5">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h3 className="font-display text-base font-semibold text-moon-100">{connection.name}</h3>
              <Badge tone="neutral" mono>{connection.provider}</Badge>
            </div>
            <p className="mt-1 truncate font-mono text-[12px] text-moon-400">{connection.base_url}</p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <Button
              variant="ghost"
              size="sm"
              leadingIcon={testConnection.isPending ? <Spinner size={12} /> : <RefreshCw size={12} />}
              onClick={doTest}
              disabled={testConnection.isPending}
            >
              Test
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setRotating(true)}>Edit token</Button>
            <Button variant="danger" size="sm" leadingIcon={<Trash2 size={13} />} onClick={() => setConfirmDelete(true)}>
              Delete
            </Button>
          </div>
        </div>
        <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
          <span className={cn("inline-flex items-center gap-1.5", meta.text)}>
            <span className={cn("h-1.5 w-1.5 rounded-full", meta.dot)} /> {meta.label}
          </span>
          <span className={connection.credential_set ? "text-success" : "text-failed"}>
            {connection.credential_set ? "token on file" : "no token set"}
          </span>
          {connection.last_checked_at && (
            <span className="text-moon-600">checked {relativeTime(connection.last_checked_at)}</span>
          )}
        </div>
        {connection.status_detail && (
          <p className="mt-1.5 rounded-control border border-failed/30 bg-failed/10 px-2 py-1 text-[11px] text-failed">
            {connection.status_detail}
          </p>
        )}
      </div>

      <div>
        <div className="mb-2 flex items-center justify-between">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-moon-600">Repositories</h4>
          <Button variant="ghost" size="sm" leadingIcon={<Plus size={13} />} onClick={() => setAddingRepo(true)}>
            Add repo
          </Button>
        </div>
        {repoLinks.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-moon-400"><Spinner size={13} /> Loading…</div>
        ) : (repoLinks.data ?? []).length === 0 ? (
          <p className="text-sm text-moon-600">No repositories linked yet.</p>
        ) : (
          <div className="space-y-2">
            {(repoLinks.data ?? []).map((rl) => (
              <RepoLinkRow key={rl.id} repo={rl} />
            ))}
          </div>
        )}
      </div>

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={`Delete ${connection.name}?`}
        body="Removes the connection. Blocked while it still owns repository links — remove those first."
        confirmLabel="Delete"
        danger
        busy={deleteConnection.isPending}
        onConfirm={doDelete}
      />

      {rotating && (
        <EditTokenDialog
          connection={connection}
          busy={updateConnection.isPending}
          onClose={() => setRotating(false)}
          onSave={async (value) => {
            try {
              await updateConnection.mutateAsync({ id: connection.id, body: { credential_value: value } });
              toast.success("Token updated");
              setRotating(false);
            } catch (err) {
              toast.error(err instanceof ApiError ? err.message : "Could not update token");
            }
          }}
        />
      )}

      {addingRepo && (
        <AddRepoDialog connection={connection} onClose={() => setAddingRepo(false)} />
      )}
    </div>
  );
}

function RepoLinkRow({ repo }: { repo: RepoLinkOut }) {
  const deleteRepoLink = useDeleteRepoLink();
  const [confirm, setConfirm] = useState(false);

  async function doDelete() {
    try {
      await deleteRepoLink.mutateAsync(repo.id);
      toast.success("Repository removed");
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not remove repository");
    } finally {
      setConfirm(false);
    }
  }

  return (
    <div className="flex items-center gap-2 rounded-card border border-ink-700 bg-ink-900 px-3 py-2">
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium text-moon-100">{repo.display_name || repo.external_path}</div>
        <div className="truncate font-mono text-[11px] text-moon-600">{repo.external_path}</div>
      </div>
      <span className="shrink-0 text-[11px] text-moon-600">
        {repo.project_ids.length} project{repo.project_ids.length === 1 ? "" : "s"}
      </span>
      {repo.web_url && (
        <Tooltip content="Open in GitLab">
          <a
            href={repo.web_url}
            target="_blank"
            rel="noreferrer"
            className="text-moon-600 hover:text-moon-100"
            aria-label="Open in GitLab"
          >
            <ExternalLinkIcon size={13} />
          </a>
        </Tooltip>
      )}
      <button onClick={() => setConfirm(true)} className="text-moon-600 hover:text-failed" aria-label="Remove repository">
        <Trash2 size={13} />
      </button>
      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        title={`Remove ${repo.external_path}?`}
        body="Blocked while any ticket links an issue or MR from this repository."
        confirmLabel="Remove"
        danger
        busy={deleteRepoLink.isPending}
        onConfirm={doDelete}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Dialogs
// ---------------------------------------------------------------------------

function CreateConnectionDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (id: string) => void }) {
  const createConnection = useCreateConnection();
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("https://gitlab.com");
  const [token, setToken] = useState("");

  async function create() {
    try {
      const created = await createConnection.mutateAsync({
        name: name.trim(),
        provider: "gitlab",
        base_url: baseUrl.trim() || "https://gitlab.com",
        auth_kind: "pat",
        credential_value: token.trim() || undefined,
      });
      toast.success("Connection created", { description: "Run Test to verify the token." });
      onCreated(created.id);
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not create connection");
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="Add GitLab connection"
      description="Use a personal, project, or group access token with read_api scope."
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={createConnection.isPending}>Cancel</Button>
          <Button variant="primary" loading={createConnection.isPending} disabled={!name.trim()} onClick={create}>
            Create
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="Name">
          <Input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="company gitlab" />
        </Field>
        <Field label="Base URL" hint="gitlab.com or your self-hosted instance.">
          <Input mono value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://gitlab.com" />
        </Field>
        <Field label="Access token" hint="Stored encrypted; never shown again.">
          <Input mono type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder="glpat-…" />
        </Field>
      </div>
    </Dialog>
  );
}

function EditTokenDialog({
  connection,
  busy,
  onClose,
  onSave,
}: {
  connection: ConnectionOut;
  busy: boolean;
  onClose: () => void;
  onSave: (value: string) => void;
}) {
  const [value, setValue] = useState("");
  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={`Update token — ${connection.name}`}
      description="Replaces the stored access token."
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" loading={busy} disabled={!value.trim()} onClick={() => onSave(value.trim())}>
            Save
          </Button>
        </>
      }
    >
      <Field label="New access token">
        <Input mono type="password" autoFocus value={value} onChange={(e) => setValue(e.target.value)} placeholder="glpat-…" />
      </Field>
    </Dialog>
  );
}

function AddRepoDialog({ connection, onClose }: { connection: ConnectionOut; onClose: () => void }) {
  const createRepoLink = useCreateRepoLink();
  const [search, setSearch] = useState("");
  const [results, setResults] = useState<ProviderProjectOut[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    const t = setTimeout(async () => {
      try {
        const rows = await integrationsApi.connectionProjects(connection.id, search);
        if (!cancelled) setResults(rows);
      } catch (err) {
        if (!cancelled) setError(err instanceof ApiError ? err.message : "Could not search projects");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [search, connection.id]);

  async function pick(p: ProviderProjectOut) {
    try {
      await createRepoLink.mutateAsync({
        connection_id: connection.id,
        external_kind: "gitlab_project",
        external_id: p.external_id,
        external_path: p.external_path,
        display_name: p.display_name,
        git_remote_url: p.git_remote_url,
        web_url: p.web_url,
      });
      toast.success(`Added ${p.external_path}`);
      onClose();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not add repository");
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="Add repository"
      description="Search projects this token can see, then pick one."
      size="md"
      footer={<Button variant="ghost" onClick={onClose}>Close</Button>}
    >
      <div className="space-y-3">
        <Input autoFocus value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search projects…" />
        {loading ? (
          <div className="flex items-center gap-2 text-sm text-moon-400"><Spinner size={13} /> Searching…</div>
        ) : error ? (
          <p className="rounded-control border border-failed/30 bg-failed/10 px-2 py-1.5 text-[12px] text-failed">{error}</p>
        ) : results.length === 0 ? (
          <p className="text-sm text-moon-600">No projects found.</p>
        ) : (
          <ul className="max-h-[320px] space-y-1 overflow-y-auto">
            {results.map((p) => (
              <li key={p.external_id}>
                <button
                  type="button"
                  onClick={() => pick(p)}
                  disabled={createRepoLink.isPending}
                  className="w-full rounded-card border border-ink-700 bg-ink-900 px-3 py-2 text-left hover:border-lamp/50 hover:bg-ink-800 disabled:opacity-50"
                >
                  <div className="truncate text-sm font-medium text-moon-100">{p.display_name || p.external_path}</div>
                  <div className="truncate font-mono text-[11px] text-moon-600">{p.external_path}</div>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </Dialog>
  );
}
