import type { ReactNode } from "react";
import * as RDialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { cn } from "@/lib/cn";

export interface DialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title?: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  /** width preset */
  size?: "sm" | "md" | "lg" | "xl";
  className?: string;
}

const sizes = {
  sm: "max-w-sm",
  md: "max-w-lg",
  lg: "max-w-2xl",
  // Wide body for long-form content (prompts, transcripts) that still leaves a
  // gutters on each side and caps on very large monitors.
  xl: "max-w-[min(80vw,900px)]",
};

export function Dialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  footer,
  size = "md",
  className,
}: DialogProps) {
  return (
    <RDialog.Root open={open} onOpenChange={onOpenChange}>
      <RDialog.Portal>
        <RDialog.Overlay className="overlay-in fixed inset-0 z-40 bg-black/70 backdrop-blur-[3px]" />
        <RDialog.Content
          className={cn(
            "pop-in fixed left-1/2 top-1/2 z-50 w-[calc(100vw-2rem)] -translate-x-1/2 -translate-y-1/2",
            "rounded-card border border-ink-700 bg-ink-900 shadow-[var(--shadow-overlay)]",
            // Viewport safety net: never taller than the (dynamic) viewport, and
            // scroll internally if a consumer's body overflows. Consumers with
            // their own scroll region (Composer's full editor) stay within this.
            "max-h-[85dvh] overflow-y-auto overscroll-contain",
            "focus:outline-none",
            sizes[size],
            className,
          )}
        >
          {(title || description) && (
            <div className="flex items-start justify-between gap-4 border-b border-ink-700 px-5 py-4">
              <div className="min-w-0">
                {title && (
                  <RDialog.Title className="font-display text-base font-semibold text-moon-100">
                    {title}
                  </RDialog.Title>
                )}
                {description && (
                  <RDialog.Description className="mt-1 text-sm text-moon-400">
                    {description}
                  </RDialog.Description>
                )}
              </div>
              <RDialog.Close
                aria-label="Close"
                className="rounded-control p-1 text-moon-400 hover:bg-ink-800 hover:text-moon-100"
              >
                <X size={16} />
              </RDialog.Close>
            </div>
          )}
          <div className="px-5 py-4">{children}</div>
          {footer && (
            <div className="flex items-center justify-end gap-2 border-t border-ink-700 px-5 py-3.5">
              {footer}
            </div>
          )}
        </RDialog.Content>
      </RDialog.Portal>
    </RDialog.Root>
  );
}

export const DialogClose = RDialog.Close;
