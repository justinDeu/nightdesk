import type { EndpointUsage, UsageSeverity, UsageWindow } from "@/api/usage";
import { cn } from "@/lib/cn";
import { relativeTime } from "@/lib/time";

/** Shared subscription-usage renderer used by the worker popover ("compact")
 *  and the analytics page ("card"). One block per endpoint: a header (provider
 *  name + plan) then one meter row per rate-limit window, or a muted
 *  "unavailable" note when the upstream fetch failed. */

type Variant = "compact" | "card";

const FILL: Record<UsageSeverity, string> = {
  normal: "bg-lamp",
  warning: "bg-warn",
  critical: "bg-failed",
};

const PERCENT: Record<UsageSeverity, string> = {
  normal: "text-moon-100",
  warning: "text-warn",
  critical: "text-failed",
};

function clampPercent(v: number): number {
  if (Number.isNaN(v)) return 0;
  return Math.max(0, Math.min(100, v));
}

export function usageProviderLabel(endpoint: EndpointUsage): string {
  return endpoint.plan ? `${endpoint.provider_name} · ${endpoint.plan}` : endpoint.provider_name;
}

function WindowRow({ window, variant }: { window: UsageWindow; variant: Variant }) {
  const pct = clampPercent(window.used_percent);
  const rounded = Math.round(window.used_percent);
  const compact = variant === "compact";
  return (
    <div className="flex items-center gap-2">
      <span
        className={cn(
          "shrink-0 text-moon-600",
          compact ? "w-20 text-[11px]" : "w-24 text-xs",
        )}
      >
        {window.label}
      </span>
      <div
        role="meter"
        aria-valuenow={rounded}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`${window.label}: ${rounded}% used`}
        className={cn(
          "relative min-w-0 flex-1 overflow-hidden rounded-full bg-ink-700",
          compact ? "h-1.5" : "h-2",
        )}
      >
        <div
          className={cn("h-full rounded-full transition-[width]", FILL[window.severity])}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span
        className={cn(
          "shrink-0 text-right font-mono tabular-nums",
          compact ? "w-9 text-[11px]" : "w-11 text-xs",
          PERCENT[window.severity],
        )}
      >
        {rounded}%
      </span>
      {window.resets_at && (
        <span
          className={cn(
            "shrink-0 text-moon-600",
            compact ? "hidden w-16 text-[10px] sm:inline" : "w-20 text-[11px]",
          )}
        >
          resets {relativeTime(window.resets_at)}
        </span>
      )}
    </div>
  );
}

/** Just the window rows for one endpoint (no provider header) — the header is
 *  rendered by the caller (a SectionHeader in the popover, a ChartCard title on
 *  analytics). Falls back to a muted note on error or an empty window list. */
export function UsageWindowRows({
  endpoint,
  variant = "compact",
}: {
  endpoint: EndpointUsage;
  variant?: Variant;
}) {
  const compact = variant === "compact";
  if (endpoint.error) {
    return (
      <p className={cn("text-moon-600", compact ? "text-[11px]" : "text-xs")}>
        Usage unavailable right now.
      </p>
    );
  }
  if (endpoint.windows.length === 0) {
    return (
      <p className={cn("text-moon-600", compact ? "text-[11px]" : "text-xs")}>No active windows.</p>
    );
  }
  return (
    <div className={compact ? "space-y-1" : "space-y-1.5"}>
      {endpoint.windows.map((w, i) => (
        <WindowRow key={`${w.label}-${i}`} window={w} variant={variant} />
      ))}
    </div>
  );
}

function EndpointBlock({ endpoint, variant }: { endpoint: EndpointUsage; variant: Variant }) {
  const compact = variant === "compact";
  return (
    <div className={compact ? "space-y-1" : "space-y-1.5"}>
      <div className="flex items-baseline justify-between gap-2">
        <span className={cn("min-w-0 truncate font-medium text-moon-100", compact ? "text-xs" : "text-sm")}>
          {usageProviderLabel(endpoint)}
        </span>
        {endpoint.error && (
          <span className={cn("shrink-0 text-moon-600", compact ? "text-[10px]" : "text-[11px]")}>
            unavailable
          </span>
        )}
      </div>
      <UsageWindowRows endpoint={endpoint} variant={variant} />
    </div>
  );
}

export function UsageMeters({
  endpoints,
  variant = "compact",
}: {
  endpoints: EndpointUsage[];
  variant?: Variant;
}) {
  return (
    <div className={variant === "compact" ? "space-y-2" : "space-y-4"}>
      {endpoints.map((e) => (
        <EndpointBlock key={e.endpoint_id} endpoint={e} variant={variant} />
      ))}
    </div>
  );
}
