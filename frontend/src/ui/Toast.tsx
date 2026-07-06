import { useEffect, useRef, useState, type ReactNode } from "react";
import { Toaster as SonnerToaster, toast as sonnerToast } from "sonner";
import { AlertTriangle, CheckCircle2, Info, X } from "lucide-react";
import { ApiError } from "@/api/client";

/**
 * Toast host + a thin wrapper over sonner.
 *
 * Every toast is rendered as a custom card (sonner handles stacking, hover-
 * expand, and enter/exit motion; we own the visuals and the countdown). This
 * buys three things over the stock sonner toast:
 *   - a danger treatment for errors (ember accent + red-cast ink surface) that
 *     is unmistakable next to the quiet neutral success/info cards;
 *   - a thin progress bar that drains over the toast's lifetime, pauses on
 *     hover, and dismisses the toast when it empties;
 *   - a dedicated close (×) button (the card body is not itself a dismiss
 *     target, so links/text inside a toast stay usable).
 *
 * Errors also get better copy for free: pass the caught error as `error` and
 * the card derives a plain-language line from the status + server `detail`
 * (see `describeError`), so the title stays "what the user was doing".
 */

type Variant = "error" | "success" | "info";

/** Auto-dismiss windows. Errors linger a beat longer so the detail is readable. */
const DEFAULT_DURATION: Record<Variant, number> = {
  error: 7000,
  success: 4000,
  info: 4500,
};

export interface ToastOptions {
  description?: ReactNode;
  /** Override the auto-dismiss window (ms). */
  duration?: number;
  /**
   * The caught error. When present on `toast.error`, the card's description is
   * derived from it (status → plain-language lead + server detail, or a
   * server-unreachable line for network failures) and any `description` is
   * ignored.
   */
  error?: unknown;
  id?: string | number;
  /** Optional inline action button (e.g. "Open" on a "Ticket minted" toast). */
  action?: { label: string; onClick: () => void };
}

function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

/** Pull a human detail out of an ApiError, dropping our generic fallbacks. */
function cleanDetail(err: ApiError): string | null {
  const m = err.message?.trim();
  if (!m) return null;
  if (/^Request failed \(\d+\)$/.test(m)) return null;
  if (m === "Network request failed") return null;
  return m;
}

/**
 * Turn any thrown value into one plain-language line. Callers own the title
 * ("Couldn't set project"); this is the explanation underneath it.
 */
export function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 0) {
      return "Can't reach the server. It may be offline or restarting — try again in a moment.";
    }
    if (err.status === 401) {
      return "Your session has expired. Sign in again to continue.";
    }
    const detail = cleanDetail(err);
    if (err.status >= 500) {
      return detail
        ? `The server ran into an error. ${detail}`
        : "The server ran into an error. Try again in a moment.";
    }
    if (err.status >= 400) {
      return detail ?? "The request was rejected.";
    }
    return detail ?? "Something went wrong.";
  }
  if (err instanceof Error && err.message) return err.message;
  return "Something went wrong.";
}

function usePrefersReducedMotion(): boolean {
  const [reduce, setReduce] = useState(
    () =>
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onChange = () => setReduce(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return reduce;
}

const ICONS: Record<Variant, typeof AlertTriangle> = {
  error: AlertTriangle,
  success: CheckCircle2,
  info: Info,
};

const ACCENT: Record<Variant, string> = {
  error: "text-[var(--color-danger-accent)]",
  success: "text-success",
  info: "text-moon-400",
};

const BAR: Record<Variant, string> = {
  error: "bg-[var(--color-danger-accent)]",
  success: "bg-success",
  info: "bg-moon-600",
};

interface CardProps {
  id: string | number;
  variant: Variant;
  title: ReactNode;
  description?: ReactNode;
  duration: number;
  action?: { label: string; onClick: () => void };
}

function ToastCard({ id, variant, title, description, duration, action }: CardProps) {
  const barRef = useRef<HTMLDivElement>(null);
  const pausedRef = useRef(false);
  const reduce = usePrefersReducedMotion();

  useEffect(() => {
    let raf = 0;
    let last = performance.now();
    let left = duration;
    const step = (now: number) => {
      const dt = now - last;
      last = now;
      if (!pausedRef.current) left -= dt;
      if (barRef.current && !reduce) {
        barRef.current.style.transform = `scaleX(${Math.max(0, left / duration)})`;
      }
      if (left <= 0) {
        sonnerToast.dismiss(id);
        return;
      }
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [id, duration, reduce]);

  const Icon = ICONS[variant];
  const isError = variant === "error";

  return (
    <div
      role={isError ? "alert" : "status"}
      onMouseEnter={() => {
        pausedRef.current = true;
      }}
      onMouseLeave={() => {
        pausedRef.current = false;
      }}
      className={cx(
        "group relative flex w-[356px] max-w-[calc(100vw-2rem)] items-start gap-3",
        "overflow-hidden rounded-card border px-3.5 py-3 font-sans text-sm shadow-[var(--shadow-overlay)]",
        isError
          ? "border-[var(--color-danger-border)] border-l-2 border-l-[var(--color-danger-accent)] bg-[var(--color-danger-surface)]"
          : "border-ink-700 bg-ink-800",
      )}
    >
      <Icon className={cx("mt-0.5 h-4 w-4 shrink-0", ACCENT[variant])} aria-hidden />
      <div className="min-w-0 flex-1">
        <div className="font-medium text-moon-100">{title}</div>
        {description ? (
          <div className="mt-0.5 break-words text-moon-400">{description}</div>
        ) : null}
        {action ? (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              action.onClick();
              sonnerToast.dismiss(id);
            }}
            className="mt-2 rounded-control border border-ink-700 bg-ink-800 px-2 py-1 text-xs font-semibold text-moon-100 transition-colors hover:bg-ink-700"
          >
            {action.label}
          </button>
        ) : null}
      </div>
      <button
        type="button"
        aria-label="Dismiss"
        onClick={(e) => {
          e.stopPropagation();
          sonnerToast.dismiss(id);
        }}
        className="-mr-1 -mt-0.5 shrink-0 rounded-control p-1 text-moon-600 transition-colors hover:bg-ink-700 hover:text-moon-100"
      >
        <X className="h-3.5 w-3.5" />
      </button>
      <div
        aria-hidden
        className="pointer-events-none absolute inset-x-0 bottom-0 h-0.5 bg-ink-700/40"
      >
        <div
          ref={barRef}
          className={cx("h-full origin-left", BAR[variant])}
          style={{ transform: "scaleX(1)" }}
        />
      </div>
    </div>
  );
}

function show(variant: Variant, title: ReactNode, opts: ToastOptions): string | number {
  const description =
    variant === "error" && opts.error !== undefined
      ? describeError(opts.error)
      : opts.description;
  return sonnerToast.custom(
    (id) => (
      <ToastCard
        id={id}
        variant={variant}
        title={title}
        description={description}
        duration={opts.duration ?? DEFAULT_DURATION[variant]}
        action={opts.action}
      />
    ),
    { duration: Infinity, id: opts.id },
  );
}

export const toast = {
  error: (title: ReactNode, opts: ToastOptions = {}) => show("error", title, opts),
  success: (title: ReactNode, opts: ToastOptions = {}) => show("success", title, opts),
  info: (title: ReactNode, opts: ToastOptions = {}) => show("info", title, opts),
  dismiss: (id?: string | number) => sonnerToast.dismiss(id),
};

/** Toast host. Mount once near the app root. Sonner is unstyled — the card
 *  above owns every pixel. */
export function Toaster() {
  return (
    <SonnerToaster
      position="bottom-right"
      gap={8}
      // Respect the phone's safe areas (home indicator / rounded corners) so a
      // toast never tucks under system chrome; falls back to 16px elsewhere.
      offset={{
        bottom: "max(16px, env(safe-area-inset-bottom))",
        right: "max(16px, env(safe-area-inset-right))",
      }}
      mobileOffset={{
        bottom: "max(16px, env(safe-area-inset-bottom))",
        right: "max(16px, env(safe-area-inset-right))",
        left: "max(16px, env(safe-area-inset-left))",
      }}
      toastOptions={{ unstyled: true }}
    />
  );
}
