import { useState } from "react";
import type { ProfileOut, TicketOut } from "@/api/types";
import { ticketsApi } from "@/api/tickets";
import { Button } from "@/ui/Button";
import { Textarea, Input, Field } from "@/ui/Input";
import { Select } from "@/ui/Select";
import { Dialog } from "@/ui/Dialog";
import { toast } from "@/ui/Toast";

/** Start a fresh conversation (new session, no resumed history). */
export function NewConversationDialog({
  open,
  ticket,
  profiles,
  onClose,
  onDone,
}: {
  open: boolean;
  ticket: TicketOut;
  profiles: ProfileOut[];
  onClose: () => void;
  onDone: () => void;
}) {
  const [message, setMessage] = useState("");
  const [profileId, setProfileId] = useState(ticket.profile_id ?? "");
  const [workspace, setWorkspace] = useState<"keep" | "fresh">("keep");
  const [busy, setBusy] = useState(false);

  async function submit() {
    setBusy(true);
    try {
      await ticketsApi.newConversation(ticket.id, {
        message: message || null,
        profile_id: profileId || null,
        workspace,
      });
      toast.success("New conversation queued");
      onClose();
      onDone();
    } catch (err) {
      toast.error("Could not start conversation", { error: err });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => !o && onClose()}
      title="New conversation"
      description="Fresh session — no resumed history. Use this to switch runtime or start clean."
      size="md"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={busy} onClick={submit}>
            Start
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <Field label="Opening message (optional)">
          <Textarea value={message} onChange={(e) => setMessage(e.target.value)} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Profile">
            <Select value={profileId} onChange={(e) => setProfileId(e.target.value)}>
              {profiles.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Workspace">
            <Select value={workspace} onChange={(e) => setWorkspace(e.target.value as "keep" | "fresh")}>
              <option value="keep">Keep current files</option>
              <option value="fresh">Fresh worktree</option>
            </Select>
          </Field>
        </div>
      </div>
    </Dialog>
  );
}

/** Fresh worktree + fresh context — start over from the original prompt. */
export function RestartDialog({
  open,
  ticket,
  onClose,
  onDone,
}: {
  open: boolean;
  ticket: TicketOut;
  onClose: () => void;
  onDone: () => void;
}) {
  const [message, setMessage] = useState("");
  const [policy, setPolicy] = useState<"recreate_in_place" | "fresh_path">("recreate_in_place");
  const [busy, setBusy] = useState(false);

  async function submit() {
    setBusy(true);
    try {
      await ticketsApi.restart(ticket.id, { message: message || null, workspace_policy: policy });
      toast.success("Restart queued");
      onClose();
      onDone();
    } catch (err) {
      toast.error("Could not restart", { error: err });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => !o && onClose()}
      title="Restart ticket"
      description="Fresh worktree and fresh context — start over from the original prompt."
      size="md"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={busy} onClick={submit}>
            Restart
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <Field label="Workspace">
          <Select value={policy} onChange={(e) => setPolicy(e.target.value as typeof policy)}>
            <option value="recreate_in_place">Recreate in place (same path)</option>
            <option value="fresh_path">Fresh path (new worktree)</option>
          </Select>
        </Field>
        <Field label="Steering note (optional)">
          <Textarea value={message} onChange={(e) => setMessage(e.target.value)} />
        </Field>
      </div>
    </Dialog>
  );
}

/** Create a fresh copy of the ticket. */
export function CloneDialog({
  open,
  ticket,
  onClose,
  onCloned,
}: {
  open: boolean;
  ticket: TicketOut;
  onClose: () => void;
  onCloned: (id: string) => void;
}) {
  const [title, setTitle] = useState("");
  const [carry, setCarry] = useState(false);
  const [busy, setBusy] = useState(false);

  async function submit() {
    setBusy(true);
    try {
      const cloned = await ticketsApi.clone(ticket.id, {
        title: title.trim() || null,
        carry_context: carry,
      });
      toast.success("Ticket cloned");
      onClose();
      onCloned(cloned.id);
    } catch (err) {
      toast.error("Could not clone", { error: err });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => !o && onClose()}
      title="Clone ticket"
      description="Create a fresh copy with the same prompt and settings."
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={busy} onClick={submit}>
            Clone
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <Field label="New title (optional)">
          <Input value={title} onChange={(e) => setTitle(e.target.value)} placeholder={`Copy of ${ticket.title}`} />
        </Field>
        <label className="flex items-center gap-2 text-sm text-moon-100">
          <input type="checkbox" checked={carry} onChange={(e) => setCarry(e.target.checked)} className="accent-lamp" />
          Carry over the conversation context
        </label>
      </div>
    </Dialog>
  );
}
