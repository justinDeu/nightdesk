import { useEffect, useRef, useState } from "react";
import { Search, X } from "lucide-react";
import { cn } from "@/lib/cn";
import {
  FILTER_KEYS,
  parseFilter,
  serializeFilter,
  suggestValues,
  type FilterContext,
  type FilterKey,
} from "./filterModel";

export interface FilterBarProps {
  value: string;
  onChange: (raw: string) => void;
  ctx: FilterContext;
}

interface Suggestion {
  label: string;
  /** the fragment to insert (without trailing space handling) */
  insert: string;
}

/** Parse the active (last) fragment to drive suggestions. */
function activeFragment(raw: string): { before: string; frag: string } {
  const m = raw.match(/(^|.*\s)(\S*)$/);
  if (!m) return { before: raw, frag: "" };
  return { before: m[1], frag: m[2] };
}

export function FilterBar({ value, onChange, ctx }: FilterBarProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [focused, setFocused] = useState(false);
  const [active, setActive] = useState(0);

  // The global "/" shortcut focuses the filter (dispatched from the keymap).
  useEffect(() => {
    const focus = () => inputRef.current?.focus();
    window.addEventListener("nightdesk:focus-filter", focus);
    return () => window.removeEventListener("nightdesk:focus-filter", focus);
  }, []);

  const parsed = parseFilter(value);
  const { before, frag } = activeFragment(value);

  // Build suggestions for the active fragment.
  let suggestions: Suggestion[] = [];
  const colon = frag.indexOf(":");
  if (colon > 0) {
    const key = frag.slice(0, colon).toLowerCase();
    if ((FILTER_KEYS as readonly string[]).includes(key)) {
      suggestions = suggestValues(key as FilterKey, frag.slice(colon + 1), ctx).map((v) => ({
        label: `${key}:${v}`,
        insert: `${key}:${v}`,
      }));
    }
  } else if (frag.length > 0) {
    suggestions = FILTER_KEYS.filter((k) => k.startsWith(frag.toLowerCase())).map((k) => ({
      label: `${k}:`,
      insert: `${k}:`,
    }));
  }

  const applySuggestion = (s: Suggestion) => {
    const needsSpace = s.insert.endsWith(":") ? "" : " ";
    onChange(`${before}${s.insert}${needsSpace}`);
    setActive(0);
    inputRef.current?.focus();
  };

  const removeToken = (i: number) => {
    const next = { ...parsed, tokens: parsed.tokens.filter((_, idx) => idx !== i) };
    onChange(serializeFilter(next));
  };

  const showMenu = focused && suggestions.length > 0;

  return (
    <div className="relative flex-1">
      <div
        className={cn(
          "flex min-h-9 flex-wrap items-center gap-1.5 rounded-control border bg-ink-950 px-2.5 py-1",
          focused ? "border-lamp" : "border-ink-700",
        )}
        onClick={() => inputRef.current?.focus()}
      >
        <Search size={14} className="text-moon-400" />
        {parsed.tokens.map((tok, i) => (
          <span
            key={`${tok.key}-${tok.value}-${i}`}
            className="inline-flex items-center gap-1 rounded-full border border-ink-700 bg-ink-800 px-2 py-0.5 text-[11px] text-moon-100"
          >
            <span className="text-moon-400">{tok.key}:</span>
            {tok.value}
            <button
              type="button"
              className="text-moon-600 hover:text-moon-100"
              onClick={(e) => {
                e.stopPropagation();
                removeToken(i);
              }}
              aria-label={`Remove ${tok.key} filter`}
            >
              <X size={11} />
            </button>
          </span>
        ))}
        <input
          ref={inputRef}
          value={value}
          onChange={(e) => {
            onChange(e.target.value);
            setActive(0);
          }}
          onFocus={() => setFocused(true)}
          onBlur={() => setTimeout(() => setFocused(false), 120)}
          onKeyDown={(e) => {
            if (!showMenu) return;
            if (e.key === "ArrowDown") {
              e.preventDefault();
              setActive((a) => Math.min(suggestions.length - 1, a + 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setActive((a) => Math.max(0, a - 1));
            } else if (e.key === "Enter" || e.key === "Tab") {
              if (suggestions[active]) {
                e.preventDefault();
                applySuggestion(suggestions[active]);
              }
            }
          }}
          placeholder={parsed.tokens.length ? "" : "Filter — status: project: label: priority: profile: or free text"}
          className="min-w-[8rem] flex-1 bg-transparent text-sm text-moon-100 placeholder:text-moon-600 focus:outline-none"
          spellCheck={false}
          autoComplete="off"
        />
        {value && (
          <button
            type="button"
            className="text-moon-600 hover:text-moon-100"
            onClick={() => onChange("")}
            aria-label="Clear filter"
          >
            <X size={14} />
          </button>
        )}
      </div>

      {showMenu && (
        <div className="absolute z-30 mt-1 w-72 overflow-hidden rounded-card border border-ink-700 bg-ink-800 p-1 shadow-[var(--shadow-pop)]">
          {suggestions.map((s, i) => (
            <button
              key={s.insert}
              type="button"
              onMouseDown={(e) => {
                e.preventDefault();
                applySuggestion(s);
              }}
              className={cn(
                "block w-full truncate rounded-control px-2 py-1.5 text-left font-mono text-[12px]",
                i === active ? "bg-ink-700 text-moon-100" : "text-moon-400 hover:bg-ink-700",
              )}
            >
              {s.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
