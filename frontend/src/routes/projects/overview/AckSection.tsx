import { useNavigate } from "@tanstack/react-router";
import { Check, CheckCheck } from "lucide-react";
import { useAckDigest, useAcknowledge, useBulkAck } from "@/api/ack";
import type { AckDigestGroup, AckDigestTicket } from "@/api/types";
import { formatUsd } from "@/lib/status";
import { ticketHref } from "@/lib/routes";
import { StatusPill } from "@/ui/StatusPill";
import { toast } from "@/ui/Toast";
import { SectionHead, VerbButton } from "./VerdictSection";
import { ANCHORS, ackDayLabel } from "./overview";

/**
 * TO ACKNOWLEDGE — the project-scoped twin of the Desk's ToAcknowledgeBand,
 * rendered as a ledger section so it matches Verdict/Waiting/Inbox. The digest
 * is already scoped to this project (possibly several day groups); shown
 * fully — no slice — since project scope keeps it small. (docs/design/
 * project-control-plane.md §Overview.)
 */
export function AckSection({ projectId }: { projectId: string }) {
  const digest = useAckDigest(projectId, { refetchInterval: 5000 });
  const bulkAck = useBulkAck();
  const navigate = useNavigate();

  const total = digest.data?.total ?? 0;
  const groups = digest.data?.groups ?? [];

  if (total === 0) return null;

  const openTicket = (id: string) => navigate({ to: "/tickets/$id", params: { id } });

  const ackGroup = (group: AckDigestGroup) => {
    bulkAck.mutate(
      { ticket_ids: group.tickets.map((t) => t.ticket_id) },
      { onError: (err) => toast.error("Ack failed", { error: err }) },
    );
  };

  const ackAll = () => {
    const ids = groups.flatMap((g) => g.tickets.map((t) => t.ticket_id));
    bulkAck.mutate(
      { ticket_ids: ids },
      { onError: (err) => toast.error("Ack failed", { error: err }) },
    );
  };

  return (
    <section id={ANCHORS.ack} className="mb-7 scroll-mt-32">
      <SectionHead
        label="To acknowledge"
        count={total}
        legend={
          <VerbButton tone="outline" onClick={ackAll}>
            <CheckCheck size={12} />
            Ack all ({total})
          </VerbButton>
        }
      />
      <div className="space-y-2.5">
        {groups.map((group) => (
          <AckGroupBlock
            key={`${group.project_id ?? "none"}-${group.day}`}
            group={group}
            onAckGroup={() => ackGroup(group)}
            onOpen={openTicket}
          />
        ))}
      </div>
    </section>
  );
}

function AckGroupBlock({
  group,
  onAckGroup,
  onOpen,
}: {
  group: AckDigestGroup;
  onAckGroup: () => void;
  onOpen: (id: string) => void;
}) {
  return (
    <div className="overflow-hidden rounded-control border border-ink-800">
      <div className="flex items-center gap-2 bg-ink-900/60 px-2.5 py-1.5">
        <span className="min-w-0 flex-1 truncate text-[11.5px] text-moon-300">
          {ackDayLabel(group.day)}
        </span>
        <span className="shrink-0 text-[10.5px] text-moon-600">
          {group.count} ticket{group.count === 1 ? "" : "s"} · {group.succeeded} ok
          {group.failed > 0 && <span className="text-failed"> · {group.failed} failed</span>}
          {group.cost_usd > 0 && <span> · {formatUsd(group.cost_usd)}</span>}
        </span>
        <VerbButton tone="outline" onClick={onAckGroup}>
          <Check size={12} />
          Ack
        </VerbButton>
      </div>
      <div>
        {group.tickets.map((t) => (
          <AckTicketRow key={t.ticket_id} ticket={t} onOpen={() => onOpen(t.ticket_id)} />
        ))}
      </div>
    </div>
  );
}

function AckTicketRow({
  ticket,
  onOpen,
}: {
  ticket: AckDigestTicket;
  onOpen: () => void;
}) {
  const acknowledge = useAcknowledge();

  return (
    <div className="group flex items-center gap-2.5 border-t border-ink-900 px-2.5 py-1.5 hover:bg-ink-900/50">
      {ticket.outcome === "failed" ? (
        <StatusPill status="failed" label="Failed" />
      ) : (
        <StatusPill status={ticket.status === "archived" ? "archived" : "review"} />
      )}
      <a
        href={ticketHref(ticket.ticket_id)}
        onClick={(e) => {
          if (e.metaKey || e.ctrlKey) return;
          e.preventDefault();
          onOpen();
        }}
        className="min-w-0 flex-1 truncate rounded-[4px] text-[12.5px] text-moon-100 group-hover:text-lamp focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lamp"
      >
        {ticket.title}
      </a>
      {ticket.cost_usd != null && (
        <span className="shrink-0 font-mono text-[11px] text-moon-600">
          {formatUsd(ticket.cost_usd)}
        </span>
      )}
      <VerbButton
        tone="outline"
        onClick={() =>
          acknowledge.mutate(ticket.ticket_id, {
            onError: (err) => toast.error("Ack failed", { error: err }),
          })
        }
      >
        <Check size={12} />
        Ack
      </VerbButton>
    </div>
  );
}
