import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleDot, ExternalLink as ExternalLinkIcon, GitMerge, Import, Link2, X } from "lucide-react";
import { qk } from "@/api";
import { integrationsApi } from "@/api/integrations";
import type { RepoLinkOut, TicketOut } from "@/api/types";
import { ApiError } from "@/api/client";
import { Badge } from "@/ui/Badge";
import { Button } from "@/ui/Button";
import { Select } from "@/ui/Select";
import { Spinner } from "@/ui/Spinner";
import { Tooltip } from "@/ui/Tooltip";
import { toast } from "@/ui/Toast";
import { relativeTime } from "@/lib/time";
import { cn } from "@/lib/cn";
import { externalStateTone } from "@/components/ExternalItemChips";
import { peekRailClasses } from "@/routes/tickets/peekLayout";

// ---------------------------------------------------------------------------
// Issue / MR side-peek (never navigates). Shared by the Integration lens on the
// Tickets page and the GitLab integrations panel on the Project page — extracted
// here so both routes reuse one component (and one Import/Link flow).
//
// `projectId` is the project an imported issue is filed under. It is OPTIONAL:
// undefined when a repo isn't attached to exactly one project (e.g. the lens in
// global scope). When undefined the Import button is shown disabled with a
// tooltip explaining the ambiguity; Link-to-ticket stays available either way.
// ---------------------------------------------------------------------------

export function ExternalItemPeek({
  repo,
  kind,
  iid,
  projectId,
  onClose,
}: {
  repo: RepoLinkOut;
  kind: "issue" | "merge_request";
  iid: string;
  /** Project to file an imported issue under. Undefined in global scope when the
   *  repo isn't attached to exactly one project — import is then disabled. */
  projectId?: string;
  onClose: () => void;
}) {
  const item = useQuery({
    queryKey: qk.integrations.item(repo.id, kind, iid),
    queryFn: () =>
      kind === "issue" ? integrationsApi.getIssue(repo.id, iid) : integrationsApi.getMr(repo.id, iid),
  });

  const workedBy = useQuery({
    queryKey: ["integrations", "worked-by", repo.id, kind, iid],
    queryFn: async () => {
      // No cross-ticket external-link index in v1; derive by scanning the
      // project's tickets for a matching link is out of scope. Placeholder for
      // v2 back-links; kept as an empty list so the section renders cleanly.
      return [] as { ticket_id: string; title: string }[];
    },
    enabled: false,
  });

  const data = item.data;
  const [importing, setImporting] = useState(false);

  // Dismiss on Escape. Registered on window (not the keybinds registry) so it
  // composes with whatever Escape binds a host page already has — pressing Esc
  // closes this overlay regardless of where it's mounted.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <aside className={cn(peekRailClasses)} aria-label="Issue preview">
      <div className="flex items-start justify-between gap-2 border-b border-ink-700 px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="text-moon-600">{kind === "merge_request" ? <GitMerge size={14} /> : <CircleDot size={14} />}</span>
            <span className="font-mono text-[12px] text-moon-600">
              {kind === "merge_request" ? "!" : "#"}{iid}
            </span>
          </div>
          <h2 className="mt-0.5 text-sm font-semibold leading-snug text-moon-100">
            {data?.title ?? (item.isLoading ? "Loading…" : "(unavailable)")}
          </h2>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {data?.web_url && (
            <Tooltip content="Open in GitLab">
              <a href={data.web_url} target="_blank" rel="noreferrer" className="grid h-7 w-7 place-items-center rounded-control text-moon-400 hover:bg-ink-800 hover:text-moon-100" aria-label="Open in GitLab">
                <ExternalLinkIcon size={14} />
              </a>
            </Tooltip>
          )}
          <button onClick={onClose} className="grid h-7 w-7 place-items-center rounded-control text-moon-400 hover:bg-ink-800 hover:text-moon-100" aria-label="Close">
            <X size={15} />
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        {item.isLoading ? (
          <div className="flex items-center gap-2 text-sm text-moon-600"><Spinner size={13} /> Loading…</div>
        ) : item.isError ? (
          <p className="text-sm text-failed">{item.error instanceof ApiError ? item.error.message : "Could not load"}</p>
        ) : data ? (
          <>
            <div className="mb-3 flex flex-wrap items-center gap-1.5">
              <Badge tone={externalStateTone(kind, data.state)} dot>{data.state}</Badge>
              {data.author?.username && <span className="text-[11px] text-moon-600">@{data.author.username}</span>}
              {data.updated_at && <span className="text-[11px] text-moon-600">updated {relativeTime(data.updated_at)}</span>}
              {kind === "merge_request" && data.source_branch && (
                <span className="font-mono text-[11px] text-moon-600">{data.source_branch} → {data.target_branch}</span>
              )}
            </div>
            {(data.labels ?? []).length > 0 && (
              <div className="mb-3 flex flex-wrap gap-1">
                {(data.labels ?? []).map((l) => (
                  <Badge key={l} tone="neutral">{l}</Badge>
                ))}
              </div>
            )}
            <div className="whitespace-pre-wrap break-words border-t border-ink-700/60 pt-3 text-sm leading-relaxed text-moon-300">
              {data.description?.trim() || <span className="text-moon-600">No description.</span>}
            </div>
            {(workedBy.data ?? []).length > 0 && (
              <div className="mt-3 border-t border-ink-700/60 pt-3">
                <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-moon-600">Worked by</div>
                {(workedBy.data ?? []).map((w) => (
                  <div key={w.ticket_id} className="text-xs text-moon-300">{w.title}</div>
                ))}
              </div>
            )}
          </>
        ) : null}
      </div>

      <div className="flex items-center gap-2 border-t border-ink-700 px-4 py-3">
        {kind === "issue" && projectId && (
          <Button
            size="sm"
            variant="primary"
            leadingIcon={<Import size={13} />}
            loading={importing}
            onClick={async () => {
              setImporting(true);
              try {
                const ticket = await integrationsApi.importTicket(repo.id, {
                  kind: "issue",
                  external_iid: iid,
                  project_id: projectId,
                });
                toast.success("Imported as draft", { description: (ticket as TicketOut).title });
                onClose();
              } catch (err) {
                toast.error(err instanceof ApiError ? err.message : "Could not import");
              } finally {
                setImporting(false);
              }
            }}
          >
            Import as ticket
          </Button>
        )}
        {kind === "issue" && !projectId && (
          <Tooltip content="Attach this repository to a single project to import it as a ticket.">
            <span>
              <Button size="sm" variant="primary" leadingIcon={<Import size={13} />} disabled>
                Import as ticket
              </Button>
            </span>
          </Tooltip>
        )}
        <LinkToTicketButton repo={repo} kind={kind} iid={iid} />
      </div>
    </aside>
  );
}

function LinkToTicketButton({
  repo,
  kind,
  iid,
}: {
  repo: RepoLinkOut;
  kind: "issue" | "merge_request";
  iid: string;
}) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [ticketId, setTicketId] = useState("");
  const tickets = useQuery({
    queryKey: ["tickets", "link-picker"],
    queryFn: () => import("@/api/tickets").then((m) => m.ticketsApi.list({ limit: 100 })),
    enabled: open,
  });
  const options = useMemo(
    () => (tickets.data ?? []).filter((t) => t.status !== "archived"),
    [tickets.data],
  );

  async function link() {
    const tid = ticketId || options[0]?.id;
    if (!tid) return;
    try {
      await integrationsApi.createTicketLink(tid, {
        repo_link_id: repo.id,
        kind,
        external_iid: iid,
        role: kind === "merge_request" ? "produced_mr" : "references",
      });
      qc.invalidateQueries({ queryKey: qk.integrations.ticketLinks(tid) });
      toast.success("Linked to ticket");
      setOpen(false);
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not link");
    }
  }

  if (!open) {
    return (
      <Button size="sm" variant="ghost" leadingIcon={<Link2 size={13} />} onClick={() => setOpen(true)}>
        Link to ticket…
      </Button>
    );
  }

  return (
    <div className="flex flex-1 items-center gap-1.5">
      <Select value={ticketId || options[0]?.id || ""} onChange={(e) => setTicketId(e.target.value)} className="min-w-0 flex-1">
        {options.length === 0 ? (
          <option value="">No tickets</option>
        ) : (
          options.map((t) => (
            <option key={t.id} value={t.id}>{t.title}</option>
          ))
        )}
      </Select>
      <Button size="sm" variant="primary" disabled={options.length === 0} onClick={link}>Link</Button>
      <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
    </div>
  );
}
