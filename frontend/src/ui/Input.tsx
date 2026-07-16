import { forwardRef } from "react";
import type { InputHTMLAttributes, TextareaHTMLAttributes } from "react";
import { cn } from "@/lib/cn";

const fieldBase =
  "w-full rounded-control bg-ink-950 border border-ink-700 text-moon-100 " +
  "placeholder:text-moon-600 transition-colors duration-100 " +
  "hover:border-ink-700 focus:border-lamp focus:outline-none " +
  "disabled:opacity-50 disabled:cursor-not-allowed";

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  /** Render ids/paths/costs in mono. */
  mono?: boolean;
  invalid?: boolean;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { className, mono, invalid, ...rest },
  ref,
) {
  return (
    <input
      ref={ref}
      className={cn(
        fieldBase,
        "h-9 px-3 text-sm",
        mono && "font-mono text-[13px]",
        invalid && "border-failed focus:border-failed",
        className,
      )}
      {...rest}
    />
  );
});

export interface TextareaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  mono?: boolean;
  invalid?: boolean;
}

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { className, mono, invalid, ...rest },
  ref,
) {
  // cn is plain clsx (no tailwind-merge), so a conflicting utility in className
  // wouldn't reliably beat the base one. Drop the base default when the caller
  // supplies its own min-height or resize behaviour (e.g. a flex-1 fill).
  const cls = className ?? "";
  const overridesMinH = /(?:^|\s)min-h-/.test(cls);
  const overridesResize = /(?:^|\s)resize-(?:none|x|y)(?:\s|$)/.test(cls);
  return (
    <textarea
      ref={ref}
      className={cn(
        fieldBase,
        "px-3 py-2 text-sm leading-relaxed",
        !overridesMinH && "min-h-[80px]",
        !overridesResize && "resize-y",
        mono && "font-mono text-[13px]",
        invalid && "border-failed focus:border-failed",
        className,
      )}
      {...rest}
    />
  );
});

export interface FieldProps {
  label?: string;
  hint?: string;
  error?: string;
  htmlFor?: string;
  children: React.ReactNode;
  className?: string;
}

/** Label + hint + error wrapper for form controls. */
export function Field({ label, hint, error, htmlFor, children, className }: FieldProps) {
  return (
    <div className={cn("flex flex-col gap-1.5", className)}>
      {label && (
        <label htmlFor={htmlFor} className="text-xs font-medium text-moon-400">
          {label}
        </label>
      )}
      {children}
      {error ? (
        <p className="text-xs text-failed">{error}</p>
      ) : hint ? (
        <p className="text-xs text-moon-600">{hint}</p>
      ) : null}
    </div>
  );
}
