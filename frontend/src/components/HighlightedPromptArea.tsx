import { useMemo, useRef } from "react";
import type { ReactNode, TextareaHTMLAttributes } from "react";
import { createLowlight } from "lowlight";
import markdown from "highlight.js/lib/languages/markdown";
import { cn } from "@/lib/cn";

/**
 * An editable <textarea> that renders its own value in the app mono font with
 * live markdown syntax highlighting — the same terminal-harness look as
 * MarkdownSource, but stays a plain, fully editable textarea.
 *
 * Overlay-editor pattern: an aria-hidden highlight layer paints the tokenised
 * value, and a transparent-text textarea sits directly on top so the real caret
 * and selection show through onto the coloured glyphs beneath. For the overlay
 * to stay pixel-aligned, BOTH layers must be typographically identical — same
 * font, size, line-height, padding, and wrapping — and no token class may add
 * padding/margins or change font-size (that would shift glyph advance).
 */
const lowlight = createLowlight({ markdown });

/**
 * hljs markdown scope (sans `hljs-` prefix) → app token classes.
 *
 * Metrics-safe variant of MarkdownSource's TOKEN_CLASS: the `code` entry keeps
 * only background+color (no px-1/py-px/rounded) because any padding on an inline
 * span would nudge the following glyphs out from under the transparent textarea
 * text. font-semibold / italic are safe — IBM Plex Mono holds advance widths
 * across weight and slant, so bold/italic tokens don't drift.
 */
const TOKEN_CLASS: Record<string, string> = {
  section: "font-semibold text-lamp", // # headers, hash marks included
  bullet: "text-lamp", // -, *, 1. list markers
  strong: "font-semibold text-moon-100", // **bold**, markers in place
  emphasis: "italic text-moon-100", // *italic*
  code: "bg-ink-800 text-dawn", // `inline code` — no padding/radius (metrics)
  link: "text-lamp underline decoration-lamp/40 underline-offset-2", // (url)
  string: "text-lamp", // [link text]
  symbol: "text-moon-400", // reference labels
  quote: "italic text-moon-400", // > blockquote lines
};

/** Minimal hast shape (avoids depending on @types/hast directly). */
interface HastNode {
  type: string;
  value?: string;
  properties?: { className?: unknown };
  children?: HastNode[];
}

function classesFor(node: HastNode): string {
  const raw = node.properties?.className;
  const names = Array.isArray(raw) ? raw.map(String) : [];
  return names
    .map((c) => TOKEN_CLASS[c.replace(/^hljs-/, "")])
    .filter(Boolean)
    .join(" ");
}

function renderHast(nodes: HastNode[], keyPrefix: string): ReactNode[] {
  return nodes.map((n, i) => {
    if (n.type === "text") return n.value ?? "";
    if (n.type === "element") {
      const key = `${keyPrefix}-${i}`;
      return (
        <span key={key} className={classesFor(n) || undefined}>
          {renderHast(n.children ?? [], key)}
        </span>
      );
    }
    return null;
  });
}

function highlight(src: string): ReactNode[] {
  const tree = lowlight.highlight("markdown", src) as unknown as HastNode;
  return renderHast(tree.children ?? [], "md");
}

export interface HighlightedPromptAreaProps
  extends Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "value" | "onChange"> {
  value: string;
  onChange: (e: React.ChangeEvent<HTMLTextAreaElement>) => void;
  /** Passthrough for the wrapper — the caller supplies flex sizing here. */
  className?: string;
}

export function HighlightedPromptArea({
  value,
  onChange,
  className,
  placeholder,
  onScroll,
  ...rest
}: HighlightedPromptAreaProps) {
  const highlightRef = useRef<HTMLDivElement>(null);

  // A value ending in "\n" collapses its final (empty) line in HTML, so the
  // highlight layer would be one line shorter than the textarea. Append a
  // zero-width space to keep the trailing line rendered.
  const rendered = useMemo(
    () => highlight(value.endsWith("\n") ? value + "​" : value),
    [value],
  );

  return (
    <div
      className={cn(
        "relative overflow-hidden rounded-control border border-ink-700 bg-ink-950",
        "transition-colors duration-100 focus-within:border-lamp",
        className,
      )}
    >
      {/* Highlight layer — painted underneath, never interactive. */}
      <div
        ref={highlightRef}
        aria-hidden
        className="pointer-events-none absolute inset-0 overflow-hidden whitespace-pre-wrap break-words px-3 py-2 font-mono text-[12.5px] leading-relaxed text-moon-100 [scrollbar-gutter:stable]"
      >
        {rendered}
      </div>
      {/* Real editor — transparent glyphs, visible caret over the paint. */}
      <textarea
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        spellCheck={false}
        onScroll={(e) => {
          const el = e.currentTarget;
          const layer = highlightRef.current;
          if (layer) {
            layer.scrollTop = el.scrollTop;
            layer.scrollLeft = el.scrollLeft;
          }
          onScroll?.(e);
        }}
        className={cn(
          "absolute inset-0 h-full w-full resize-none overflow-auto border-0 bg-transparent px-3 py-2 [scrollbar-gutter:stable]",
          "font-mono text-[12.5px] leading-relaxed text-transparent caret-lamp",
          "placeholder:text-moon-600 focus:outline-none",
        )}
        {...rest}
      />
    </div>
  );
}
