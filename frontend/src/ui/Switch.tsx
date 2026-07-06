import { forwardRef, type ComponentPropsWithoutRef } from "react";
import { cn } from "@/lib/cn";

export interface SwitchProps
  extends Omit<ComponentPropsWithoutRef<"button">, "onChange" | "type" | "role" | "aria-checked"> {
  checked: boolean;
  onChange: (next: boolean) => void;
}

/**
 * Accessible pill toggle. `role="switch"` + `aria-checked` announce the state;
 * a native `<button>` gives Enter/Space activation and focus for free. Wrap in a
 * <Tooltip> or pair with a label via `aria-label` / `aria-labelledby`.
 */
export const Switch = forwardRef<HTMLButtonElement, SwitchProps>(function Switch(
  { checked, onChange, disabled, className, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        "inline-flex h-5 w-9 shrink-0 items-center rounded-full border px-0.5 transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lamp/50 focus-visible:ring-offset-1 focus-visible:ring-offset-ink-950",
        checked ? "border-lamp/40 bg-lamp/25" : "border-ink-700 bg-ink-800",
        disabled && "cursor-not-allowed opacity-50",
        className,
      )}
      {...rest}
    >
      <span
        aria-hidden
        className={cn(
          "h-3.5 w-3.5 rounded-full transition-transform",
          checked ? "translate-x-4 bg-lamp" : "translate-x-0 bg-moon-600",
        )}
      />
    </button>
  );
});
