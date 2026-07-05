import { Search } from "lucide-react";
import { Kbd } from "@/ui/Kbd";
import { WorkerPill } from "./WorkerPill";

/** Top strip: global search stub (Cmd+K palette lands in P3) + worker/spend. */
export function TopStrip() {
  return (
    <header className="flex h-14 shrink-0 items-center gap-3 border-b border-ink-700 bg-ink-950/60 px-4 backdrop-blur">
      <button
        type="button"
        onClick={() => window.dispatchEvent(new CustomEvent("nightdesk:open-palette"))}
        className="group flex h-8 w-full max-w-md items-center gap-2.5 rounded-control border border-ink-700 bg-ink-900 px-2.5 text-left text-sm text-moon-600 transition-colors hover:border-ink-700 hover:bg-ink-800"
      >
        <Search size={15} className="text-moon-400" />
        <span className="flex-1">Search tickets, runs, projects…</span>
        <span className="flex items-center gap-1">
          <Kbd>⌘</Kbd>
          <Kbd>K</Kbd>
        </span>
      </button>

      <div className="ml-auto flex items-center gap-2">
        <WorkerPill />
      </div>
    </header>
  );
}
