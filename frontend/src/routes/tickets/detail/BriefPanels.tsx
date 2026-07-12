import { useEffect, useState } from "react";
import { ChevronRight, Maximize2, Pencil } from "lucide-react";
import { Button } from "@/ui/Button";
import { Textarea } from "@/ui/Input";
import { Dialog } from "@/ui/Dialog";
import { useUnsavedGuard } from "@/lib/useUnsavedGuard";
import { cn } from "@/lib/cn";
import type { TicketOut } from "@/api/types";

/**
 * The ticket's "brief" — the human description and the agent prompt — as
 * collapsible, in-place-editable disclosure cards.
 *
 * One shell serves both placements: collapsed to a single line in the metadata
 * rail (so it never crowds the transcript) and expanded as the hero of the
 * center stage while a ticket is still being drafted. Whichever placement, the
 * brief stays one click away from editing and never participates in the
 * transcript's scroll.
 */

export function DescriptionPanel({
  ticket,
  onSave,
  defaultOpen = false,
}: {
  ticket: TicketOut;
  onSave: (v: string) => void;
  defaultOpen?: boolean;
}) {
  return (
    <BriefPanel
      label="Description"
      value={ticket.description ?? ""}
      placeholder="What is this ticket, and why? Written for a human scanning the board and review."
      emptyHint="No description yet — click to add the what/why for a human."
      saveLabel="Save description"
      textareaClass="min-h-[120px]"
      defaultOpen={defaultOpen}
      onSave={onSave}
      trim
    />
  );
}

export function PromptPanel({
  ticket,
  onSave,
  defaultOpen = false,
}: {
  ticket: TicketOut;
  onSave: (v: string) => void;
  defaultOpen?: boolean;
}) {
  return (
    <BriefPanel
      label="Agent prompt"
      value={ticket.prompt}
      placeholder="The instructions the agent actually runs."
      emptyHint="No prompt yet — click to write one."
      saveLabel="Save prompt"
      textareaClass="min-h-[160px]"
      defaultOpen={defaultOpen}
      onSave={onSave}
    />
  );
}

function BriefPanel({
  label,
  value,
  placeholder,
  emptyHint,
  saveLabel,
  textareaClass,
  defaultOpen,
  onSave,
  trim = false,
}: {
  label: string;
  value: string;
  placeholder: string;
  emptyHint: string;
  saveLabel: string;
  textareaClass: string;
  defaultOpen: boolean;
  onSave: (v: string) => void;
  trim?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);

  useUnsavedGuard(editing && draft !== value);

  const hasText = value.trim().length > 0;
  const editLabel = `Edit ${label.toLowerCase()}`;

  const beginEdit = () => {
    setEditing(true);
    setOpen(true);
  };
  const save = () => {
    onSave(trim ? draft.trim() : draft);
    setEditing(false);
  };
  const cancel = () => {
    setDraft(value);
    setEditing(false);
  };

  return (
    <div className="overflow-hidden rounded-card border border-ink-700 bg-ink-900">
      <div className="flex items-center gap-2 px-3 py-2">
        <button
          onClick={() => setOpen((o) => !o)}
          className="flex flex-1 items-center gap-1.5 text-left text-[11px] font-semibold uppercase tracking-wide text-moon-400 hover:text-moon-100"
          aria-expanded={open}
        >
          <ChevronRight size={12} className={cn("transition-transform", open && "rotate-90")} />
          {label}
        </button>
        {!editing && (
          <button
            onClick={beginEdit}
            className="rounded-control p-1 text-moon-600 hover:bg-ink-800 hover:text-moon-100"
            aria-label={editLabel}
          >
            <Pencil size={12} />
          </button>
        )}
      </div>
      {open && (
        <div className="border-t border-ink-700/60 px-3 py-2.5">
          {editing ? (
            <div className="space-y-2">
              <Textarea
                autoFocus
                className={textareaClass}
                placeholder={placeholder}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
              />
              <div className="flex gap-2">
                <Button size="sm" variant="primary" onClick={save}>
                  {saveLabel}
                </Button>
                <Button size="sm" variant="ghost" onClick={cancel}>
                  Cancel
                </Button>
              </div>
            </div>
          ) : hasText ? (
            <p className="whitespace-pre-wrap text-[13px] leading-relaxed text-moon-100">{value}</p>
          ) : (
            <button onClick={beginEdit} className="text-sm italic text-moon-600 hover:text-moon-400">
              {emptyHint}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Rail placement for the agent prompt once a ticket has runs: a single-line row
 * (label + preview snippet + open icon) that opens a large dialog instead of
 * expanding inside the narrow rail. Long prompts are unreadable in a 264px
 * column, so the body lives in a wide, internally-scrolling, monospaced dialog.
 * Edit happens inside the dialog with the same unsaved-guard behavior the
 * collapsible BriefPanel uses.
 */
export function PromptRailRow({
  ticket,
  onSave,
}: {
  ticket: TicketOut;
  onSave: (v: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const prompt = ticket.prompt ?? "";
  const hasText = prompt.trim().length > 0;
  const firstLine = prompt.trim().split("\n").find((l) => l.trim()) ?? "";

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="group flex w-full items-center gap-2 rounded-card border border-ink-700 bg-ink-900 px-3 py-2 text-left hover:bg-ink-800"
        aria-label="View agent prompt"
      >
        <span className="shrink-0 text-[11px] font-semibold uppercase tracking-wide text-moon-400">
          Agent prompt
        </span>
        <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-moon-600">
          {hasText ? firstLine : "No prompt yet — click to write one"}
        </span>
        <Maximize2
          size={12}
          className="shrink-0 text-moon-600 group-hover:text-moon-100"
        />
      </button>
      <PromptDialog
        open={open}
        onOpenChange={setOpen}
        value={prompt}
        onSave={onSave}
      />
    </>
  );
}

function PromptDialog({
  open,
  onOpenChange,
  value,
  onSave,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  value: string;
  onSave: (v: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);

  useUnsavedGuard(editing && draft !== value);

  const save = () => {
    onSave(draft);
    setEditing(false);
  };
  const cancel = () => {
    setDraft(value);
    setEditing(false);
  };
  const close = () => {
    onOpenChange(false);
  };

  const hasText = value.trim().length > 0;

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Agent prompt"
      description="The instructions the agent actually runs."
      size="xl"
      footer={
        editing ? (
          <>
            <Button size="sm" variant="ghost" onClick={cancel}>
              Cancel
            </Button>
            <Button size="sm" variant="primary" onClick={save}>
              Save prompt
            </Button>
          </>
        ) : (
          <>
            <Button size="sm" variant="ghost" onClick={close}>
              Close
            </Button>
            <Button
              size="sm"
              variant="primary"
              leadingIcon={<Pencil size={13} />}
              onClick={() => setEditing(true)}
            >
              Edit
            </Button>
          </>
        )
      }
    >
      {editing ? (
        <Textarea
          autoFocus
          mono
          className="h-[44vh] max-h-[55dvh]"
          placeholder="The instructions the agent actually runs."
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
        />
      ) : (
        // Body scrolls internally (capped at 55dvh) so the title + footer stay
        // fixed and the dialog never grows past the viewport. Mono + preserved
        // whitespace so prompts read like they do in the transcript.
        <div className="max-h-[55dvh] overflow-y-auto overscroll-contain rounded-control border border-ink-700/60 bg-ink-950/60 p-3">
          {hasText ? (
            <pre className="whitespace-pre-wrap break-words font-mono text-[13px] leading-relaxed text-moon-100">
              {value}
            </pre>
          ) : (
            <p className="text-sm italic text-moon-600">
              No prompt yet — click Edit to write one.
            </p>
          )}
        </div>
      )}
    </Dialog>
  );
}
