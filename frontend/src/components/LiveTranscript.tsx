import type { ReactNode } from "react";
import { useLiveTranscript } from "@/lib/transcript";
import { TranscriptScroller } from "./TranscriptScroller";

/**
 * Self-subscribing transcript for the theater and run-timeline: streams a run's
 * (or ticket's latest run's) transcript over SSE and renders it. The detail page
 * subscribes itself instead, to share events with the Tasks / Sub-agents panels.
 */
export function LiveTranscript({
  ticketId,
  runId,
  enabled,
  running = false,
  caption,
  className,
}: {
  /** Stream the ticket's current/latest run transcript. */
  ticketId?: string;
  /** Stream a specific run's transcript (takes precedence over ticketId). */
  runId?: string;
  enabled: boolean;
  running?: boolean;
  caption?: ReactNode;
  className?: string;
}) {
  const path = runId
    ? `/api/v1/runs/${runId}/transcript`
    : ticketId
      ? `/api/v1/tickets/${ticketId}/transcript`
      : undefined;
  const { events, status } = useLiveTranscript(path, enabled);
  return (
    <TranscriptScroller
      events={events}
      status={status}
      running={running}
      caption={caption}
      className={className}
    />
  );
}
