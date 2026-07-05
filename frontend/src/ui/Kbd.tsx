import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

export interface KbdProps {
  children: ReactNode;
  className?: string;
}

/** A single key cap. Compose multiples for chords: <Kbd>g</Kbd> <Kbd>d</Kbd>. */
export function Kbd({ children, className }: KbdProps) {
  return (
    <kbd
      className={cn(
        "inline-flex h-5 min-w-[20px] items-center justify-center rounded-[5px]",
        "border border-ink-700 border-b-2 bg-ink-800 px-1.5",
        "font-mono text-[11px] leading-none text-moon-400 select-none",
        className,
      )}
    >
      {children}
    </kbd>
  );
}
