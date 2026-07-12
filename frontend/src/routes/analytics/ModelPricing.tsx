import { useState } from "react";
import { ChevronDown, Info } from "lucide-react";
import { Tooltip } from "@/ui/Tooltip";
import { cn } from "@/lib/cn";
import { formatUsd } from "@/lib/status";
import type { PriceRow, SpendModelRow } from "@/api/analytics";

/**
 * "Model pricing" disclosure for the Analytics Costs section.
 *
 * Why a collapsible section (not a per-row popover): each model needs four
 * rates, an effective blended $/M, the cache split, and provenance — too dense
 * for a popover. A disclosure keeps the default Costs view at Linear density
 * (the spend breakdown leads) and expands the rate detail on demand, without
 * pushing it onto its own scroll-only screen.
 *
 * The rates are the CURRENT effective set (live → cache → bundled), not what
 * any past run was priced at; the intro line labels that honestly. Blended is
 * the range's effective $/M (cost ÷ total tokens) — shown next to the cache-hit
 * split because cache-read dominance is what makes blended look tiny.
 */
export function ModelPricing({
  prices,
  models,
  loading,
  sourceLabel,
}: {
  prices: PriceRow[] | undefined;
  models: SpendModelRow[];
  loading: boolean;
  sourceLabel: string;
}) {
  const [open, setOpen] = useState(false);
  if (models.length === 0) return null;

  const priceByModel = new Map<string, PriceRow>();
  for (const p of prices ?? []) priceByModel.set(p.model, p);

  const rows = models.map((m) => ({
    model: m.model,
    cost: m.cost ?? 0,
    total_tokens: m.total_tokens,
    cache_hit_rate: m.cache_hit_rate,
    price: priceByModel.get(m.model),
  }));

  return (
    <div className="mt-4 rounded-card border border-ink-700/50 bg-ink-900 shadow-[var(--shadow-raised)]">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left transition-colors hover:bg-ink-800/40"
      >
        <span className="flex items-center gap-2">
          <span className="font-display text-sm font-semibold text-moon-100">Model pricing</span>
          <Tooltip content="The per-million-token rates behind these cost numbers — what the system resolves right now (live → cache → bundled), not what any past run was priced at. Blended is this range's effective $/M (cost ÷ tokens).">
            <Info size={13} className="text-moon-600" aria-hidden />
          </Tooltip>
        </span>
        <span className="flex items-center gap-3">
          <span className="font-mono text-[11px] text-moon-600">{sourceLabel}</span>
          <ChevronDown
            size={16}
            className={cn("text-moon-500 transition-transform", open && "rotate-180")}
            aria-hidden
          />
        </span>
      </button>

      {open && (
        <div className="border-t border-ink-700/50 px-4 py-3">
          <p className="mb-3 text-[11px] leading-relaxed text-moon-600">
            Current effective rates (USD per 1M tokens) plus this range&apos;s blended actuals.
            Historical runs were priced by their own frozen snapshot, which may differ from today&apos;s
            table.
          </p>
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-left">
              <thead>
                <tr className="text-[10px] uppercase tracking-wide text-moon-600">
                  <th className="py-1 pr-3 font-medium">Model</th>
                  <th className="py-1 px-2 text-right font-medium">Input</th>
                  <th className="py-1 px-2 text-right font-medium">Output</th>
                  <th className="py-1 px-2 text-right font-medium">Cache W</th>
                  <th className="py-1 px-2 text-right font-medium">Cache R</th>
                  <th className="py-1 px-2 text-right font-medium">
                    <Tooltip content="Effective $/M for the range = cost ÷ total tokens. It looks tiny when most reads are cache hits (cache-read rates are ~10–50× cheaper) — that is expected, not a bug. See the Cache hit column.">
                      <span className="cursor-help">Blended</span>
                    </Tooltip>
                  </th>
                  <th className="py-1 px-2 text-right font-medium">Cache hit</th>
                  <th className="py-1 pl-2 font-medium">Source</th>
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <tr>
                    <td colSpan={8} className="py-6 text-center text-xs text-moon-600">
                      Loading prices…
                    </td>
                  </tr>
                ) : (
                  rows.map((r) => {
                    const p = r.price;
                    const blended = r.total_tokens > 0 ? (r.cost / r.total_tokens) * 1e6 : null;
                    const src = p?.source ?? "none";
                    const unpriced = src === "none";
                    return (
                      <tr key={r.model} className="border-t border-ink-700/40">
                        <th scope="row" className="py-1.5 pr-3 text-left">
                          <span className="font-mono text-[12px] text-moon-100">{r.model}</span>
                        </th>
                        <td className="py-1.5 px-2 text-right font-mono text-[11px] tabular-nums text-moon-300">
                          {fmtRate(p?.input)}
                        </td>
                        <td className="py-1.5 px-2 text-right font-mono text-[11px] tabular-nums text-moon-300">
                          {fmtRate(p?.output)}
                        </td>
                        <td className="py-1.5 px-2 text-right font-mono text-[11px] tabular-nums text-moon-300">
                          {fmtRate(p?.cache_write)}
                        </td>
                        <td className="py-1.5 px-2 text-right font-mono text-[11px] tabular-nums text-moon-300">
                          {fmtRate(p?.cache_read)}
                        </td>
                        <td className="py-1.5 px-2 text-right font-mono text-[11px] tabular-nums text-moon-100">
                          {blended == null ? "—" : formatUsd(blended)}
                        </td>
                        <td className="py-1.5 px-2 text-right font-mono text-[11px] tabular-nums text-moon-400">
                          {pct(r.cache_hit_rate)}
                        </td>
                        <td className="py-1.5 pl-2">
                          <div className="flex items-center gap-1.5">
                            <span
                              className={cn(
                                "font-mono text-[10px]",
                                unpriced ? "text-warn" : "text-moon-500",
                              )}
                            >
                              {sourceCell(src, p?.as_of)}
                            </span>
                            {p?.repriced_since && (
                              <Tooltip content="Some runs in this range were priced on a different snapshot than today's rates — historical spend may not match these rates.">
                                <span className="cursor-default rounded-full border border-warn/25 bg-warn/10 px-1 py-px text-[9px] font-semibold uppercase tracking-wide text-warn">
                                  repriced
                                </span>
                              </Tooltip>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

function fmtRate(v: number | null | undefined): string {
  if (v == null) return "—";
  return `$${v % 1 === 0 ? v.toFixed(0) : v.toFixed(2)}`;
}

function pct(v: number | null | undefined): string {
  if (v == null) return "—";
  return `${Math.round(v * 100)}%`;
}

function sourceCell(src: PriceRow["source"], asOf: string | null | undefined): string {
  if (src === "none") return "no pricing";
  const label = src === "live" ? "live" : src === "cache" ? "cached" : "bundled";
  return asOf ? `${label} · ${asOf}` : label;
}
