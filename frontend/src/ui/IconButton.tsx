import { forwardRef } from "react";
import type { ButtonHTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/cn";
import { Tooltip } from "./Tooltip";

export interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Required — icon buttons are unlabeled, so this is the accessible name and
   *  the tooltip text. */
  label: string;
  icon: ReactNode;
  size?: "sm" | "md";
  active?: boolean;
}

const sizes = {
  sm: "h-7 w-7",
  md: "h-9 w-9",
};

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, icon, size = "md", active, className, ...rest },
  ref,
) {
  return (
    <Tooltip content={label}>
      <button
        ref={ref}
        aria-label={label}
        className={cn(
          "inline-flex items-center justify-center rounded-control border border-transparent",
          "text-moon-400 transition-colors duration-100",
          "hover:bg-ink-800 hover:text-moon-100",
          "disabled:opacity-45 disabled:pointer-events-none",
          active && "bg-ink-800 text-lamp",
          sizes[size],
          className,
        )}
        {...rest}
      >
        {icon}
      </button>
    </Tooltip>
  );
});
