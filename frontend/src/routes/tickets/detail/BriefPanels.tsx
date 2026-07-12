import { useEffect, useState } from "react";
import { ChevronRight, Pencil } from "lucide-react";
import { Button } from "@/ui/Button";
import { Textarea } from "@/ui/Input";
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
