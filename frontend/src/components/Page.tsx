import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

export interface PageProps {
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  /** Content width: "default" reading width, "wide" for dashboards, "xwide" for
   *  dashboards that should breathe on ultrawide monitors, "full" edge-to-edge
   *  (board/theater). `bleed` is a legacy alias for "full". */
  width?: "default" | "wide" | "xwide" | "full";
  bleed?: boolean;
}

const WIDTHS: Record<string, string> = {
  default: "max-w-6xl",
  wide: "max-w-[1600px]",
  xwide: "max-w-[2200px]",
  full: "max-w-none",
};

export function Page({ title, subtitle, actions, children, className, width, bleed }: PageProps) {
  const maxWidth = WIDTHS[bleed ? "full" : (width ?? "default")];
  return (
    <div className={cn("mx-auto w-full px-6 py-6", maxWidth, className)}>
      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="font-display text-xl font-semibold tracking-tight text-moon-100">
            {title}
          </h1>
          {subtitle && <p className="mt-1 text-sm text-moon-400">{subtitle}</p>}
        </div>
        {actions && <div className="flex items-center gap-2">{actions}</div>}
      </div>
      {children}
    </div>
  );
}
