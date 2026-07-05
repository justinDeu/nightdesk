import { forwardRef } from "react";
import type { HTMLAttributes } from "react";
import { cn } from "@/lib/cn";

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** Raise the surface a step (ink-800) for nested cards / active rows. */
  raised?: boolean;
  interactive?: boolean;
  /** Draw the animated dawn edge along the top — marks a running ticket. */
  running?: boolean;
}

export const Card = forwardRef<HTMLDivElement, CardProps>(function Card(
  { className, raised, interactive, running, children, ...rest },
  ref,
) {
  return (
    <div
      ref={ref}
      className={cn(
        // Elevation (raised shadow + inset top-highlight) carries the depth, so
        // the border recedes to a soft edge rather than a hard flat line.
        "relative rounded-card border border-ink-700/50 shadow-[var(--shadow-raised)] overflow-hidden",
        raised ? "bg-ink-800" : "bg-ink-900",
        interactive && "transition-colors duration-100 hover:border-ink-700 hover:bg-ink-800 cursor-pointer",
        className,
      )}
      {...rest}
    >
      {running && (
        <span
          aria-hidden
          className="dawn-edge absolute inset-x-0 top-0 h-[2px]"
        />
      )}
      {children}
    </div>
  );
});

export function CardHeader({ className, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("px-4 pt-3.5 pb-2", className)} {...rest} />;
}

export function CardBody({ className, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("px-4 py-3", className)} {...rest} />;
}

export function CardFooter({ className, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("px-4 py-2.5 border-t border-ink-700/70 bg-ink-950/30", className)}
      {...rest}
    />
  );
}
