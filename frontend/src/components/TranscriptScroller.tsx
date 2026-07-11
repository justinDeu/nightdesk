import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ArrowDown } from "lucide-react";
import type { TranscriptEvent, TranscriptStatus } from "@/lib/transcript";
import { isTurnInFlight } from "@/lib/transcript";
import { TranscriptView } from "./TranscriptView";
import { WorkingIndicator } from "./WorkingIndicator";
import { cn } from "@/lib/cn";

/** Presentational transcript: an auto-scrolling, pin-to-bottom log of already-
 *  fetched events. The subscription lives in the caller (so the detail page can
 *  share one event stream between the transcript and the Tasks / Sub-agents
 *  rail panels). */
export function TranscriptScroller({
  events,
  status,
  running = false,
  caption,
  footer,
  className,
}: {
  events: TranscriptEvent[];
  status: TranscriptStatus;
  running?: boolean;
  caption?: ReactNode;
  /** Rendered inside the scroll container, after the event list — e.g. pending
   *  (queued, undelivered) user bubbles on the agent screen. Participates in
   *  the pin-to-bottom follow so a bubble appearing or resolving keeps the
   *  view at the bottom. */
  footer?: ReactNode;
  className?: string;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [pinned, setPinned] = useState(true);

  // "The agent is working": a turn is in flight (user message delivered / run
  // started, no settling result yet) while the run/session is live. Drives the
  // tail indicator inside the scroll container.
  const inFlight = useMemo(() => isTurnInFlight(events), [events]);
  const working = running && inFlight;

  // Pinned-to-bottom chat behavior: follow every new event while the reader is
  // at (or near) the bottom, whether or not the run is flagged as live — the
  // liveness poll can lag the SSE stream (agents), and a freshly opened
  // transcript should start at its latest output. Scrolling up unpins; the
  // reader is never yanked back down until they return to the bottom.
  // `footer` is in the deps so pending bubbles appearing/resolving re-pin too;
  // re-running on an unchanged footer just rewrites scrollTop to its current
  // value, which is a no-op.
  // `working` is in the deps so the indicator appearing/disappearing re-pins
  // (it lives inside the scroll container and changes its height).
  useEffect(() => {
    if (!pinned) return;
    const el = scrollRef.current;
    if (!el) return;
    // Skip the write when already at the bottom: a redundant scrollTop write
    // still fires a scroll event, and an effect re-run per poll/render (e.g. a
    // fresh footer identity) would turn that into a scroll-event drumbeat that
    // can feed back through onScroll/scroll anchoring into visible jitter.
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 1) return;
    el.scrollTop = el.scrollHeight;
  }, [events, footer, pinned, working]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    setPinned(el.scrollHeight - el.scrollTop - el.clientHeight < 40);
  };
  const jumpToBottom = () => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    setPinned(true);
  };

  const live = running && (status === "open" || status === "connecting");
  const statusLabel = live
    ? status === "connecting"
      ? "connecting…"
      : "live"
    : status === "connecting"
      ? "loading…"
      : "transcript";

  return (
    <div className={cn("relative flex min-h-0 flex-col", className)}>
      <div className="mb-1.5 flex items-center gap-2 text-[11px] text-moon-600">
        <span
          className={cn(
            "inline-block h-1.5 w-1.5 rounded-full",
            live ? "bg-success motion-safe:animate-pulse" : "bg-moon-600",
          )}
        />
        <span className={live ? "text-success" : ""}>{statusLabel}</span>
        <span className="font-mono">· {events.length} events</span>
        {caption && <span className="ml-auto truncate font-mono">{caption}</span>}
      </div>

      <div
        ref={scrollRef}
        onScroll={onScroll}
        role="log"
        aria-label="Run transcript"
        aria-live={running ? "polite" : "off"}
        /* overflow-anchor:none — this component owns the scroll position (pin
           effect + onScroll); Chrome's scroll anchoring adjusting scrollTop on
           content-height changes fights those writes and can oscillate.
           scrollbar-gutter:stable — with the app's classic (space-taking)
           scrollbars, a scrollbar appearing/disappearing rewraps the content;
           at a critical width that becomes a wrap↔scrollbar feedback loop.
           Reserving the gutter makes content width scrollbar-invariant. */
        className="min-h-0 flex-1 overflow-auto overscroll-contain rounded-card border border-ink-700 bg-ink-950/40 p-2.5 [overflow-anchor:none] [scrollbar-gutter:stable]"
      >
        <TranscriptView events={events} suppressEmpty={working} />
        {working && <WorkingIndicator />}
        {footer}
      </div>

      {/* Whenever the reader is scrolled up, offer the way back — an idle
          agent's (or finished run's) transcript needs it as much as a live
          one. Unpinning only happens via onScroll at >=40px above the bottom,
          so !pinned already implies the container overflows. */}
      {!pinned && (
        <button
          onClick={jumpToBottom}
          className="absolute bottom-3 right-3 inline-flex items-center gap-1 rounded-full border border-ink-700 bg-ink-800 px-2.5 py-1 text-[11px] text-moon-100 shadow-[var(--shadow-pop)] hover:bg-ink-700"
        >
          <ArrowDown size={12} /> Jump to latest
        </button>
      )}
    </div>
  );
}
