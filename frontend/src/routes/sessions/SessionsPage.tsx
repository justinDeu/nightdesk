import { useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { MessagesSquare, Plus } from "lucide-react";
import { Page } from "@/components/Page";
import { Button } from "@/ui/Button";
import { Dialog } from "@/ui/Dialog";
import { Field, Input } from "@/ui/Input";
import { Select } from "@/ui/Select";
import { EmptyState } from "@/ui/EmptyState";
import { ErrorState } from "@/ui/ErrorState";
import { StatusPill } from "@/ui/StatusPill";
import { Spinner } from "@/ui/Spinner";
import { PathInput } from "@/components/PathInput";
import { toast, describeError } from "@/ui/Toast";
import { useProfiles } from "@/api/profiles";
import { useCreateSession, useSessions } from "@/api/sessions";
import { relativeTime } from "@/lib/time";
import type { SessionOut, TicketStatus } from "@/api/types";

/** Interactive sessions: ad-hoc, chat-first agent conversations against a
 *  profile, with no ticket authoring. Each row opens a live chat page. */
export function SessionsPage() {
  const navigate = useNavigate();
  const sessionsQ = useSessions({
    refetchInterval: (q) =>
      (q.state.data ?? []).some((s) => s.status === "running" || s.status === "queued")
        ? 3000
        : false,
  });
  const [dialogOpen, setDialogOpen] = useState(false);

  return (
    <Page
      title="Sessions"
      subtitle="Ad-hoc chats with an agent — no ticket required."
      width="wide"
      actions={
        <Button leadingIcon={<Plus size={15} />} onClick={() => setDialogOpen(true)}>
          New session
        </Button>
      }
    >
      {sessionsQ.isError ? (
        <ErrorState
          title="Could not load sessions"
          action={<Button variant="ghost" onClick={() => sessionsQ.refetch()}>Retry</Button>}
        />
      ) : sessionsQ.isLoading ? (
        <div className="p-8 text-sm text-moon-600">Loading sessions…</div>
      ) : (sessionsQ.data?.length ?? 0) === 0 ? (
        <EmptyState
          icon={<MessagesSquare size={20} />}
          title="No sessions yet"
          description="Start an interactive chat with an agent against one of your profiles."
          action={
            <Button leadingIcon={<Plus size={15} />} onClick={() => setDialogOpen(true)}>
              New session
            </Button>
          }
        />
      ) : (
        <ul className="flex flex-col gap-2">
          {sessionsQ.data!.map((s) => (
            <SessionRow key={s.id} session={s} onOpen={() => navigate({ to: "/sessions/$id", params: { id: s.id } })} />
          ))}
        </ul>
      )}

      <NewSessionDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        onCreated={(id) => {
          setDialogOpen(false);
          navigate({ to: "/sessions/$id", params: { id } });
        }}
      />
    </Page>
  );
}

function SessionRow({ session, onOpen }: { session: SessionOut; onOpen: () => void }) {
  const busy = session.status === "running" || session.status === "queued";
  return (
    <li>
      <button
        onClick={onOpen}
        className="flex w-full items-center gap-3 rounded-card border border-ink-700 bg-ink-900/60 px-4 py-3 text-left transition-colors hover:border-ink-600 hover:bg-ink-800/60"
      >
        <MessagesSquare size={16} className="shrink-0 text-moon-500" />
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-moon-100">
          {session.title}
        </span>
        {busy && <Spinner className="h-3.5 w-3.5 text-lamp" />}
        <StatusPill status={session.status as TicketStatus} />
        <span className="w-24 shrink-0 text-right font-mono text-[11px] text-moon-600">
          {relativeTime(session.updated_at)}
        </span>
      </button>
    </li>
  );
}

function NewSessionDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onCreated: (id: string) => void;
}) {
  const profiles = useProfiles();
  const create = useCreateSession();
  const [title, setTitle] = useState("");
  const [profileId, setProfileId] = useState("");
  const [sourcePath, setSourcePath] = useState("");

  const effectiveProfile = profileId || profiles.data?.[0]?.id || "";

  const submit = async () => {
    if (!effectiveProfile) {
      toast.error("Pick a profile first");
      return;
    }
    try {
      const s = await create.mutateAsync({
        title: title.trim() || undefined,
        profile_id: effectiveProfile,
        source_path: sourcePath.trim() || undefined,
      });
      setTitle("");
      setSourcePath("");
      onCreated(s.id);
    } catch (err) {
      toast.error("Could not start session", { description: describeError(err) });
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="New session"
      description="Chat with an agent against a profile and an optional working directory."
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={submit} loading={create.isPending}>
            Start session
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <Field label="Title" hint="Optional — defaults to “Session”.">
          <Input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Session"
          />
        </Field>
        <Field label="Profile">
          <Select
            value={effectiveProfile}
            onChange={(e) => setProfileId(e.target.value)}
          >
            {(profiles.data ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} ({p.backend})
              </option>
            ))}
          </Select>
        </Field>
        <Field
          label="Working directory"
          hint="Optional — leave blank for a fresh scratch directory. A path runs the agent in-place on that directory."
        >
          <PathInput value={sourcePath} onChange={setSourcePath} placeholder="/path/to/repo (optional)" />
        </Field>
      </div>
    </Dialog>
  );
}
