import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Plus, RefreshCw, X, ExternalLink as ExternalLinkIcon } from "lucide-react";
import { qk } from "@/api";
import {
  integrationsApi,
  useRepoLinks,
  useTicketExternalLinks,
} from "@/api/integrations";
import type { ExternalLinkKind, ExternalLinkOut, ExternalLinkRole } from "@/api/types";
import { ApiError } from "@/api/client";
import { Badge } from "@/ui/Badge";
import { Button } from "@/ui/Button";
import { Input } from "@/ui/Input";
import { Select } from "@/ui/Select";
import { Spinner } from "@/ui/Spinner";
import { Tooltip } from "@/ui/Tooltip";
import { toast } from "@/ui/Toast";
import {
  externalKindGlyph,
  externalRefLabel,
  externalStateLabel,
  externalStateTone,
  roleLabel,
} from "@/components/ExternalItemChips";

/** Ticket rail "Linked items" section (§7): issues and MRs referenced by this
 *  ticket, with add / refresh / unlink. Reads the persisted external_links, not
 *  a live browse — these rows are the tracked state. */
export function LinkedItems({ ticketId }: { ticketId: string }) {
  const qc = useQueryClient();
  const links = useTicketExternalLinks(ticketId);
  const [adding, setAdding] = useState(false);

  const invalidate = () => qc.invalidateQueries({ queryKey: qk.integrations.ticketLinks(ticketId) });

  if (links.isLoading) {
    return <div className="flex items-center gap-2 text-xs text-moon-600"><Spinner size={12} /> Loading…</div>;
  }

  const rows = links.data ?? [];

  return (
    <div className="space-y-1.5">
      {rows.length === 0 && !adding && <p className="text-xs text-moon-600">No linked issues or MRs.</p>}
      {rows.map((link) => (
        <LinkRow key={link.id} ticketId={ticketId} link={link} onChange={invalidate} />
      ))}
      {adding ? (
        <AddLinkForm ticketId={ticketId} onClose={() => setAdding(false)} onAdded={invalidate} />
      ) : (
        <button
          onClick={() => setAdding(true)}
          className="inline-flex items-center gap-1 text-xs text-moon-400 hover:text-moon-100"
        >
          <Plus size={12} /> Link issue or MR
        </button>
      )}
    </div>
  );
}

function LinkRow({
  ticketId,
  link,
  onChange,
}: {
  ticketId: string;
  link: ExternalLinkOut;
  onChange: () => void;
}) {
  const [busy, setBusy] = useState(false);

  async function refresh() {
    setBusy(true);
    try {
      await integrationsApi.refreshExternalLink(link.id);
      onChange();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not refresh");
    } finally {
      setBusy(false);
    }
  }

  async function unlink() {
    setBusy(true);
    try {
      await integrationsApi.removeTicketLink(ticketId, link.id);
      onChange();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not unlink");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex items-center gap-2 rounded-control border border-ink-700 bg-ink-900 px-2 py-1.5">
      <span className="text-moon-600">{externalKindGlyph(link.kind)}</span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="font-mono text-[11px] text-moon-400">
            {externalRefLabel(link.kind, link.external_iid)}
          </span>
          <span className="truncate text-xs text-moon-100">{link.title || "(untitled)"}</span>
        </div>
        <div className="mt-0.5 flex items-center gap-1.5">
          <Badge tone="neutral" mono>{roleLabel(link.role)}</Badge>
          {link.state && (
            <Badge tone={externalStateTone(link.kind, link.state)} dot>
              {externalStateLabel(link.state)}
            </Badge>
          )}
        </div>
      </div>
      {link.url && (
        <Tooltip content="Open in GitLab">
          <a href={link.url} target="_blank" rel="noreferrer" className="text-moon-600 hover:text-moon-100" aria-label="Open in GitLab">
            <ExternalLinkIcon size={12} />
          </a>
        </Tooltip>
      )}
      <Tooltip content="Refresh state">
        <button onClick={refresh} disabled={busy} className="text-moon-600 hover:text-moon-100 disabled:opacity-50" aria-label="Refresh">
          <RefreshCw size={12} />
        </button>
      </Tooltip>
      <button onClick={unlink} disabled={busy} className="text-moon-600 hover:text-failed disabled:opacity-50" aria-label="Unlink">
        <X size={12} />
      </button>
    </div>
  );
}

function AddLinkForm({
  ticketId,
  onClose,
  onAdded,
}: {
  ticketId: string;
  onClose: () => void;
  onAdded: () => void;
}) {
  const repoLinks = useRepoLinks();
  const [repoLinkId, setRepoLinkId] = useState("");
  const [kind, setKind] = useState<ExternalLinkKind>("issue");
  const [iid, setIid] = useState("");
  const [role, setRole] = useState<ExternalLinkRole>("references");
  const [busy, setBusy] = useState(false);

  const repos = repoLinks.data ?? [];

  async function add() {
    const rid = repoLinkId || repos[0]?.id;
    if (!rid || !iid.trim()) {
      toast.error("Pick a repository and enter an issue/MR number");
      return;
    }
    setBusy(true);
    try {
      await integrationsApi.createTicketLink(ticketId, {
        repo_link_id: rid,
        kind,
        external_iid: iid.trim(),
        role,
      });
      onAdded();
      onClose();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not link");
    } finally {
      setBusy(false);
    }
  }

  if (repos.length === 0) {
    return (
      <p className="text-xs text-moon-600">
        No repositories connected. Add one under Settings → Connections first.
      </p>
    );
  }

  return (
    <div className="space-y-1.5 rounded-control border border-ink-700 bg-ink-900 p-2">
      <Select value={repoLinkId || repos[0].id} onChange={(e) => setRepoLinkId(e.target.value)}>
        {repos.map((r) => (
          <option key={r.id} value={r.id}>{r.external_path}</option>
        ))}
      </Select>
      <div className="flex items-center gap-1.5">
        <Select value={kind} onChange={(e) => setKind(e.target.value as ExternalLinkKind)} className="w-28">
          <option value="issue">Issue</option>
          <option value="merge_request">MR</option>
        </Select>
        <Input
          value={iid}
          onChange={(e) => setIid(e.target.value)}
          placeholder="number"
          className="w-24"
          inputMode="numeric"
        />
        <Select value={role} onChange={(e) => setRole(e.target.value as ExternalLinkRole)} className="flex-1">
          <option value="references">references</option>
          <option value="fixes">fixes</option>
          <option value="produced_mr">produced MR</option>
        </Select>
      </div>
      <div className="flex items-center gap-1.5">
        <Button size="sm" variant="primary" loading={busy} onClick={add}>Link</Button>
        <Button size="sm" variant="ghost" onClick={onClose}>Cancel</Button>
      </div>
    </div>
  );
}
