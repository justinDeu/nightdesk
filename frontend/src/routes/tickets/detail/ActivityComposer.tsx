import { useEffect, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { ChevronDown, GitMerge, Pencil, Send, Sparkles, X } from "lucide-react";
import type { ProfileOut, TicketOut } from "@/api/types";
import { ticketsApi } from "@/api/tickets";
import { Button } from "@/ui/Button";
import { Textarea } from "@/ui/Input";
import { Tooltip } from "@/ui/Tooltip";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/ui/DropdownMenu";
import { toast } from "@/ui/Toast";
import { relativeTime } from "@/lib/time";
import { cn } from "@/lib/cn";
import { NewConversationDialog, RestartDialog, CloneDialog } from "./ConversationDialogs";

type Intent = "continue" | "guidance";

/**
 * The single activity composer. One input, one explicit intent switch:
 *   • Continue now  → POST /continue: sends immediately as the next turn on the
 *     resumed conversation (only when the ticket is reviewable).
 *   • Guidance for next run → stages next_run_context: queued, surfaced as a
 *     pending-context chip until the next run picks it up (or you merge it).
 * The old separate "continue box" and "steering card" collapse into this.
 */
export function ActivityComposer({
  ticket,
  profiles,
  running,
  onChange,
}: {
  ticket: TicketOut;
  profiles: ProfileOut[];
  running: boolean;
  onChange: () => void;
}) {
  const navigate = useNavigate();
  const continueAvailable = ticket.status === "review" || ticket.status === "archived";
  const [intent, setIntent] = useState<Intent>(continueAvailable ? "continue" : "guidance");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [newConvo, setNewConvo] = useState(false);
  const [restart, setRestart] = useState(false);
  const [clone, setClone] = useState(false);

  // If the ticket becomes non-resumable, fall back to guidance.
  useEffect(() => {
    if (!continueAvailable && intent === "continue") setIntent("guidance");
  }, [continueAvailable, intent]);

  const pending = (ticket.next_run_context ?? "").trim();

  async function act(label: string, fn: () => Promise<unknown>, clear = false) {
    setBusy(true);
    try {
      await fn();
      if (clear) setMessage("");
      toast.success(label);
      onChange();
    } catch (err) {
      toast.error(`${label} failed`, { error: err });
    } finally {
      setBusy(false);
    }
  }

  const submit = () => {
    if (!message.trim()) return;
    if (intent === "continue") {
      act("Continue queued", () => ticketsApi.continue(ticket.id, { message }), true);
    } else {
      act("Guidance saved", () => ticketsApi.setNextRunContext(ticket.id, { body: message }), true);
    }
  };

  const helper =
    intent === "continue"
      ? "Sends now as the next turn on the resumed conversation."
      : running
        ? "Saved for the next run after this one finishes — doesn't interrupt the current run."
        : "Saved as a note the agent sees on its next run — doesn't run anything now.";

  return (
    <div className="shrink-0 border-t border-ink-700 bg-ink-900/60 p-3">
      {pending && (
        <PendingChip
          text={pending}
          updatedAt={ticket.next_run_context_updated_at}
          onEdit={() => {
            setIntent("guidance");
            setMessage(pending);
          }}
          onMerge={() =>
            act("Merged into prompt", () => ticketsApi.mergeNextRunContext(ticket.id))
          }
          onClear={() =>
            act("Guidance cleared", () => ticketsApi.setNextRunContext(ticket.id, { body: "" }))
          }
        />
      )}

      <div className="mb-2 flex items-center gap-2">
        <div className="flex rounded-control border border-ink-700 bg-ink-950 p-0.5 text-xs">
          <Tooltip content={continueAvailable ? "" : "Available once a run has finished (review/archived)"}>
            <button
              onClick={() => continueAvailable && setIntent("continue")}
              disabled={!continueAvailable}
              className={cn(
                "rounded-[6px] px-2.5 py-1 font-medium transition-colors",
                intent === "continue" ? "bg-ink-800 text-moon-100" : "text-moon-400 hover:text-moon-100",
                !continueAvailable && "cursor-not-allowed opacity-40",
              )}
            >
              Continue now
            </button>
          </Tooltip>
          <button
            onClick={() => setIntent("guidance")}
            className={cn(
              "rounded-[6px] px-2.5 py-1 font-medium transition-colors",
              intent === "guidance" ? "bg-ink-800 text-moon-100" : "text-moon-400 hover:text-moon-100",
            )}
          >
            Guidance for next run
          </button>
        </div>

        <div className="ml-auto">
          <RunMenu
            ticket={ticket}
            continueAvailable={continueAvailable}
            onResume={() => act("Resume queued", () => ticketsApi.resume(ticket.id, { message: message || null }))}
            onRetry={() => act("Retry queued", () => ticketsApi.retry(ticket.id, { message: message || null }))}
            onRestart={() => setRestart(true)}
            onNewConversation={() => setNewConvo(true)}
            onClone={() => setClone(true)}
          />
        </div>
      </div>

      <Textarea
        value={message}
        onChange={(e) => setMessage(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit();
        }}
        placeholder={
          intent === "continue"
            ? "Reply to the agent — sent as the next turn…"
            : "Guidance the agent should follow on its next run…"
        }
        className="min-h-[72px]"
      />
      <div className="mt-1.5 flex items-center gap-2">
        <span className="text-[11px] text-moon-600">{helper}</span>
        <Button
          size="sm"
          variant="primary"
          className="ml-auto"
          leadingIcon={intent === "continue" ? <Send size={13} /> : <Sparkles size={13} />}
          disabled={busy || !message.trim()}
          onClick={submit}
        >
          {intent === "continue" ? "Send" : "Save guidance"}
        </Button>
      </div>

      <NewConversationDialog
        open={newConvo}
        ticket={ticket}
        profiles={profiles}
        onClose={() => setNewConvo(false)}
        onDone={onChange}
      />
      <RestartDialog open={restart} ticket={ticket} onClose={() => setRestart(false)} onDone={onChange} />
      <CloneDialog
        open={clone}
        ticket={ticket}
        onClose={() => setClone(false)}
        onCloned={(id) => navigate({ to: "/tickets/$id", params: { id } })}
      />
    </div>
  );
}

function PendingChip({
  text,
  updatedAt,
  onEdit,
  onMerge,
  onClear,
}: {
  text: string;
  updatedAt: string | null;
  onEdit: () => void;
  onMerge: () => void;
  onClear: () => void;
}) {
  return (
    <div className="mb-2 flex items-start gap-2 rounded-control border border-lamp/30 bg-lamp/[0.07] px-2.5 py-2">
      <Sparkles size={13} className="mt-0.5 shrink-0 text-lamp" />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="text-[11px] font-semibold uppercase tracking-wide text-lamp">
            Guidance staged for next run
          </span>
          {updatedAt && <span className="text-[10px] text-moon-600">{relativeTime(updatedAt)}</span>}
        </div>
        <p className="mt-0.5 line-clamp-2 whitespace-pre-wrap text-[12px] text-moon-100">{text}</p>
      </div>
      <div className="flex shrink-0 items-center gap-0.5">
        <Tooltip content="Edit">
          <button
            onClick={onEdit}
            aria-label="Edit guidance"
            className="rounded-control p-1 text-moon-400 hover:bg-ink-800 hover:text-moon-100"
          >
            <Pencil size={12} />
          </button>
        </Tooltip>
        <Tooltip content="Merge into prompt">
          <button
            onClick={onMerge}
            aria-label="Merge guidance into prompt"
            className="rounded-control p-1 text-moon-400 hover:bg-ink-800 hover:text-moon-100"
          >
            <GitMerge size={12} />
          </button>
        </Tooltip>
        <Tooltip content="Clear">
          <button
            onClick={onClear}
            aria-label="Clear guidance"
            className="rounded-control p-1 text-moon-400 hover:bg-ink-800 hover:text-failed"
          >
            <X size={12} />
          </button>
        </Tooltip>
      </div>
    </div>
  );
}

function RunMenu({
  continueAvailable,
  onResume,
  onRetry,
  onRestart,
  onNewConversation,
  onClone,
}: {
  ticket: TicketOut;
  continueAvailable: boolean;
  onResume: () => void;
  onRetry: () => void;
  onRestart: () => void;
  onNewConversation: () => void;
  onClone: () => void;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button size="sm" variant="ghost" trailingIcon={<ChevronDown size={13} />}>
          New run
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" side="top">
        {continueAvailable ? (
          <>
            <DropdownMenuLabel>Same worktree</DropdownMenuLabel>
            <DropdownMenuItem onSelect={onResume}>Resume — fresh context, same files</DropdownMenuItem>
            <DropdownMenuItem onSelect={onRetry}>Retry — re-attempt, same files</DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuLabel>Fresh start</DropdownMenuLabel>
            <DropdownMenuItem onSelect={onNewConversation}>New conversation…</DropdownMenuItem>
            <DropdownMenuItem onSelect={onRestart}>Restart (fresh worktree)…</DropdownMenuItem>
            <DropdownMenuSeparator />
          </>
        ) : null}
        <DropdownMenuItem onSelect={onClone}>Clone ticket…</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
