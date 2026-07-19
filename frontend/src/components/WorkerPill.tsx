import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { Link } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import * as Popover from "@radix-ui/react-popover";
import { Activity, Clock, Cpu, ExternalLink, Gauge, Server, Zap } from "lucide-react";
import { useWorkerStatus } from "@/api/config";
import { analyticsApi, type SpendResponse } from "@/api/analytics";
import { useSubscriptionUsage, USAGE_POLL, type SubscriptionUsage } from "@/api/usage";
import type { WorkerStatusOut } from "@/api/types";
import { formatTokens, formatUsd } from "@/lib/status";
import { cn } from "@/lib/cn";
import { durationBetween, parseTs } from "@/lib/time";
import { UsageMeters } from "@/components/UsageMeter";

/** One status pill + one rich popover, merging the old separate worker pill
 *  and spend chip. Polls /api/v1/worker/status every 3s; the popover's deeper
 *  spend breakdown is fetched lazily from /api/v1/analytics/spend only while
 *  open, so the 3s poll stays cheap.
 *
 *  Interaction: hover opens, click pins (so the links inside are clickable),
 *  Esc or click-outside closes. The running-ticket entries are real TanStack
 *  `<Link>` anchors, so middle-click opens them in a new tab. */

type Tone = "running" | "idle" | "stale" | "offline";

const DOT_CLASS: Record<Tone, string> = {
  running: "bg-lamp",
  idle: "bg-success",
  stale: "bg-failed",
  offline: "bg-moon-600",
};

const TONE_LABEL: Record<Tone, string> = {
  running: "Running",
  idle: "Idle",
  stale: "Stale",
  offline: "Offline",
};

function deriveTone(
  data: WorkerStatusOut | undefined,
  isError: boolean,
  isLoading: boolean,
): Tone {
  if (isLoading) return "idle";
  if (isError || !data) return "offline";
  if (data.stale) return "stale";
  if (data.total_running > 0) return "running";
  return "idle";
}

/** Backend clock rendered in the schedule zone, with tz abbreviation. */
function formatServerNow(iso: string | null | undefined, tz: string): string {
  const ms = parseTs(iso);
  if (ms === null) return "—";
  try {
    return new Date(ms).toLocaleString(undefined, {
      timeZone: tz,
      weekday: "short",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      timeZoneName: "short",
    });
  } catch {
    // Invalid tz name -> fall back to the local zone, no abbreviation.
    return new Date(ms).toLocaleString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }
}

export function WorkerPill() {
  const { data, isError, isLoading } = useWorkerStatus();
  const tone = deriveTone(data, isError, isLoading);

  // Hover opens; click pins. open = pinned || hovered. Esc / click-outside
  // (delivered by Radix as onOpenChange(false)) clears both.
  const [pinned, setPinned] = useState(false);
  const [hovered, setHovered] = useState(false);
  const pinnedRef = useRef(false);
  pinnedRef.current = pinned;
  const closeTimer = useRef<number | undefined>(undefined);

  const arm = useCallback(() => {
    if (closeTimer.current) window.clearTimeout(closeTimer.current);
    setHovered(true);
  }, []);
  const disarm = useCallback(() => {
    if (closeTimer.current) window.clearTimeout(closeTimer.current);
    closeTimer.current = window.setTimeout(() => {
      if (!pinnedRef.current) setHovered(false);
    }, 140);
  }, []);
  useEffect(() => () => { if (closeTimer.current) window.clearTimeout(closeTimer.current); }, []);

  const open = pinned || hovered;

  // Deeper per-model / token-split breakdown for today. Only fetched while the
  // popover is open; cache is shared with the analytics page. Degrades away
  // silently (the always-on numbers below don't depend on it).
  const spendQ = useQuery<SpendResponse>({
    queryKey: ["analytics", "spend", "today", null],
    queryFn: () => analyticsApi.spend("today"),
    enabled: open,
    staleTime: 60_000,
    retry: false,
  });

  // Subscription rate-limit windows, same lazy-while-open pattern as spend.
  const usageQ = useSubscriptionUsage({ enabled: open, staleTime: USAGE_POLL });

  const slots = data
    ? data.max_parallel > 0
      ? `${data.total_running}/${data.max_parallel}`
      : `${data.total_running}`
    : "—";

  return (
    <Popover.Root
      open={open}
      onOpenChange={(o) => {
        if (!o) {
          setPinned(false);
          setHovered(false);
        }
      }}
    >
      <Popover.Anchor asChild>
        <button
          type="button"
          onClick={() => setPinned((p) => !p)}
          onMouseEnter={arm}
          onMouseLeave={disarm}
          aria-haspopup="dialog"
          aria-expanded={open}
          aria-label={
            data
              ? `Worker ${TONE_LABEL[tone].toLowerCase()}, ${slots} slots, ${formatUsd(
                  data.day_spend_usd,
                )} today. Open for details.`
              : "Worker status. Open for details."
          }
          className={cn(
            "flex h-8 items-center gap-2 rounded-control border border-ink-700 bg-ink-900 px-2.5",
            "text-xs transition-colors hover:bg-ink-800",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-lamp/50 focus-visible:ring-offset-1 focus-visible:ring-offset-ink-950",
          )}
        >
          <span
            className={cn(
              "h-2 w-2 shrink-0 rounded-full",
              DOT_CLASS[tone],
              tone === "running" && "motion-safe:animate-pulse",
            )}
          />
          <span className="hidden shrink-0 font-medium text-moon-100 sm:inline">
            {TONE_LABEL[tone]}
          </span>
          <span aria-hidden className="hidden h-3 w-px shrink-0 bg-ink-700 sm:inline" />
          <span className="shrink-0 font-mono tabular-nums text-moon-400">{slots}</span>
          <span aria-hidden className="hidden h-3 w-px shrink-0 bg-ink-700 sm:inline" />
          <span className="hidden shrink-0 font-mono sm:inline">
            <span className="text-lamp">{formatUsd(data?.day_spend_usd)}</span>
            <span className="ml-1 text-moon-600">today</span>
          </span>
        </button>
      </Popover.Anchor>
      <Popover.Portal>
        <Popover.Content
          onMouseEnter={arm}
          onMouseLeave={disarm}
          onOpenAutoFocus={(e) => e.preventDefault()}
          align="end"
          sideOffset={6}
          collisionPadding={8}
          aria-label="Worker status"
          className={cn(
            "pop-in z-50 w-[300px] rounded-card border border-ink-700 bg-ink-800 p-3",
            "shadow-[var(--shadow-pop)] outline-none",
            "max-h-[min(70vh,540px)] overflow-y-auto overscroll-contain",
            "sm:w-[340px]",
          )}
        >
          {data ? (
            <WorkerPopoverContent data={data} spend={spendQ.data} usage={usageQ.data} tone={tone} />
          ) : (
            <OfflineState isLoading={isLoading} />
          )}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

function OfflineState({ isLoading }: { isLoading: boolean }) {
  return (
    <div className="space-y-2 py-1">
      <SectionHeader icon={<Activity size={11} />} title="Worker" />
      <p className="text-xs text-moon-400">
        {isLoading ? "Connecting to API…" : "Worker status unavailable — is the API running?"}
      </p>
    </div>
  );
}

function SectionHeader({ icon, title }: { icon: ReactNode; title: string }) {
  return (
    <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-moon-600">
      {icon}
      {title}
    </div>
  );
}

function Separator() {
  return <div className="my-2 h-px bg-ink-700/70" />;
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-0.5">
      <dt className="shrink-0 text-[11px] text-moon-600">{label}</dt>
      <dd className="min-w-0 truncate text-right font-mono text-[11px] text-moon-100">
        {children}
      </dd>
    </div>
  );
}

function WorkerPopoverContent({
  data,
  spend,
  usage,
  tone,
}: {
  data: WorkerStatusOut;
  spend: SpendResponse | undefined;
  usage: SubscriptionUsage | undefined;
  tone: Tone;
}) {
  const usageEndpoints = usage?.endpoints ?? [];
  const byModel = spend
    ? [...spend.by_model].sort((a, b) => b.cost - a.cost).slice(0, 4)
    : [];

  return (
    <div className="space-y-0.5">
      {/* Worker ---------------------------------------------------------- */}
      <div className="space-y-1">
        <SectionHeader icon={<Activity size={11} />} title="Worker" />
        <div className="flex items-center gap-2 py-0.5">
          <span
            className={cn(
              "h-2 w-2 rounded-full",
              DOT_CLASS[tone],
              tone === "running" && "motion-safe:animate-pulse",
            )}
          />
          <span className="text-sm font-medium text-moon-100">{TONE_LABEL[tone]}</span>
          <span className="ml-auto font-mono text-[11px] text-moon-400">
            {data.max_parallel > 0
              ? `${data.total_running}/${data.max_parallel} slots`
              : `${data.total_running} running`}
          </span>
        </div>
        <dl>
          <Row label="Schedule">
            {data.active_window ?? "no window"}
            <span className="text-moon-600"> · </span>
            <span className={data.in_window ? "text-success" : "text-moon-600"}>
              {data.in_window ? "in window" : "off hours"}
            </span>
          </Row>
          <Row label="Backend time">
            {formatServerNow(data.server_now, data.schedule_timezone)}
          </Row>
          <Row label="Process">
            {data.pid != null && data.host ? (
              <>
                <span className="text-moon-400">pid</span> {data.pid}
                <span className="text-moon-600"> @ </span>
                {data.host}
              </>
            ) : (
              <span className="text-moon-600">no heartbeat</span>
            )}
          </Row>
          {data.last_seen_at && (
            <Row label="Last seen">
              <span className="text-moon-400">{durationBetween(data.last_seen_at, null)}</span>
              <span className="text-moon-600"> ago</span>
            </Row>
          )}
        </dl>
      </div>

      {/* Limits -------------------------------------------------------- */}
      {usageEndpoints.length > 0 && (
        <>
          <Separator />
          <div className="space-y-1.5">
            <SectionHeader icon={<Gauge size={11} />} title="Limits" />
            <UsageMeters endpoints={usageEndpoints} variant="compact" />
          </div>
        </>
      )}

      {/* Running tickets ----------------------------------------------- */}
      <Separator />
      <div className="space-y-1">
        <SectionHeader
          icon={<Zap size={11} />}
          title={`Running · ${data.total_running}${data.max_parallel > 0 ? ` of ${data.max_parallel}` : ""}`}
        />
        {data.running_tickets.length === 0 ? (
          <p className="py-1 text-[11px] text-moon-600">Nothing in flight right now.</p>
        ) : (
          <ul className="space-y-0.5">
            {data.running_tickets.map((t) => (
              <li key={t.id}>
                <Link
                  to="/tickets/$id"
                  params={{ id: t.id }}
                  className="group/tkt flex items-center gap-2 rounded-control px-1.5 py-1 hover:bg-ink-700"
                >
                  <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-lamp motion-safe:animate-pulse" />
                  <span className="min-w-0 flex-1 truncate text-xs text-moon-100">
                    {t.title || t.id}
                  </span>
                  {t.run_now && (
                    <span className="shrink-0 rounded-full bg-lamp/15 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-lamp">
                      run-now
                    </span>
                  )}
                  <span className="shrink-0 font-mono text-[10px] tabular-nums text-moon-600">
                    {durationBetween(t.started_at, null)}
                  </span>
                  <ExternalLink
                    size={11}
                    className="shrink-0 text-moon-600 opacity-0 transition-opacity group-hover/tkt:opacity-100"
                  />
                </Link>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Spend --------------------------------------------------------- */}
      <Separator />
      <div className="space-y-1">
        <SectionHeader icon={<Server size={11} />} title="Spend" />
        <dl>
          <Row label="Today">
            <span className="text-lamp">{formatUsd(data.day_spend_usd)}</span>
            <span className="text-moon-600"> · </span>
            <span className="text-moon-400">{formatTokens(data.day_tokens)} tok</span>
          </Row>
          <Row label="Month to date">
            <span className="text-moon-100">{formatUsd(data.month_spend_usd)}</span>
          </Row>
        </dl>

        {spend && (
          <>
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 px-0.5 font-mono text-[10px] text-moon-600">
              <span>
                in <span className="text-moon-400">{formatTokens(spend.totals.input_tokens)}</span>
              </span>
              <span>
                out <span className="text-moon-400">{formatTokens(spend.totals.output_tokens)}</span>
              </span>
              <span>
                cache{" "}
                <span className="text-moon-400">
                  {formatTokens(spend.totals.cache_read_tokens + spend.totals.cache_write_tokens)}
                </span>
              </span>
              {spend.totals.cache_hit_rate > 0 && (
                <span>
                  hit{" "}
                  <span className="text-moon-400">
                    {Math.round(spend.totals.cache_hit_rate * 100)}%
                  </span>
                </span>
              )}
            </div>
            {byModel.length > 0 && (
              <ul className="mt-1 space-y-0.5">
                {byModel.map((m) => (
                  <li
                    key={m.model}
                    className="flex items-center gap-2 rounded-control px-1.5 py-0.5"
                  >
                    <Cpu size={10} className="shrink-0 text-moon-600" />
                    <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-moon-400">
                      {m.model}
                    </span>
                    <span className="shrink-0 font-mono text-[11px] text-lamp">
                      {formatUsd(m.cost)}
                    </span>
                    <span className="shrink-0 font-mono text-[10px] tabular-nums text-moon-600">
                      {formatTokens(m.total_tokens)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
            <p className="flex items-center gap-1 px-1 pt-0.5 text-[9px] text-moon-600">
              <Clock size={9} />
              repriced · {spend.price_source}
            </p>
          </>
        )}
      </div>
    </div>
  );
}
