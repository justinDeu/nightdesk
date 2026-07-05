import { forwardRef } from "react";
import type { SelectHTMLAttributes } from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/cn";

export interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  invalid?: boolean;
}

/** Native select styled to match the token system. Radix Select is overkill for
 *  simple option lists; the command palette / property pickers use DropdownMenu
 *  where richer rows are needed. */
export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { className, invalid, children, ...rest },
  ref,
) {
  return (
    <div className="relative">
      <select
        ref={ref}
        className={cn(
          "h-9 w-full appearance-none rounded-control bg-ink-950 border border-ink-700",
          "pl-3 pr-8 text-sm text-moon-100 transition-colors duration-100",
          "focus:border-lamp focus:outline-none disabled:opacity-50",
          invalid && "border-failed focus:border-failed",
          className,
        )}
        {...rest}
      >
        {children}
      </select>
      <ChevronDown
        size={15}
        className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-moon-400"
      />
    </div>
  );
});
