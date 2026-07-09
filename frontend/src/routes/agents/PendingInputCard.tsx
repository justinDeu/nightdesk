import { useState } from "react";
import { Check, HelpCircle, ShieldQuestion, X, ClipboardCheck } from "lucide-react";
import { Button } from "@/ui/Button";
import { Textarea } from "@/ui/Input";
import { cn } from "@/lib/cn";
import type { AgentAnswer, AgentPendingOut } from "@/api/types";

/**
 * The inline answer surface for an agent blocked on a human decision. Renders
 * per kind (resident-agents-v3.md §9):
 *  - permission   → the tool + its input, Allow / Deny (with optional reason)
 *  - ask_question → the question(s) with option chips + an "Other" free-text
 *  - plan_approval→ the plan text, Approve / Keep planning
 * The chosen decision POSTs to /agents/{id}/pending/{request_id}.
 */
export function PendingInputCard({
  pending,
  onAnswer,
  busy,
}: {
  pending: AgentPendingOut;
  onAnswer: (body: AgentAnswer) => Promise<unknown>;
  busy: boolean;
}) {
  return (
    <div className="rounded-card border border-lamp/40 bg-lamp/[0.06] shadow-[var(--shadow-raised)]">
      <div className="flex items-center gap-2 border-b border-lamp/20 px-3.5 py-2">
        <KindIcon kind={pending.kind} />
        <span className="text-[11px] font-semibold uppercase tracking-wide text-lamp">
          {pending.kind === "plan_approval"
            ? "Plan approval"
            : pending.kind === "ask_question"
              ? "Agent asked you"
              : "Permission request"}
        </span>
      </div>
      <div className="px-3.5 py-3">
        {pending.kind === "plan_approval" ? (
          <PlanApproval pending={pending} onAnswer={onAnswer} busy={busy} />
        ) : pending.kind === "ask_question" ? (
          <AskQuestion pending={pending} onAnswer={onAnswer} busy={busy} />
        ) : (
          <Permission pending={pending} onAnswer={onAnswer} busy={busy} />
        )}
      </div>
    </div>
  );
}

function KindIcon({ kind }: { kind: string }) {
  if (kind === "plan_approval") return <ClipboardCheck size={14} className="text-lamp" />;
  if (kind === "ask_question") return <HelpCircle size={14} className="text-lamp" />;
  return <ShieldQuestion size={14} className="text-lamp" />;
}

// --- permission ----------------------------------------------------------------

function Permission({
  pending,
  onAnswer,
  busy,
}: {
  pending: AgentPendingOut;
  onAnswer: (body: AgentAnswer) => Promise<unknown>;
  busy: boolean;
}) {
  const [denying, setDenying] = useState(false);
  const [reason, setReason] = useState("");
  const input = (pending.payload.input as Record<string, unknown>) ?? {};
  const suggestions = (pending.payload.suggestions as unknown[]) ?? [];

  return (
    <div className="space-y-3">
      <p className="text-[13px] text-moon-100">
        The agent wants to use{" "}
        <span className="font-mono font-semibold text-lamp">{pending.tool ?? "a tool"}</span>.
      </p>
      {Object.keys(input).length > 0 && (
        <pre className="max-h-48 overflow-auto rounded-control border border-ink-700 bg-ink-950 px-2.5 py-2 font-mono text-[11px] text-moon-300">
          {JSON.stringify(input, null, 2)}
        </pre>
      )}
      {suggestions.length > 0 && (
        <p className="text-[11px] text-moon-500">
          {suggestions.length} suggestion{suggestions.length === 1 ? "" : "s"} from the CLI attached.
        </p>
      )}

      {denying ? (
        <div className="space-y-2">
          <Textarea
            autoFocus
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Optional — tell the agent why (rides back as the denial message)…"
            className="min-h-[56px] text-[12px]"
          />
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant="danger"
              leadingIcon={<X size={13} />}
              loading={busy}
              onClick={() => onAnswer({ decision: "deny", answer: reason.trim() || undefined })}
            >
              Deny
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setDenying(false)} disabled={busy}>
              Back
            </Button>
          </div>
        </div>
      ) : (
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="primary"
            leadingIcon={<Check size={13} />}
            loading={busy}
            onClick={() => onAnswer({ decision: "allow" })}
          >
            Allow
          </Button>
          <Button size="sm" variant="danger" leadingIcon={<X size={13} />} disabled={busy} onClick={() => setDenying(true)}>
            Deny
          </Button>
        </div>
      )}
    </div>
  );
}

// --- ask_question --------------------------------------------------------------

interface QuestionOption {
  label: string;
  description?: string;
}

interface Question {
  question: string;
  header?: string;
  options: QuestionOption[];
  multiSelect?: boolean;
}

function normalizeQuestions(payload: Record<string, unknown>): Question[] {
  const raw = payload.questions;
  const arr = Array.isArray(raw) ? raw : raw ? [raw] : [];
  return arr.map((q) => {
    const obj = (q as Record<string, unknown>) ?? {};
    const opts = Array.isArray(obj.options) ? obj.options : [];
    return {
      question: String(obj.question ?? obj.prompt ?? "The agent asked a question."),
      header: obj.header ? String(obj.header) : undefined,
      multiSelect: Boolean(obj.multiSelect),
      options: opts.map((o) =>
        typeof o === "string"
          ? { label: o }
          : {
              label: String((o as Record<string, unknown>).label ?? o),
              description: (o as Record<string, unknown>).description
                ? String((o as Record<string, unknown>).description)
                : undefined,
            },
      ),
    };
  });
}

function AskQuestion({
  pending,
  onAnswer,
  busy,
}: {
  pending: AgentPendingOut;
  onAnswer: (body: AgentAnswer) => Promise<unknown>;
  busy: boolean;
}) {
  const questions = normalizeQuestions(pending.payload);
  // Selected label per question index; free-text "Other" per question index.
  const [selected, setSelected] = useState<Record<number, string[]>>({});
  const [other, setOther] = useState<Record<number, string>>({});
  const [otherOpen, setOtherOpen] = useState<Record<number, boolean>>({});

  const toggle = (qi: number, label: string, multi: boolean) => {
    setSelected((prev) => {
      const cur = prev[qi] ?? [];
      if (multi) {
        return { ...prev, [qi]: cur.includes(label) ? cur.filter((l) => l !== label) : [...cur, label] };
      }
      return { ...prev, [qi]: cur.includes(label) ? [] : [label] };
    });
  };

  const compose = (): string => {
    return questions
      .map((q, qi) => {
        const picks = [...(selected[qi] ?? [])];
        const free = otherOpen[qi] ? (other[qi] ?? "").trim() : "";
        if (free) picks.push(free);
        const answer = picks.join(", ");
        return questions.length > 1 ? `${q.header || q.question}: ${answer}` : answer;
      })
      .filter(Boolean)
      .join("\n");
  };

  const answered = compose().trim().length > 0;

  return (
    <div className="space-y-4">
      {questions.map((q, qi) => (
        <div key={qi} className="space-y-2">
          {q.header && (
            <div className="text-[10px] font-semibold uppercase tracking-wide text-moon-500">{q.header}</div>
          )}
          <p className="text-[13px] text-moon-100">{q.question}</p>
          <div className="flex flex-wrap gap-1.5">
            {q.options.map((o) => {
              const on = (selected[qi] ?? []).includes(o.label);
              return (
                <button
                  key={o.label}
                  type="button"
                  title={o.description}
                  onClick={() => toggle(qi, o.label, Boolean(q.multiSelect))}
                  className={cn(
                    "rounded-control border px-2.5 py-1 text-left text-[12px] transition-colors",
                    on
                      ? "border-lamp bg-lamp/15 text-lamp"
                      : "border-ink-700 text-moon-200 hover:border-ink-600 hover:text-moon-100",
                  )}
                >
                  <span className="font-medium">{o.label}</span>
                  {o.description && (
                    <span className="ml-1.5 text-[11px] text-moon-500">— {o.description}</span>
                  )}
                </button>
              );
            })}
            <button
              type="button"
              onClick={() => setOtherOpen((p) => ({ ...p, [qi]: !p[qi] }))}
              className={cn(
                "rounded-control border px-2.5 py-1 text-[12px] transition-colors",
                otherOpen[qi]
                  ? "border-lamp bg-lamp/15 text-lamp"
                  : "border-dashed border-ink-600 text-moon-400 hover:text-moon-100",
              )}
            >
              Other…
            </button>
          </div>
          {otherOpen[qi] && (
            <Textarea
              autoFocus
              value={other[qi] ?? ""}
              onChange={(e) => setOther((p) => ({ ...p, [qi]: e.target.value }))}
              placeholder="Type your own answer…"
              className="min-h-[52px] text-[12px]"
            />
          )}
        </div>
      ))}
      <Button
        size="sm"
        variant="primary"
        leadingIcon={<Check size={13} />}
        loading={busy}
        disabled={!answered}
        onClick={() => onAnswer({ decision: "allow", answer: compose() })}
      >
        Send answer
      </Button>
    </div>
  );
}

// --- plan_approval -------------------------------------------------------------

function PlanApproval({
  pending,
  onAnswer,
  busy,
}: {
  pending: AgentPendingOut;
  onAnswer: (body: AgentAnswer) => Promise<unknown>;
  busy: boolean;
}) {
  const [revising, setRevising] = useState(false);
  const [note, setNote] = useState("");
  const plan = String(pending.payload.plan ?? "");

  return (
    <div className="space-y-3">
      <div className="max-h-72 overflow-auto rounded-control border border-ink-700 bg-ink-950 px-3 py-2.5">
        {plan.trim() ? (
          <p className="whitespace-pre-wrap font-sans text-[13px] leading-relaxed text-moon-100">{plan}</p>
        ) : (
          <p className="text-[12px] italic text-moon-500">The agent proposed a plan with no text.</p>
        )}
      </div>
      {revising ? (
        <div className="space-y-2">
          <Textarea
            autoFocus
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="What should change? (sent back so the agent keeps planning)…"
            className="min-h-[56px] text-[12px]"
          />
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant="primary"
              loading={busy}
              onClick={() => onAnswer({ decision: "deny", answer: note.trim() || undefined })}
            >
              Send feedback
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setRevising(false)} disabled={busy}>
              Back
            </Button>
          </div>
        </div>
      ) : (
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="primary"
            leadingIcon={<Check size={13} />}
            loading={busy}
            onClick={() => onAnswer({ decision: "approve" })}
          >
            Approve plan
          </Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => setRevising(true)}>
            Keep planning
          </Button>
        </div>
      )}
    </div>
  );
}
