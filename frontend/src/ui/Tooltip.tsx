import type { ReactNode } from "react";
import * as RTooltip from "@radix-ui/react-tooltip";
import { cn } from "@/lib/cn";

export interface TooltipProps {
  content: ReactNode;
  children: ReactNode;
  side?: "top" | "right" | "bottom" | "left";
  align?: "start" | "center" | "end";
  delayDuration?: number;
  /** Mono styling for ids/paths/costs. */
  mono?: boolean;
}

/** Styled tooltip. The quality floor forbids native `title=` — everything that
 *  needs a hint uses this. Wrap the whole app once in <TooltipProvider>. */
export function Tooltip({
  content,
  children,
  side = "top",
  align = "center",
  delayDuration = 250,
  mono,
}: TooltipProps) {
  if (content === null || content === undefined || content === "") {
    return <>{children}</>;
  }
  return (
    <RTooltip.Root delayDuration={delayDuration}>
      <RTooltip.Trigger asChild>{children}</RTooltip.Trigger>
      <RTooltip.Portal>
        <RTooltip.Content
          side={side}
          align={align}
          sideOffset={6}
          collisionPadding={8}
          className={cn(
            "z-50 max-w-xs rounded-control border border-ink-700 bg-ink-800 px-2 py-1",
            "text-xs text-moon-100 shadow-[var(--shadow-pop)]",
            "select-none pop-in",
            mono && "font-mono",
          )}
        >
          {content}
          <RTooltip.Arrow className="fill-ink-800" width={10} height={5} />
        </RTooltip.Content>
      </RTooltip.Portal>
    </RTooltip.Root>
  );
}

export const TooltipProvider = RTooltip.Provider;
