import { useMemo, useState } from "react";
import type { KeyboardEvent } from "react";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { Archive, ArrowUp, ChevronLeft, Info, Rocket } from "lucide-react";
import { Button } from "@/ui/Button";
import { Dialog } from "@/ui/Dialog";
import { Field, Input, Textarea } from "@/ui/Input";
import { ErrorState } from "@/ui/ErrorState";
import { Kbd } from "@/ui/Kbd";
import { StatusPill } from "@/ui/StatusPill";
import { toast, describeError } from "@/ui/Toast";
import { confirm } from "@/ui/confirm";
import { TranscriptScroller } from "@/components/TranscriptScroller";
import { useLiveTranscript } from "@/lib/transcript";
import { useProfiles } from "@/api/profiles";
import {
  useArchiveSession,
  usePostSessionMessage,
  usePromoteSession,
  useSession,
} from "@/api/sessions";
import type { TicketStatus } from "@/api/types";

/**
 * One interactive session as a chat: the live transcript scrolls above, a
 * composer anchors the bottom. Each message dispatches immediately (bypasses
 * the schedule window) and the composer is disabled while a turn is in flight.
 */
export function SessionChatPage() {
  const { id } = useParams({ from: "/app/sessions/$id" });
  const navigate = useNavigate();

  const sessionQ = useSession(id, {
    refetchInterval: (q) =>
      q.state.data?.status === "running" || q.state.data?.status === "queued" ? 2000 : false,
  });
  const s = sessionQ.data;
  const profiles = useProfiles();
  const post = usePostSessionMessage(id);
  const archive = useArchiveSession(id);

  const [message, setMessage] = useState("");
  const [promoteOpen, setPromoteOpen] = useState(false);

  const hasConversations = (s?.conversations?.length ?? 0) > 0;
  const tx = useLiveTranscript(`/api/v1/tickets/${id}/transcript`, hasConversations);

  const running = s?.status === "running";
  const queued = s?.status === "queued";
  const busy = running || queued;

  const profile = useMemo(
    () => profiles.data?.find((p) => p.id === s?.profile_id),
    [profiles.data, s?.profile_id],
  );
  const isOpencode = profile?.backend === "opencode";

  const send = async () => {
    const body = message.trim();
    if (!body || busy) return;
    setMessage("");
    try {
      await post.mutateAsync({ message: body });
    } catch (err) {
      setMessage(body); // restore so the user doesn't lose their text
      toast.error("Could not send message", { description: describeError(err) });
    }
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      void send();
    }
  };

  const onArchive = async () => {
    if (!(await confirm({ title: "Archive session?", confirmLabel: "Archive" }))) return;
    try {
      await archive.mutateAsync();
      navigate({ to: "/sessions" });
    } catch (err) {
      toast.error("Could not archive", { description: describeError(err) });
    }
  };

  if (sessionQ.isLoading) {
    return <div className="p-8 text-sm text-moon-600">Loading session…</div>;
  }
  if (sessionQ.isError || !s) {
    return (
      <div className="grid h-full place-items-center px-4">
        <ErrorState
          title="Session not found"
          action={
            <Button asChild variant="ghost">
              <Link to="/sessions">Back to sessions</Link>
            </Button>
          }
        />
      </div>
    );
  }

  return (
    <div className="mx-auto flex h-full w-full max-w-4xl flex-col px-4 py-4 sm:px-6">
      {/* Header */}
      <div className="mb-3 flex items-center gap-3">
        <Button asChild variant="ghost" size="sm">
          <Link to="/sessions" aria-label="Back to sessions">
            <ChevronLeft size={16} />
          </Link>
        </Button>
        <h1 className="min-w-0 flex-1 truncate font-display text-lg font-semibold text-moon-100">
          {s.title}
        </h1>
        <StatusPill status={s.status as TicketStatus} />
        {!busy && (
          <>
            <Button
              variant="ghost"
              size="sm"
              leadingIcon={<Rocket size={14} />}
              onClick={() => setPromoteOpen(true)}
            >
              Promote
            </Button>
            <Button
              variant="ghost"
              size="sm"
              leadingIcon={<Archive size={14} />}
              onClick={onArchive}
            >
              Archive
            </Button>
          </>
        )}
      </div>

      {isOpencode && (
        <div className="mb-3 flex items-start gap-2 rounded-card border border-ink-700 bg-ink-900/60 px-3 py-2 text-xs text-moon-400">
          <Info size={14} className="mt-0.5 shrink-0 text-moon-500" />
          <span>
            This profile uses the opencode backend, which has no resumable chat memory.
            Each turn re-reads the workspace with fresh context rather than replaying the
            conversation.
          </span>
        </div>
      )}

      {/* Transcript */}
      {hasConversations ? (
        <TranscriptScroller
          events={tx.events}
          status={tx.status}
          running={running}
          className="min-h-0 flex-1"
        />
      ) : (
        <div className="flex min-h-0 flex-1 items-center justify-center rounded-card border border-dashed border-ink-700 bg-ink-900/40 text-center text-sm text-moon-500">
          Send the first message to start the conversation.
        </div>
      )}

      {/* Composer */}
      <div className="mt-3">
        <div className="relative">
          <Textarea
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            onKeyDown={onKeyDown}
            disabled={busy}
            placeholder={busy ? "Waiting for the current turn to finish…" : "Send a message…"}
            className="min-h-[64px] pr-28"
            aria-label="Message"
          />
          <Button
            className="absolute bottom-2.5 right-2.5"
            size="sm"
            onClick={send}
            disabled={busy || !message.trim()}
            loading={post.isPending}
            leadingIcon={<ArrowUp size={14} />}
          >
            Send
          </Button>
        </div>
        <div className="mt-1.5 flex items-center gap-2 text-[11px] text-moon-600">
          {queued ? (
            <span className="text-lamp">Dispatching…</span>
          ) : running ? (
            <span className="text-success">Agent is working…</span>
          ) : (
            <span>
              <Kbd>⌘</Kbd>/<Kbd>Ctrl</Kbd> + <Kbd>↵</Kbd> to send
            </span>
          )}
        </div>
      </div>

      <PromoteDialog
        open={promoteOpen}
        onOpenChange={setPromoteOpen}
        sessionId={id}
        defaultTitle={s.title}
      />
    </div>
  );
}

function PromoteDialog({
  open,
  onOpenChange,
  sessionId,
  defaultTitle,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  sessionId: string;
  defaultTitle: string;
}) {
  const navigate = useNavigate();
  const promote = usePromoteSession(sessionId);
  const [title, setTitle] = useState(defaultTitle);

  const submit = async () => {
    if (!title.trim()) {
      toast.error("A title is required");
      return;
    }
    try {
      const t = await promote.mutateAsync({ title: title.trim(), target_status: "review" });
      onOpenChange(false);
      navigate({ to: "/tickets/$id", params: { id: t.id } });
    } catch (err) {
      toast.error("Could not promote", { description: describeError(err) });
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Promote to ticket"
      description="Turn this session into a board ticket, keeping its full conversation and run history."
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={submit} loading={promote.isPending}>
            Promote
          </Button>
        </>
      }
    >
      <Field label="Ticket title">
        <Input value={title} onChange={(e) => setTitle(e.target.value)} autoFocus />
      </Field>
    </Dialog>
  );
}
