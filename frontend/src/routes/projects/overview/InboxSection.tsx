import { useNavigate } from "@tanstack/react-router";
import { TriangleAlert } from "lucide-react";
import type { InboxItemOut } from "@/api/types";
import { useTicketActions } from "@/lib/ticketActions";
import { relativeTime } from "@/lib/time";
import { Tooltip } from "@/ui/Tooltip";
import { SectionHead, VerbButton } from "./VerdictSection";
import { inboxTriaged, sortInbox } from "./overview";

/**
 * INBOX — one row per unresolved inbox item, verbs inline. A blocked or
 * >48h-stale item sorts first, renders amber, and gets a single Triage verb
 * (it can't be queued as-is) instead of the three-way Draft/Queue/Decline.
 * (docs/design/project-control-plane.md §Overview ⑤.)
 */
export function InboxSection({ items }: { items: InboxItemOut[] }) {
  const ordered = sortInbox(items);
  if (ordered.length === 0) return null;

  return (
    <section id="ov-inbox" className="mb-7 scroll-mt-32">
      <SectionHead label="Inbox" count={ordered.length} />
      <div>
        {ordered.map((item) => (
          <InboxRow key={item.ticket.id} item={item} />
        ))}
      </div>
    </section>
  );
}

function InboxRow({ item }: { item: InboxItemOut }) {
  const actions = useTicketActions();
  const navigate = useNavigate();
  const triaged = inboxTriaged(item);
  const t = item.ticket;
  const blocked = item.blockers.length > 0;

  return (
    <div
      className={
        "flex items-center gap-2.5 border-b border-ink-900 px-2.5 py-2 last:border-b-0 " +
        (triaged ? "bg-warn/[0.05]" : "hover:bg-ink-900/50")
      }
    >
      <span aria-hidden className="shrink-0 text-[13px] text-moon-600">
        ◌
      </span>
      <button
        onClick={() => navigate({ to: "/tickets/$id", params: { id: t.id } })}
        className={
          "min-w-0 flex-1 truncate text-left text-[12.5px] " +
          (triaged ? "text-warn-soft" : "text-moon-100")
        }
      >
        {t.title}
      </button>

      {blocked ? (
        <Tooltip content={item.blockers.join(" · ")}>
          <span className="hidden shrink-0 items-center gap-1 rounded-[5px] bg-failed/15 px-1.5 py-px text-[10px] font-semibold text-failed sm:inline-flex">
            <TriangleAlert size={10} /> blocked: {item.blockers[0]}
            {item.blockers.length > 1 ? ` +${item.blockers.length - 1}` : ""}
          </span>
        </Tooltip>
      ) : triaged ? (
        <span className="hidden shrink-0 rounded-[5px] bg-warn/15 px-1.5 py-px text-[10px] font-semibold text-warn sm:inline-block">
          stale {relativeTime(t.created_at)}
        </span>
      ) : (
        <span className="hidden shrink-0 font-mono text-[11px] text-moon-600 sm:inline">
          ready {relativeTime(t.created_at)}
        </span>
      )}

      <span className="flex shrink-0 gap-1.5">
        {triaged ? (
          <VerbButton
            tone="warn"
            onClick={() => navigate({ to: "/tickets/$id", params: { id: t.id } })}
          >
            Triage
          </VerbButton>
        ) : (
          <>
            <VerbButton tone="ghost" onClick={() => actions.promote(t, "draft")}>
              Draft
            </VerbButton>
            <VerbButton tone="ghost" onClick={() => actions.promote(t, "queued")}>
              Queue
            </VerbButton>
            <VerbButton tone="ghost" onClick={() => actions.decline(t)}>
              Decline
            </VerbButton>
          </>
        )}
      </span>
    </div>
  );
}
