import type { ReactNode } from "react";
import * as RPopover from "@radix-ui/react-popover";
import { Check, SlidersHorizontal } from "lucide-react";
import {
  GROUP_BY_OPTIONS,
  ORDER_BY_OPTIONS,
  type GroupBy,
  type OrderBy,
} from "./displayModel";
import { cn } from "@/lib/cn";

export function DisplayOptions({
  open,
  onOpenChange,
  groupBy,
  orderBy,
  onGroupBy,
  onOrderBy,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  groupBy: GroupBy;
  orderBy: OrderBy;
  onGroupBy: (g: GroupBy) => void;
  onOrderBy: (o: OrderBy) => void;
}) {
  return (
    <RPopover.Root open={open} onOpenChange={onOpenChange}>
      <RPopover.Trigger asChild>
        <button
          aria-label="Display options"
          className={cn(
            "inline-flex h-8 shrink-0 items-center gap-1.5 rounded-control border border-ink-700 bg-ink-900 px-2.5 text-xs font-medium text-moon-400",
            "transition-colors hover:bg-ink-800 hover:text-moon-100",
            open && "bg-ink-800 text-moon-100",
          )}
        >
          <SlidersHorizontal size={14} />
          <span className="hidden sm:inline">Display</span>
        </button>
      </RPopover.Trigger>
      <RPopover.Portal>
        <RPopover.Content
          align="end"
          sideOffset={6}
          collisionPadding={8}
          className="pop-in z-40 w-60 rounded-card border border-ink-700 bg-ink-800 p-1.5 shadow-[var(--shadow-pop)] focus:outline-none"
        >
          <Section title="Group by">
            {GROUP_BY_OPTIONS.map((o) => (
              <OptionRow key={o.value} active={groupBy === o.value} onClick={() => onGroupBy(o.value)}>
                {o.label}
              </OptionRow>
            ))}
          </Section>
          <div className="my-1 h-px bg-ink-700" />
          <Section title="Ordering">
            {ORDER_BY_OPTIONS.map((o) => (
              <OptionRow key={o.value} active={orderBy === o.value} onClick={() => onOrderBy(o.value)}>
                {o.label}
              </OptionRow>
            ))}
          </Section>
        </RPopover.Content>
      </RPopover.Portal>
    </RPopover.Root>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="px-2 py-1 text-[11px] font-semibold uppercase tracking-wide text-moon-600">
        {title}
      </div>
      {children}
    </div>
  );
}

function OptionRow({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex w-full items-center gap-2 rounded-control px-2 py-1.5 text-left text-sm",
        active ? "text-moon-100" : "text-moon-400 hover:bg-ink-700 hover:text-moon-100",
      )}
    >
      <span className="flex w-4 justify-center">{active && <Check size={14} className="text-lamp" />}</span>
      <span className="flex-1">{children}</span>
    </button>
  );
}
