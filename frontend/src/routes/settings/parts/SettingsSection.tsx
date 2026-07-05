import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

/** A settings content page: a heading block + body. Each section renders one of
 *  these at its root so the right pane reads consistently across sections. */
export function SectionHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="mb-5 flex items-start justify-between gap-4">
      <div className="min-w-0">
        <h2 className="font-display text-lg font-semibold tracking-tight text-moon-100">
          {title}
        </h2>
        {description && <p className="mt-1 text-sm text-moon-400">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}

/** A bordered grouping inside a section — titled block of related fields. */
export function SettingsCard({
  title,
  description,
  children,
  className,
  footer,
}: {
  title?: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  className?: string;
  footer?: ReactNode;
}) {
  return (
    <div className={cn("rounded-card border border-ink-700 bg-ink-900", className)}>
      {(title || description) && (
        <div className="border-b border-ink-700/70 px-4 py-3">
          {title && (
            <h3 className="font-display text-sm font-semibold text-moon-100">{title}</h3>
          )}
          {description && <p className="mt-0.5 text-xs text-moon-400">{description}</p>}
        </div>
      )}
      <div className="px-4 py-4">{children}</div>
      {footer && (
        <div className="border-t border-ink-700/70 bg-ink-950/30 px-4 py-2.5">{footer}</div>
      )}
    </div>
  );
}

/** Two-column settings body: a readable-width primary column on the left and a
 *  contextual aside that fills the remaining width on the right, so narrow forms
 *  cap their line length without leaving the page a centered island. Stacks to a
 *  single column below `lg`. */
export function SettingsSplit({
  children,
  aside,
  className,
}: {
  children: ReactNode;
  aside: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "grid gap-6 lg:grid-cols-[minmax(0,38rem)_minmax(16rem,1fr)] xl:gap-8",
        className,
      )}
    >
      <div className="min-w-0">{children}</div>
      <aside className="min-w-0">{aside}</aside>
    </div>
  );
}

/** A quiet explanatory panel for the right rail of a SettingsSplit — reads as
 *  guidance, not another form card. */
export function AsidePanel({
  title,
  children,
  className,
}: {
  title?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "rounded-card border border-ink-700/60 bg-ink-950/40 px-4 py-3.5",
        className,
      )}
    >
      {title && (
        <h3 className="mb-2 font-display text-xs font-semibold uppercase tracking-wide text-moon-400">
          {title}
        </h3>
      )}
      <div className="space-y-2.5 text-[13px] leading-relaxed text-moon-400">{children}</div>
    </div>
  );
}

/** A single labelled read-only value row (definition list style). */
export function InfoRow({
  label,
  children,
  mono,
}: {
  label: string;
  children: ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1.5">
      <span className="text-xs text-moon-400">{label}</span>
      <span className={cn("text-right text-sm text-moon-100", mono && "font-mono text-[13px]")}>
        {children}
      </span>
    </div>
  );
}
