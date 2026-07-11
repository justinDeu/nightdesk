import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/cn";

/**
 * Markdown renderer for model prose (assistant_text, result summaries).
 * Tuned for a dense transcript log, not an article: tight margins, compact
 * lists, small stepped headings, bordered ink-950 code cards. No raw HTML is
 * rendered (react-markdown default — no rehype-raw), so model output cannot
 * inject markup.
 */
const components: Components = {
  p: (p) => <p className="my-1" {...p} />,
  h1: (p) => <h1 className="mb-1 mt-2.5 text-[15px] font-semibold text-moon-100" {...p} />,
  h2: (p) => <h2 className="mb-1 mt-2.5 text-[14px] font-semibold text-moon-100" {...p} />,
  h3: (p) => <h3 className="mb-0.5 mt-2 text-[13.5px] font-semibold text-moon-100" {...p} />,
  h4: (p) => <h4 className="mb-0.5 mt-2 text-[13px] font-semibold text-moon-100" {...p} />,
  h5: (p) => <h5 className="mb-0.5 mt-2 text-[12.5px] font-semibold text-moon-100" {...p} />,
  h6: (p) => (
    <h6
      className="mb-0.5 mt-2 text-[11px] font-semibold uppercase tracking-wide text-moon-400"
      {...p}
    />
  ),
  ul: (p) => <ul className="my-1 list-disc space-y-0.5 pl-4 marker:text-moon-600" {...p} />,
  ol: (p) => <ol className="my-1 list-decimal space-y-0.5 pl-4 marker:text-moon-600" {...p} />,
  li: (p) => <li className="[&>p]:my-0" {...p} />,
  strong: (p) => <strong className="font-semibold text-moon-100" {...p} />,
  a: ({ children, ...p }) => (
    <a
      className="text-lamp underline decoration-lamp/40 underline-offset-2 hover:decoration-lamp"
      target="_blank"
      rel="noreferrer"
      {...p}
    >
      {children}
    </a>
  ),
  blockquote: (p) => (
    <blockquote className="my-1 border-l-2 border-ink-700 pl-3 text-moon-400" {...p} />
  ),
  hr: (p) => <hr className="my-2 border-ink-700" {...p} />,
  // Inline code. Fenced blocks live inside `pre`, whose [&_code] overrides
  // strip this chip styling back to plain block text.
  code: (p) => (
    <code
      className="rounded border border-ink-700/60 bg-ink-800 px-1 py-px font-mono text-[12px] text-moon-100"
      {...p}
    />
  ),
  pre: (p) => (
    <pre
      className={cn(
        "my-1.5 overflow-x-auto rounded-control border border-ink-700 bg-ink-950 px-2.5 py-2",
        "font-mono text-[11px] leading-relaxed text-moon-100",
        "[&_code]:block [&_code]:border-0 [&_code]:bg-transparent [&_code]:p-0 [&_code]:text-[11px]",
      )}
      {...p}
    />
  ),
  table: (p) => (
    <div className="my-1.5 overflow-x-auto">
      <table className="w-full border-collapse text-[12px]" {...p} />
    </div>
  ),
  th: (p) => (
    <th
      className="border border-ink-700/60 bg-ink-800/60 px-2 py-1 text-left font-semibold text-moon-100"
      {...p}
    />
  ),
  td: (p) => <td className="border border-ink-700/60 px-2 py-1 align-top" {...p} />,
};

export function Markdown({ text, className }: { text: string; className?: string }) {
  return (
    <div
      className={cn(
        "min-w-0 font-sans text-[13.5px] leading-relaxed text-moon-100",
        "[&>*:first-child]:mt-0 [&>*:last-child]:mb-0",
        className,
      )}
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
