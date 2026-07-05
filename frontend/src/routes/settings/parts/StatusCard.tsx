import type { ReactNode } from "react";
import { CheckCircle2, HelpCircle, XCircle } from "lucide-react";
import { cn } from "@/lib/cn";

export type CheckTone = "ok" | "fail" | "unknown";

/** A single environment check rendered as a status card (sage ok / ember fail).
 *  Used by the Claude runtime and Diagnostics sections. */
export function StatusCard({
  tone,
  title,
  value,
  detail,
}: {
  tone: CheckTone;
  title: string;
  value?: ReactNode;
  detail?: ReactNode;
}) {
  const Icon = tone === "ok" ? CheckCircle2 : tone === "fail" ? XCircle : HelpCircle;
  const accent =
    tone === "ok"
      ? "text-success border-success/30"
      : tone === "fail"
        ? "text-failed border-failed/30"
        : "text-moon-400 border-ink-700";
  return (
    <div className={cn("rounded-card border bg-ink-900 p-3.5", accent.split(" ")[1])}>
      <div className="flex items-center gap-2">
        <Icon size={15} className={accent.split(" ")[0]} />
        <span className="text-xs font-medium uppercase tracking-wide text-moon-400">{title}</span>
      </div>
      {value != null && (
        <div className="mt-1.5 break-all font-mono text-[13px] text-moon-100">{value}</div>
      )}
      {detail && <div className="mt-1 break-all text-xs text-moon-600">{detail}</div>}
    </div>
  );
}
