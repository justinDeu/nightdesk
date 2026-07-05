import { useRef, useState } from "react";
import { Input } from "@/ui/Input";
import { useFsSuggest } from "@/api/fs";
import { cn } from "@/lib/cn";

export interface PathInputProps {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  id?: string;
  invalid?: boolean;
}

/** Directory input with server-backed autocomplete (GET /api/v1/fs/suggest).
 *  Suggestions appear in a popover; click or Tab-through to accept. */
export function PathInput({ value, onChange, placeholder, id, invalid }: PathInputProps) {
  const [open, setOpen] = useState(false);
  const { data } = useFsSuggest(value, open && value.length > 0);
  const blurTimer = useRef<number | undefined>(undefined);

  const matches = data?.matches ?? [];

  return (
    <div className="relative">
      <Input
        id={id}
        mono
        value={value}
        invalid={invalid}
        placeholder={placeholder ?? "/home/you/project"}
        onChange={(e) => {
          onChange(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => {
          blurTimer.current = window.setTimeout(() => setOpen(false), 120);
        }}
        autoComplete="off"
        spellCheck={false}
      />
      {open && matches.length > 0 && (
        <div className="absolute z-30 mt-1 max-h-56 w-full overflow-auto rounded-card border border-ink-700 bg-ink-800 p-1 shadow-[var(--shadow-pop)]">
          {matches.slice(0, 12).map((m) => (
            <button
              key={m}
              type="button"
              onMouseDown={(e) => {
                e.preventDefault();
                window.clearTimeout(blurTimer.current);
                onChange(m);
                setOpen(true);
              }}
              className={cn(
                "block w-full truncate rounded-control px-2 py-1.5 text-left font-mono text-[12px]",
                "text-moon-100 hover:bg-ink-700",
              )}
            >
              {m}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
