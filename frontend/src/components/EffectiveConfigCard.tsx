import { useState } from "react";
import { AlertTriangle, Cpu, Globe, Server, Settings2, ShieldCheck } from "lucide-react";
import { useEffectiveConfig } from "@/api/effectiveConfig";
import { Dialog } from "@/ui/Dialog";
import { Tooltip } from "@/ui/Tooltip";
import { cn } from "@/lib/cn";

interface EField {
  key: string;
  label: string;
  value: unknown;
  source_label?: string;
  note?: string | null;
  items?: unknown[];
}
interface EGroup {
  title: string;
  fields: EField[];
}
interface EConfig {
  groups?: EGroup[];
  issues?: string[];
}

function fieldByKey(cfg: EConfig | undefined, key: string): EField | undefined {
  for (const g of cfg?.groups ?? []) {
    const f = g.fields?.find((x) => x.key === key);
    if (f) return f;
  }
  return undefined;
}

function display(v: unknown): string {
  if (v == null) return "—";
  if (Array.isArray(v)) return v.length ? v.join(", ") : "—";
  return String(v);
}

/** Always-visible runtime summary (the settings that shape a run) with the full
 *  resolved effective-config one click away. Source: GET
 *  /api/v1/tickets/{tid}/effective-config. */
export function EffectiveConfigCard({ ticketId }: { ticketId: string }) {
  const { data, isLoading, isError } = useEffectiveConfig(ticketId);
  const [open, setOpen] = useState(false);

  const cfg = data as EConfig | undefined;
  const model = fieldByKey(cfg, "default_model");
  const perm = fieldByKey(cfg, "permission_mode");
  const backend = fieldByKey(cfg, "backend");
  const network = fieldByKey(cfg, "network_mode");
  const issues = cfg?.issues ?? [];

  const chips: { icon: typeof Cpu; label: string; value: string; tone?: string }[] = [];
  if (model) chips.push({ icon: Cpu, label: "Model", value: display(model.value) });
  if (perm) {
    const v = display(perm.value);
    chips.push({
      icon: ShieldCheck,
      label: "Permission mode",
      value: v,
      tone: v === "bypassPermissions" ? "text-failed" : undefined,
    });
  }
  if (backend) chips.push({ icon: Server, label: "Backend", value: display(backend.value) });
  if (network) {
    const v = display(network.value);
    chips.push({ icon: Globe, label: "Network", value: v, tone: v === "on" ? "text-lamp" : undefined });
  }

  return (
    <div className="rounded-card border border-ink-700 bg-ink-900 p-2.5">
      <div className="mb-1.5 flex items-center gap-2">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-moon-600">Runtime</span>
        <button
          onClick={() => setOpen(true)}
          className="ml-auto inline-flex items-center gap-1 rounded-control px-1.5 py-0.5 text-[11px] text-moon-400 hover:bg-ink-800 hover:text-moon-100"
        >
          <Settings2 size={12} /> Full config
        </button>
      </div>

      {isLoading ? (
        <p className="text-[11px] text-moon-600">Resolving…</p>
      ) : isError ? (
        <p className="text-[11px] text-moon-600">Effective config unavailable.</p>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {chips.map((c) => (
            <Tooltip key={c.label} content={c.label}>
              <span className="inline-flex items-center gap-1 rounded-full border border-ink-700 bg-ink-800 px-2 py-0.5 text-[11px]">
                <c.icon size={11} className="text-moon-600" />
                <span className={cn("font-mono", c.tone ?? "text-moon-100")}>{c.value}</span>
              </span>
            </Tooltip>
          ))}
          {issues.length > 0 && (
            <span className="inline-flex items-center gap-1 rounded-full border border-failed/40 bg-failed/10 px-2 py-0.5 text-[11px] text-failed">
              <AlertTriangle size={11} /> {issues.length} issue{issues.length === 1 ? "" : "s"}
            </span>
          )}
        </div>
      )}

      <Dialog
        open={open}
        onOpenChange={setOpen}
        title="Effective configuration"
        description="The resolved runtime for this ticket — profile, project, ticket overrides, and defaults, merged."
        size="lg"
      >
        <div className="max-h-[65vh] space-y-4 overflow-y-auto pr-1">
          {issues.length > 0 && (
            <div className="rounded-control border border-failed/40 bg-failed/10 p-3">
              <div className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-failed">
                <AlertTriangle size={12} /> Issues
              </div>
              <ul className="list-disc space-y-0.5 pl-4 text-xs text-failed">
                {issues.map((it, i) => (
                  <li key={i}>{it}</li>
                ))}
              </ul>
            </div>
          )}
          {(cfg?.groups ?? []).map((g) => (
            <div key={g.title}>
              <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-moon-400">
                {g.title}
              </div>
              <div className="overflow-hidden rounded-control border border-ink-700">
                {g.fields.map((f, i) => (
                  <div
                    key={f.key}
                    className={cn(
                      "grid grid-cols-[140px_1fr_auto] items-start gap-3 px-3 py-2 text-xs",
                      i > 0 && "border-t border-ink-700/60",
                    )}
                  >
                    <span className="text-moon-400">{f.label}</span>
                    <span className="min-w-0 break-words font-mono text-moon-100">
                      {display(f.value)}
                      {f.note && <span className="mt-0.5 block text-[11px] text-moon-600">{f.note}</span>}
                    </span>
                    {f.source_label && (
                      <span className="shrink-0 rounded-full bg-ink-800 px-1.5 py-0.5 text-[10px] text-moon-600">
                        {f.source_label}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </Dialog>
    </div>
  );
}
