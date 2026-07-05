import { cn } from "@/lib/cn";

export interface SpinnerProps {
  size?: number;
  className?: string;
  label?: string;
}

/** Reduced-motion friendly: the ring still conveys "busy" when static. */
export function Spinner({ size = 16, className, label = "Loading" }: SpinnerProps) {
  return (
    <span
      role="status"
      aria-label={label}
      className={cn(
        "inline-block rounded-full border-2 border-ink-700 border-t-lamp motion-safe:animate-[spin_0.7s_linear_infinite]",
        className,
      )}
      style={{ width: size, height: size }}
    />
  );
}
