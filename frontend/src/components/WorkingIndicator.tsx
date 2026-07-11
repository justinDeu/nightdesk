/**
 * Tail-of-transcript "the agent is working" row: a nascent assistant bubble
 * shown while a turn is in flight (see isTurnInFlight in lib/transcript.ts).
 * Distinct from TranscriptView's Thinking card, which renders the model's
 * persisted extended-thinking text.
 *
 * Under prefers-reduced-motion the bounce is disabled (motion-safe) and the
 * dots read as a static "working…".
 */
export function WorkingIndicator() {
  return (
    <div
      role="status"
      aria-label="Agent is working"
      className="mt-2 rounded-card border border-ink-700 border-l-2 border-l-success/60 bg-ink-900 px-3.5 py-2.5"
    >
      <div className="mb-0.5 text-[10px] font-semibold uppercase tracking-wide text-success/80">
        Agent
      </div>
      <div className="flex items-center gap-1.5 font-sans text-[13px] text-moon-500">
        <span>working</span>
        <span className="inline-flex items-end gap-0.5 pb-0.5" aria-hidden="true">
          {[0, 150, 300].map((delay) => (
            <span
              key={delay}
              className="h-1 w-1 rounded-full bg-moon-500 motion-safe:animate-bounce"
              style={{ animationDelay: `${delay}ms` }}
            />
          ))}
        </span>
      </div>
    </div>
  );
}
