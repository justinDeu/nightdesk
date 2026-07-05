import { useState } from "react";
import { Plus, X } from "lucide-react";
import { Input } from "@/ui/Input";
import { cn } from "@/lib/cn";

/**
 * Edits an ordered list of short string values (paths, tool names, hostnames)
 * as removable chips with an add-field. Enter or the + button appends; the ×
 * on a chip removes it. Values are trimmed and de-duplicated.
 */
export function ListEditor({
  value,
  onChange,
  placeholder,
  mono = true,
  addLabel = "Add",
  emptyHint,
}: {
  value: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  mono?: boolean;
  addLabel?: string;
  emptyHint?: string;
}) {
  const [draft, setDraft] = useState("");

  function add() {
    const v = draft.trim();
    if (!v) return;
    if (!value.includes(v)) onChange([...value, v]);
    setDraft("");
  }

  return (
    <div className="space-y-2">
      {value.length > 0 ? (
        <ul className="flex flex-wrap gap-1.5">
          {value.map((item, i) => (
            <li
              key={`${item}-${i}`}
              className={cn(
                "inline-flex items-center gap-1.5 rounded-control border border-ink-700 bg-ink-800 py-1 pl-2.5 pr-1 text-[12px] text-moon-100",
                mono && "font-mono",
              )}
            >
              <span className="max-w-[22rem] truncate">{item}</span>
              <button
                type="button"
                aria-label={`Remove ${item}`}
                onClick={() => onChange(value.filter((_, idx) => idx !== i))}
                className="rounded p-0.5 text-moon-400 hover:bg-ink-700 hover:text-moon-100"
              >
                <X size={13} />
              </button>
            </li>
          ))}
        </ul>
      ) : (
        emptyHint && <p className="text-xs text-moon-600">{emptyHint}</p>
      )}
      <div className="flex items-center gap-2">
        <Input
          mono={mono}
          value={draft}
          placeholder={placeholder}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              add();
            }
          }}
          className="h-8"
        />
        <button
          type="button"
          onClick={add}
          disabled={!draft.trim()}
          className="inline-flex h-8 shrink-0 items-center gap-1 rounded-control border border-ink-700 px-2.5 text-xs text-moon-100 hover:bg-ink-800 disabled:opacity-45 disabled:pointer-events-none"
        >
          <Plus size={13} />
          {addLabel}
        </button>
      </div>
    </div>
  );
}
