import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { HelpCircle } from "lucide-react";
import { qk } from "@/api";
import { agentsApi } from "@/api/agents";
import type { AgentPendingItem, RepoLinkOut } from "@/api/types";
import { toast } from "@/ui/Toast";
import { SectionHead, VerbButton } from "./VerdictSection";
import { pendingQuestion, pendingQuickDecision, pendingWaitLabel } from "./overview";

export interface AwaitingMr {
  repo: RepoLinkOut;
  iid: string;
  title: string;
}

/**
 * WAITING ON YOUR REPLY — a unified feed of human-input interrupts regardless of
 * source. From the human's seat an agent question and an MR awaiting review are
 * the same interruption ("something needs my word before it moves"), so they
 * share one row shape: (source, summary, quick action). Agent rows carry the
 * question inline with a quick-reply composer; MR rows carry an Open-peek verb.
 * (docs/design/project-control-plane.md §Overview ④.)
 *
 * The MR list is pre-fetched (and counted) by the Overview orchestrator so the
 * signal strip and this feed share one source of truth.
 */
export function WaitingSection({
  pending,
  awaitingMrs,
  onOpenMr,
}: {
  pending: AgentPendingItem[];
  awaitingMrs: AwaitingMr[];
  onOpenMr: (repo: RepoLinkOut, iid: string) => void;
}) {
  const count = pending.length + awaitingMrs.length;
  if (count === 0) return null;

  return (
    <section id="ov-waiting" className="mb-7 scroll-mt-24">
      <SectionHead label="Waiting on your reply" count={count} />
      <div>
        {pending.map((p) => (
          <AgentPendingRow key={`${p.session_id}:${p.request_id}`} pending={p} />
        ))}
        {awaitingMrs.map((m) => (
          <MrRow key={`${m.repo.id}:${m.iid}`} mr={m} onOpen={() => onOpenMr(m.repo, m.iid)} />
        ))}
      </div>
    </section>
  );
}

function AgentPendingRow({ pending }: { pending: AgentPendingItem }) {
  const qc = useQueryClient();
  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState(false);
  const decision = pendingQuickDecision(pending);
  const question = pendingQuestion(pending);
  const wait = pendingWaitLabel(pending);

  async function send() {
    if (busy) return;
    const answer = reply.trim();
    // A blank quick-reply is only meaningful for permission/plan kinds (where
    // the decision itself is the reply); for an open question, require text.
    if (pending.kind === "ask_question" && !answer) return;
    setBusy(true);
    try {
      await agentsApi.answer(pending.session_id, pending.request_id, {
        decision,
        answer: answer || undefined,
      });
      qc.invalidateQueries({ queryKey: qk.agents.attention });
      qc.invalidateQueries({ queryKey: qk.agents.pending });
      qc.invalidateQueries({ queryKey: qk.agents.list });
      toast.success("Reply sent");
      setReply("");
    } catch (err) {
      toast.error("Reply failed", { error: err });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="border-b border-ink-900 px-2.5 py-2.5 last:border-b-0">
      <div className="mb-1.5 flex items-center gap-2">
        <HelpCircle size={13} className="shrink-0 text-warn" />
        <span className="font-mono text-[12px] text-moon-100">{pending.session_title}</span>
        <span className="rounded-[5px] bg-warn/15 px-1.5 py-px text-[10px] font-semibold text-warn">
          needs reply{wait ? ` · ${wait}` : ""}
        </span>
        <span className="ml-auto text-[10px] uppercase tracking-wide text-moon-600">
          {pending.kind === "plan_approval"
            ? "plan"
            : pending.kind === "ask_question"
              ? "question"
              : "permission"}
        </span>
      </div>
      <p className="mb-2 ml-[22px] text-[12.5px] leading-relaxed text-moon-300">{question}</p>
      <div className="ml-[22px] flex gap-1.5">
        <input
          value={reply}
          onChange={(e) => setReply(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              send();
            }
          }}
          placeholder={
            pending.kind === "ask_question"
              ? "Quick reply…"
              : pending.kind === "plan_approval"
                ? "Feedback (optional) — blank to approve…"
                : "Reason (optional) — blank to allow…"
          }
          className="min-w-0 flex-1 rounded-control border border-ink-700 bg-ink-950 px-2.5 py-1.5 font-sans text-[12px] text-moon-100 placeholder:text-moon-600 focus:border-warn/50 focus:outline-none"
        />
        <button
          onClick={send}
          disabled={busy || (pending.kind === "ask_question" && !reply.trim())}
          className="shrink-0 rounded-control border border-warn bg-warn px-3 py-1.5 text-[11.5px] font-semibold text-ink-950 transition-colors hover:bg-warn-soft disabled:cursor-not-allowed disabled:opacity-50"
        >
          Send
        </button>
      </div>
    </div>
  );
}

function MrRow({ mr, onOpen }: { mr: AwaitingMr; onOpen: () => void }) {
  return (
    <div className="flex items-center gap-2.5 border-b border-ink-900 px-2.5 py-2 last:border-b-0">
      <span aria-hidden className="shrink-0 text-[13px] text-moon-600">
        ⑃
      </span>
      <span className="shrink-0 font-mono text-[11.5px] text-moon-600">!{mr.iid}</span>
      <span className="min-w-0 flex-1 truncate text-[12.5px] text-moon-100">{mr.title}</span>
      <span className="hidden shrink-0 rounded-[5px] bg-review/15 px-1.5 py-px text-[10px] font-semibold text-review sm:inline-block">
        awaiting your review
      </span>
      <VerbButton tone="ghost" className="shrink-0" onClick={onOpen}>
        Open peek
      </VerbButton>
    </div>
  );
}
