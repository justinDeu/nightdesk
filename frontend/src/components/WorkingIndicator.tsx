/**
 * Tail-of-transcript status row shown while a turn is in flight (see
 * isTurnInFlight in lib/transcript.ts). Deliberately NOT a message bubble —
 * no border, no surface, no role label — so it reads as system status (the
 * MetaLine/breadcrumb family), dimmer than any event card. Distinct from
 * TranscriptView's Thinking card, which renders persisted extended-thinking
 * text.
 *
 * Under prefers-reduced-motion the bouncing dots are swapped for a static
 * ellipsis, so it degrades to plain "thinking…" text.
 */
export function WorkingIndicator() {
  return (
    <div
      role="status"
      aria-label="Agent is working"
      className="mt-1.5 flex items-center gap-1.5 px-2.5 py-1 font-mono text-[10px] font-semibold uppercase tracking-wide text-moon-600"
    >
      <span>thinking</span>
      <span className="hidden items-end gap-0.5 pb-0.5 motion-safe:inline-flex" aria-hidden="true">
        {[0, 150, 300].map((delay) => (
          <span
            key={delay}
            className="h-1 w-1 rounded-full bg-moon-600 motion-safe:animate-bounce"
            style={{ animationDelay: `${delay}ms` }}
          />
        ))}
      </span>
      <span className="motion-safe:hidden" aria-hidden="true">
        …
      </span>
    </div>
  );
}
