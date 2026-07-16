import { useMemo, useRef } from "react";
import type { ReactNode, TextareaHTMLAttributes } from "react";
import { createLowlight } from "lowlight";
import markdown from "highlight.js/lib/languages/markdown";
import { segments } from "@/components/MarkdownSource";
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
 * Metrics-safe variant of MarkdownSource's TOKEN_CLASS: token classes may only
 * recolor / reweight, never add padding/margins or change font-size (that would
 * shift glyph advance out from under the transparent textarea text). The inline
 * `code` entry is color-only — no background — so a wrapped inline span can't
 * stripe the line-height gaps; fenced blocks get a contiguous block background
 * instead. font-semibold / italic are safe — IBM Plex Mono holds advance widths
 * across weight and slant, so bold/italic tokens don't drift.
 */
const TOKEN_CLASS: Record<string, string> = {
  section: "font-semibold text-lamp", // # headers, hash marks included
  bullet: "text-lamp", // -, *, 1. list markers
  strong: "font-semibold text-moon-100", // **bold**, markers in place
  emphasis: "italic text-moon-100", // *italic*
  code: "text-dawn", // `inline code` — color only (no bg; would stripe)
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

/**
 * A segment block whose text is empty or ends in "\n" would collapse its final
 * (empty) line box in HTML, so the highlight layer would come up one line short
 * of the textarea. Emit a trailing zero-width space to keep that line rendered.
 */
function pad(text: string): ReactNode {
  return text === "" || text.endsWith("\n") ? "​" : null;
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

  // Split on fenced ``` blocks so code blocks get a contiguous block background
  // (an inline `bg` span only paints each line's text box, striping the
  // line-height gaps). The splitter consumes the \n between segments; stacking
  // the segment blocks reproduces exactly those breaks — same trick as
  // MarkdownSource's per-segment <pre>s — so the flow stays char-for-char with
  // the textarea.
  const segs = useMemo(() => segments(value), [value]);

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
        className="pointer-events-none absolute inset-0 overflow-hidden px-3 py-2 font-mono text-[12.5px] leading-relaxed text-moon-100 [scrollbar-gutter:stable]"
      >
        {segs.map((s, i) => {
          if (s.code) {
            // Exact source text of this fenced block, reconstructed from the raw
            // marker lines so trailing whitespace ("```js  ") survives and a
            // bare open fence ("```" at EOF) gets no phantom newline.
            const text =
              s.open + (s.bodyStarted ? "\n" + s.body : "") + (s.closed ? "\n" + s.close : "");
            return (
              // Block-level contiguous background. The -mx-1/px-1 pair is net-zero:
              // the padding widens only the paint, the negative margin pulls the
              // box back so glyph x-positions don't move. No vertical padding, no
              // border, no font-size change — vertical flow stays aligned.
              <div
                key={i}
                className="-mx-1 whitespace-pre-wrap break-words rounded bg-ink-800/70 px-1"
              >
                <span className="text-moon-600">{s.open}</span>
                {s.bodyStarted && "\n" + s.body}
                {s.closed && <span className="text-moon-600">{"\n" + s.close}</span>}
                {pad(text)}
              </div>
            );
          }
          return (
            <div key={i} className="whitespace-pre-wrap break-words">
              {highlight(s.body)}
              {pad(s.body)}
            </div>
          );
        })}
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
