import { useMemo } from "react";
import type { ReactNode } from "react";
import { createLowlight } from "lowlight";
import markdown from "highlight.js/lib/languages/markdown";
import { cn } from "@/lib/cn";

/**
 * Terminal-harness rendering of model prose: the RAW markdown source in the
 * app mono font with pseudo-syntax highlighting mapped to the design tokens —
 * headers tinted+bold with their # marks visible, emphasis markers styled in
 * place, inline `code` as tinted chips, list markers and links tinted.
 *
 * Tokenizer choice: lowlight (highlight.js) with only the markdown grammar
 * registered — a first-class tokenizer instead of hand-rolled regex, and far
 * lighter than shiki for a live log. Its one coarse spot is fenced blocks
 * (hljs folds a whole ``` block into a single `code` token), so a small fence
 * splitter runs first: fenced segments render as the bordered ink-950 card
 * (fence markers kept visible, overflow-x for wide lines) and only the prose
 * between fences goes through the markdown grammar.
 */
const lowlight = createLowlight({ markdown });

/** hljs markdown scope (sans `hljs-` prefix) → app token classes. */
const TOKEN_CLASS: Record<string, string> = {
  section: "font-semibold text-lamp", // # headers, hash marks included
  bullet: "text-lamp", // -, *, 1. list markers
  strong: "font-semibold text-moon-100", // **bold**, markers in place
  emphasis: "italic text-moon-100", // *italic*
  code: "rounded bg-ink-800 px-1 py-px text-dawn", // `inline code` chips
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

function highlightProse(src: string): ReactNode[] {
  const tree = lowlight.highlight("markdown", src) as unknown as HastNode;
  return renderHast(tree.children ?? [], "md");
}

// --- Fence splitter --------------------------------------------------------------

export interface Segment {
  code: boolean;
  lang: string;
  body: string;
  /** whether a code segment saw its closing fence (unterminated output while
   *  streaming shouldn't invent one). */
  closed: boolean;
  /** Raw opening fence line, verbatim — e.g. "```js  " keeping trailing space;
   *  "" for prose. An editor overlay repaints this exactly rather than
   *  re-synthesizing "```" + lang (which would drop trailing whitespace). */
  open: string;
  /** Raw closing fence line, verbatim; "" when unclosed or prose. */
  close: string;
  /** Whether a line followed the opening fence (so a newline separates the
   *  marker from the body). False only for a bare open fence at end of input
   *  ("```" typed as the last line) — lets an editor avoid painting a phantom
   *  newline after the marker. */
  bodyStarted: boolean;
}

export function segments(text: string): Segment[] {
  if (!text) return [];
  const out: Segment[] = [];
  let buf: string[] = [];
  let inCode = false;
  let lang = "";
  let open = "";
  for (const line of text.split("\n")) {
    const m = line.match(/^```([A-Za-z0-9_+\-.]*)\s*$/);
    if (m) {
      if (inCode) {
        out.push({
          code: true,
          lang,
          body: buf.join("\n"),
          closed: true,
          open,
          close: line,
          bodyStarted: buf.length > 0,
        });
        buf = [];
        inCode = false;
        lang = "";
        open = "";
      } else {
        if (buf.length)
          out.push({
            code: false,
            lang: "",
            body: buf.join("\n"),
            closed: true,
            open: "",
            close: "",
            bodyStarted: false,
          });
        buf = [];
        inCode = true;
        lang = m[1] ?? "";
        open = line;
      }
      continue;
    }
    buf.push(line);
  }
  // Emit a trailing segment for leftover prose OR for an open fence with no body
  // yet (buf empty) — the fence-open marker line must still render.
  if (buf.length || inCode) {
    out.push({
      code: inCode,
      lang,
      body: buf.join("\n"),
      closed: false,
      open,
      close: "",
      bodyStarted: inCode && buf.length > 0,
    });
  }
  return out;
}

export function MarkdownSource({ text, className }: { text: string; className?: string }) {
  const segs = useMemo(() => segments(text), [text]);
  if (!text.trim()) return null;
  return (
    <div className={cn("min-w-0 font-mono text-[12.5px] leading-relaxed text-moon-100", className)}>
      {segs.map((s, i) => {
        // A bare open fence with no body or close (a lone "```") now yields a
        // segment (for the editor overlay); the read-only view showed nothing
        // for it before, so keep skipping it here.
        if (s.code && !s.closed && !s.bodyStarted && s.body === "") return null;
        return s.code ? (
          <pre
            key={i}
            className="my-1.5 overflow-x-auto rounded-control border border-ink-700 bg-ink-950 px-2.5 py-2 text-[11px] leading-relaxed text-moon-100"
          >
            <span className="text-moon-600">{`\`\`\`${s.lang}\n`}</span>
            {s.body}
            {s.closed && <span className="text-moon-600">{"\n```"}</span>}
          </pre>
        ) : (
          <pre key={i} className="whitespace-pre-wrap break-words">
            {highlightProse(s.body)}
          </pre>
        );
      })}
    </div>
  );
}
