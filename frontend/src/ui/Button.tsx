import { forwardRef } from "react";
import type { ButtonHTMLAttributes, ReactNode } from "react";
import { Slot } from "@radix-ui/react-slot";
import { cn } from "@/lib/cn";
import { Spinner } from "./Spinner";

export type ButtonVariant = "primary" | "ghost" | "danger" | "subtle";
export type ButtonSize = "sm" | "md";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  leadingIcon?: ReactNode;
  trailingIcon?: ReactNode;
  /** Render as the single child element (e.g. a router <Link>) with button
   *  styling — used for navigational buttons. */
  asChild?: boolean;
}

const base =
  "inline-flex items-center justify-center gap-2 rounded-control font-medium " +
  "whitespace-nowrap select-none transition-colors duration-100 " +
  "disabled:pointer-events-none disabled:opacity-60";

const sizes: Record<ButtonSize, string> = {
  sm: "h-7 px-2.5 text-xs",
  md: "h-9 px-3.5 text-sm",
};

const variants: Record<ButtonVariant, string> = {
  primary:
    "bg-lamp text-ink-950 font-semibold hover:bg-lamp-soft active:brightness-95 shadow-[0_1px_0_rgba(255,255,255,0.12)_inset] " +
    // Distinct disabled: a flat inert chip, not dimmed amber (which read as a
    // muddy half-strength primary). Enabled stays full-strength lamp.
    "disabled:bg-ink-800 disabled:text-moon-600 disabled:shadow-none disabled:opacity-100",
  ghost:
    "bg-transparent text-moon-100 border border-ink-700 hover:bg-ink-800 hover:border-ink-700 active:bg-ink-900",
  subtle: "bg-ink-800 text-moon-100 hover:bg-ink-700 active:brightness-95",
  danger:
    "bg-transparent text-failed border border-failed/40 hover:bg-failed/10 hover:border-failed/70 active:bg-failed/15",
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    variant = "ghost",
    size = "md",
    loading,
    leadingIcon,
    trailingIcon,
    className,
    children,
    disabled,
    asChild,
    ...rest
  },
  ref,
) {
  const Comp = asChild ? Slot : "button";
  return (
    <Comp
      ref={ref}
      className={cn(base, sizes[size], variants[variant], className)}
      disabled={asChild ? undefined : disabled || loading}
      {...rest}
    >
      {asChild ? (
        children
      ) : (
        <>
          {loading ? <Spinner size={size === "sm" ? 12 : 14} /> : leadingIcon}
          {children}
          {!loading && trailingIcon}
        </>
      )}
    </Comp>
  );
});
