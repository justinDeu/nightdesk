import type { HTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/cn";

export type BadgeTone =
  | "neutral"
  | "lamp"
  | "review"
  | "queued"
  | "success"
  | "failed"
  | "draft";

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: BadgeTone;
  mono?: boolean;
  /** Show a leading dot swatch in the tone color. */
  dot?: boolean;
  children: ReactNode;
}

const tones: Record<BadgeTone, string> = {
  neutral: "bg-ink-800 text-moon-400 border-ink-700",
  lamp: "bg-lamp/12 text-lamp border-lamp/25",
  review: "bg-review/12 text-review border-review/25",
  queued: "bg-queued/12 text-queued border-queued/25",
  success: "bg-success/12 text-success border-success/25",
  failed: "bg-failed/12 text-failed border-failed/25",
  draft: "bg-ink-800 text-moon-600 border-ink-700",
};

const dotColor: Record<BadgeTone, string> = {
  neutral: "bg-moon-400",
  lamp: "bg-lamp",
  review: "bg-review",
  queued: "bg-queued",
  success: "bg-success",
  failed: "bg-failed",
  draft: "bg-moon-600",
};

export function Badge({ tone = "neutral", mono, dot, className, children, ...rest }: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5",
        "text-[11px] font-medium leading-none whitespace-nowrap",
        tones[tone],
        mono && "font-mono",
        className,
      )}
      {...rest}
    >
      {dot && <span className={cn("h-1.5 w-1.5 rounded-full", dotColor[tone])} />}
      {children}
    </span>
  );
}
