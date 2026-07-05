import { cn } from "@/lib/cn";
import { priorityMeta } from "@/lib/priority";
import { Tooltip } from "@/ui/Tooltip";

export interface PriorityChipProps {
  value: number;
  /** Hide entirely for "No priority" (value 0) — matches the board convention. */
  hideNone?: boolean;
  className?: string;
}

/** Compact priority chip. Shows the short P-code with the human label in a
 *  tooltip. Value 0 (No priority) is hidden when `hideNone`. */
export function PriorityChip({ value, hideNone, className }: PriorityChipProps) {
  const meta = priorityMeta(value);
  if (hideNone && value === 0) return null;
  return (
    <Tooltip content={meta.label}>
      <span
        className={cn(
          "inline-flex items-center gap-1 rounded-full border px-1.5 py-0.5",
          "text-[10px] font-semibold leading-none tabular-nums",
          meta.cls,
          className,
        )}
      >
        <span className={cn("h-1 w-1 rounded-full", meta.dot)} />
        {meta.short}
      </span>
    </Tooltip>
  );
}
