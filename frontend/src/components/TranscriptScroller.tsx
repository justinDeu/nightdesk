import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ArrowDown } from "lucide-react";
import type { TranscriptEvent, TranscriptStatus } from "@/lib/transcript";
import { TranscriptView } from "./TranscriptView";
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
  className,
}: {
  events: TranscriptEvent[];
  status: TranscriptStatus;
  running?: boolean;
  caption?: ReactNode;
  className?: string;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [pinned, setPinned] = useState(true);

  useEffect(() => {
    if (!pinned || !running) return;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [events, pinned, running]);

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
        className="min-h-0 flex-1 overflow-auto overscroll-contain rounded-card border border-ink-700 bg-ink-950/40 p-2.5"
      >
        <TranscriptView events={events} />
      </div>

      {running && !pinned && (
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
