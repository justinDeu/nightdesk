import { Menu, Search } from "lucide-react";
import { Kbd } from "@/ui/Kbd";
import { WorkerPill } from "./WorkerPill";

/** Top strip: global search stub (Cmd+K palette lands in P3) + worker/spend.
 *  Below md a hamburger opens the nav drawer and the search collapses to a
 *  compact chip so nothing overflows on a phone. */
export function TopStrip({ onOpenNav }: { onOpenNav: () => void }) {
  return (
    <header className="flex h-14 shrink-0 items-center gap-2 overflow-hidden border-b border-ink-700 bg-ink-950/60 px-3 backdrop-blur sm:gap-3 sm:px-4">
      <button
        type="button"
        onClick={onOpenNav}
        aria-label="Open navigation"
        className="grid h-10 w-10 shrink-0 place-items-center rounded-control text-moon-400 hover:bg-ink-800 hover:text-moon-100 md:hidden"
      >
        <Menu size={20} />
      </button>

      <button
        type="button"
        onClick={() => window.dispatchEvent(new CustomEvent("nightdesk:open-palette"))}
        className="group flex h-9 min-w-0 flex-1 items-center gap-2.5 rounded-control border border-ink-700 bg-ink-900 px-2.5 text-left text-sm text-moon-600 transition-colors hover:border-ink-700 hover:bg-ink-800 sm:h-8 md:max-w-md"
      >
        <Search size={15} className="shrink-0 text-moon-400" />
        <span className="min-w-0 flex-1 truncate">
          <span className="hidden sm:inline">Search tickets, runs, projects…</span>
          <span className="sm:hidden">Search…</span>
        </span>
        <span className="hidden items-center gap-1 md:flex">
          <Kbd>⌘</Kbd>
          <Kbd>K</Kbd>
        </span>
      </button>

      <div className="ml-auto flex shrink-0 items-center gap-2">
        <WorkerPill />
      </div>
    </header>
  );
}
