import { useEffect, useMemo, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { FileUp, Hexagon, Plus, Upload } from "lucide-react";
import { Button } from "@/ui/Button";
import { Input, Textarea, Field } from "@/ui/Input";
import { Select } from "@/ui/Select";
import { Dialog } from "@/ui/Dialog";
import { Badge } from "@/ui/Badge";
import { Spinner } from "@/ui/Spinner";
import { EmptyState } from "@/ui/EmptyState";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/ui/DropdownMenu";
import { toast } from "@/ui/Toast";
import { ApiError } from "@/api/client";
import { profilesApi, useProfiles } from "@/api/profiles";
import { profileTransferApi } from "@/api/profileTransfer";
import { qk } from "@/api/keys";
import { cn } from "@/lib/cn";
import { ProfileEditor } from "./ProfileEditor";

const BACKEND_LABEL: Record<string, string> = {
  claude_sdk: "Claude Code",
  omp_rpc: "OMP / RPC",
};

export function ProfilesSection() {
  const qc = useQueryClient();
  const profiles = useProfiles();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [importKind, setImportKind] = useState<"native" | "cc" | null>(null);

  const list = useMemo(
    () => [...(profiles.data ?? [])].sort((a, b) => a.name.localeCompare(b.name)),
    [profiles.data],
  );

  // Keep a valid selection as the list changes.
  useEffect(() => {
    if (list.length === 0) {
      setSelectedId(null);
    } else if (!selectedId || !list.some((p) => p.id === selectedId)) {
      setSelectedId(list[0].id);
    }
  }, [list, selectedId]);

  const selected = list.find((p) => p.id === selectedId) ?? null;
  const invalidate = () => qc.invalidateQueries({ queryKey: qk.profiles.all });

  return (
    <div className="grid grid-cols-[240px_1fr] gap-6">
      <div className="min-w-0">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="font-display text-lg font-semibold tracking-tight text-moon-100">Profiles</h2>
          <div className="flex items-center gap-1">
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="sm" leadingIcon={<Upload size={13} />}>
                  Import
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem icon={<FileUp size={14} />} onSelect={() => setImportKind("native")}>
                  From nightdesk export
                </DropdownMenuItem>
                <DropdownMenuItem icon={<FileUp size={14} />} onSelect={() => setImportKind("cc")}>
                  From Claude Code settings.json
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
            <Button variant="primary" size="sm" leadingIcon={<Plus size={13} />} onClick={() => setCreating(true)}>
              New
            </Button>
          </div>
        </div>

        {profiles.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-moon-400">
            <Spinner size={14} /> Loading…
          </div>
        ) : list.length > 0 ? (
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
                  <div className="flex items-center justify-between gap-2">
                    <span className={cn("truncate text-sm font-medium", active ? "text-moon-100" : "text-moon-400")}>
                      {p.name}
                    </span>
                  </div>
                  <div className="mt-1 flex items-center gap-1.5">
                    <Badge tone="neutral" mono>
                      {BACKEND_LABEL[p.backend] ?? p.backend}
                    </Badge>
                    <span className="text-[11px] text-moon-600">net {p.network_mode}</span>
                  </div>
                </button>
              );
            })}
          </nav>
        ) : (
          <p className="text-sm text-moon-600">No profiles.</p>
        )}
      </div>

      <div className="min-w-0">
        {selected ? (
          <ProfileEditor
            key={selected.id}
            profile={selected}
            onDeleted={() => {
              invalidate();
              setSelectedId(null);
            }}
            onCopied={(id) => setSelectedId(id)}
          />
        ) : (
          <EmptyState
            icon={<Hexagon size={18} />}
            title="No profile selected"
            description="Profiles define the sandbox, tools, credentials, and model a run uses."
            action={
              <Button variant="ghost" leadingIcon={<Plus size={14} />} onClick={() => setCreating(true)}>
                New profile
              </Button>
            }
          />
        )}
      </div>

      {creating && (
        <CreateProfileDialog
          onClose={() => setCreating(false)}
          onCreated={(id) => {
            invalidate();
            setSelectedId(id);
            setCreating(false);
          }}
        />
      )}

      {importKind && (
        <ImportDialog
          kind={importKind}
          onClose={() => setImportKind(null)}
          onImported={(id) => {
            invalidate();
            setSelectedId(id);
            setImportKind(null);
          }}
        />
      )}
    </div>
  );
}

function CreateProfileDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const [name, setName] = useState("");
  const [backend, setBackend] = useState("claude_sdk");
  const [busy, setBusy] = useState(false);

  async function create() {
    if (!name.trim()) return;
    setBusy(true);
    try {
      const created = await profilesApi.create({
        name: name.trim(),
        backend,
        // claude_sdk requires an auth source at create time; inherit is the
        // zero-config default. Non-Claude backends ignore credentials.
        ...(backend === "claude_sdk" ? { claude_credentials: { source: "inherit" } } : {}),
      });
      toast.success("Profile created", { description: "Fill in the details, then save." });
      onCreated(created.id);
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not create profile");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="New profile"
      description="Start with a name and backend — configure the rest in the editor."
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" loading={busy} disabled={!name.trim()} onClick={create}>
            Create
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="Name">
          <Input autoFocus placeholder="claude-code-bypass" value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && create()} />
        </Field>
        <Field label="Backend">
          <Select value={backend} onChange={(e) => setBackend(e.target.value)}>
            <option value="claude_sdk">Claude Code</option>
            <option value="omp_rpc">OMP / RPC</option>
          </Select>
        </Field>
      </div>
    </Dialog>
  );
}

function ImportDialog({
  kind,
  onClose,
  onImported,
}: {
  kind: "native" | "cc";
  onClose: () => void;
  onImported: (id: string) => void;
}) {
  const [text, setText] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isCc = kind === "cc";

  async function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setText(await file.text());
  }

  async function run() {
    setError(null);
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(text);
    } catch {
      setError("That is not valid JSON.");
      return;
    }
    setBusy(true);
    try {
      const result = isCc
        ? await profileTransferApi.importFromCc(parsed, name.trim() || undefined)
        : await profileTransferApi.import(parsed, name.trim() || undefined);
      const dropped = result.dropped_fields;
      toast.success("Profile imported", {
        description: dropped.length
          ? `Dropped ${dropped.length} unsupported field(s). Re-enter any secrets.`
          : "Re-enter any secrets in the editor.",
      });
      onImported(result.id);
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Import failed";
      setError(msg);
      toast.error(msg);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={isCc ? "Import from Claude Code" : "Import profile"}
      description={
        isCc
          ? "Translate a Claude Code settings.json into a nightdesk profile."
          : "Load a nightdesk profile export. Secrets are never carried — re-enter them after import."
      }
      size="md"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" loading={busy} disabled={!text.trim()} onClick={run}>
            Import
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="Name override" hint="Optional. Overrides the name in the payload.">
          <Input placeholder="Imported profile" value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label={isCc ? "settings.json" : "Profile JSON"} error={error ?? undefined}>
          <label className="mb-2 inline-flex cursor-pointer items-center gap-1.5 rounded-control border border-ink-700 px-2.5 py-1 text-xs text-moon-100 hover:bg-ink-800">
            <FileUp size={13} /> Choose file…
            <input type="file" accept="application/json,.json" className="hidden" onChange={onFile} />
          </label>
          <Textarea
            mono
            className="min-h-[160px]"
            placeholder={isCc ? '{ "model": "sonnet", "permissions": { … } }' : '{ "name": "…", "fs_read": [ … ] }'}
            value={text}
            invalid={!!error}
            onChange={(e) => {
              setText(e.target.value);
              setError(null);
            }}
          />
        </Field>
      </div>
    </Dialog>
  );
}
