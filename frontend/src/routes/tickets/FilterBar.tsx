import { useEffect, useRef, useState } from "react";
import { Search, X } from "lucide-react";
import { cn } from "@/lib/cn";
import {
  FILTER_KEYS,
  suggestValues,
  type FilterContext,
  type FilterKey,
  type FilterToken,
} from "./filterModel";

export interface FilterBarProps {
  value: string;
  onChange: (raw: string) => void;
  ctx: FilterContext;
  /** Which filter dimensions this bar offers. Defaults to the Tickets board
   *  vocabulary; the Archive passes its own (no `status:`, adds `outcome:`). */
  keys?: readonly FilterKey[];
  /** Placeholder shown when no tokens are present. */
  placeholder?: string;
}

interface Suggestion {
  label: string;
  /** the fragment to insert (without trailing space handling) */
  insert: string;
}

/**
 * Split the raw filter into *committed* tokens (rendered as pills) and the
 * *draft* still being typed (shown in the input). A `key:value` fragment
 * commits — leaving the input — only once it is followed by whitespace; the
 * trailing fragment stays editable text. Tokens are peeled greedily off the
 * front, and everything from the first non-token fragment onward is kept
 * verbatim as the draft, so multi-word free text and its spaces survive the
 * round-trip through `value`. This is what stops a parsed token from rendering
 * as both a pill AND raw text in the input.
 */
function splitCommitted(
  raw: string,
  keys: readonly FilterKey[],
): { tokens: FilterToken[]; draft: string } {
  const allowed = new Set<string>(keys);
  const tokens: FilterToken[] = [];
  let rest = raw;
  for (;;) {
    // A committed fragment must be followed by whitespace; without a trailing
    // space the fragment is the active draft and is never pilled.
    const m = rest.match(/^(\S+)(\s+)([\s\S]*)$/);
    if (!m) break;
    const frag = m[1];
    const after = m[3];
    const idx = frag.indexOf(":");
    if (idx > 0) {
      const key = frag.slice(0, idx).toLowerCase();
      const val = frag.slice(idx + 1);
      if (allowed.has(key) && val) {
        tokens.push({ key: key as FilterKey, value: val });
        rest = after;
        continue;
      }
    }
    break; // free text (or an incomplete token) — stop; keep the rest verbatim.
  }
  return { tokens, draft: rest };
}

/** Rebuild the raw filter from committed tokens + the input draft. The trailing
 *  space after the tokens keeps the last one committed even when the draft is
 *  empty (otherwise it would re-parse as the active fragment and fall back into
 *  the input). */
function joinValue(tokens: FilterToken[], draft: string): string {
  const t = tokens.map((k) => `${k.key}:${k.value}`).join(" ");
  if (!t) return draft;
  return draft ? `${t} ${draft}` : `${t} `;
}

/** Parse the active (last) fragment to drive suggestions. */
function activeFragment(raw: string): { before: string; frag: string } {
  const m = raw.match(/(^|.*\s)(\S*)$/);
  if (!m) return { before: raw, frag: "" };
  return { before: m[1], frag: m[2] };
}

export function FilterBar({
  value,
  onChange,
  ctx,
  keys = FILTER_KEYS,
  placeholder = "Filter — status: project: label: priority: profile: or free text",
}: FilterBarProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [focused, setFocused] = useState(false);
  const [active, setActive] = useState(0);

  // The global "/" shortcut focuses the filter (dispatched from the keymap).
  useEffect(() => {
    const focus = () => inputRef.current?.focus();
    window.addEventListener("nightdesk:focus-filter", focus);
    return () => window.removeEventListener("nightdesk:focus-filter", focus);
  }, []);

  // Committed tokens become pills; the draft is what stays in the input. The
  // active fragment (for suggestions) lives at the end of the draft, never
  // among the already-committed tokens.
  const { tokens, draft } = splitCommitted(value, keys);
  const { before, frag } = activeFragment(draft);

  // Build suggestions for the active fragment.
  let suggestions: Suggestion[] = [];
  const colon = frag.indexOf(":");
  if (colon > 0) {
    const key = frag.slice(0, colon).toLowerCase();
    if ((keys as readonly string[]).includes(key)) {
      suggestions = suggestValues(key as FilterKey, frag.slice(colon + 1), ctx).map((v) => ({
        label: `${key}:${v}`,
        insert: `${key}:${v}`,
      }));
    }
  } else if (frag.length > 0) {
    suggestions = keys
      .filter((k) => k.startsWith(frag.toLowerCase()))
      .map((k) => ({
        label: `${k}:`,
        insert: `${k}:`,
      }));
  }

  const applySuggestion = (s: Suggestion) => {
    // A completed `key:value` gets a trailing space so it commits to a pill; a
    // bare `key:` stays in the draft so the value can be typed next.
    const needsSpace = s.insert.endsWith(":") ? "" : " ";
    onChange(joinValue(tokens, `${before}${s.insert}${needsSpace}`));
    setActive(0);
    inputRef.current?.focus();
  };

  const removeToken = (i: number) => {
    onChange(joinValue(tokens.filter((_, idx) => idx !== i), draft));
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
        {tokens.map((tok, i) => (
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
          value={draft}
          onChange={(e) => {
            onChange(joinValue(tokens, e.target.value));
            setActive(0);
          }}
          onFocus={() => setFocused(true)}
          onBlur={() => setTimeout(() => setFocused(false), 120)}
          onKeyDown={(e) => {
            // Backspace on an empty draft removes the last committed pill, so a
            // mistaken token can be cleared from the keyboard as well as the ×.
            if (e.key === "Backspace" && draft === "" && tokens.length > 0) {
              e.preventDefault();
              removeToken(tokens.length - 1);
              return;
            }
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
          placeholder={tokens.length ? "" : placeholder}
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
