import { useState } from "react";
import type { ReactNode } from "react";
import { AlertTriangle, X } from "lucide-react";
import { cn } from "@/lib/cn";

export interface ExperimentalBannerProps {
  feature?: string;
  children?: ReactNode;
  storageKey?: string;
  className?: string;
}

function dismissedKey(storageKey: string): string {
  return `experimental-banner-dismissed:${storageKey}`;
}

export function ExperimentalBanner({ feature, children, storageKey, className }: ExperimentalBannerProps) {
  const [dismissed, setDismissed] = useState(() => {
    if (!storageKey) return false;
    return sessionStorage.getItem(dismissedKey(storageKey)) === "1";
  });

  if (dismissed) return null;

  const message =
    children ??
    (feature
      ? `Experimental — ${feature} is new in this build and may change or break.`
      : "This page is experimental and may change or break.");

  return (
    <div
      className={cn(
        "flex items-center gap-2 rounded-control border border-warn/25 bg-warn/12",
        "px-3 py-2 text-sm text-warn-soft",
        className,
      )}
    >
      <AlertTriangle size={14} className="shrink-0 text-warn" />
      <span className="flex-1">{message}</span>
      {storageKey && (
        <button
          type="button"
          aria-label="Dismiss"
          onClick={() => {
            sessionStorage.setItem(dismissedKey(storageKey), "1");
            setDismissed(true);
          }}
          className="shrink-0 rounded-control p-1 text-warn/70 transition-colors duration-100 hover:bg-warn/15 hover:text-warn"
        >
          <X size={13} />
        </button>
      )}
    </div>
  );
}
