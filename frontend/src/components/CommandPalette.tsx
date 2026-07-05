import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import * as RDialog from "@radix-ui/react-dialog";
import {
  Archive,
  BarChart3,
  CalendarClock,
  CornerDownLeft,
  Inbox,
  LayoutDashboard,
  ListTodo,
  Plus,
  Search,
  Settings,
} from "lucide-react";
import type { ReactNode } from "react";
import { useSearch as useTicketSearch } from "@/api/search";
import { fuzzyFilter } from "@/lib/fuzzy";
import { useKeybinds } from "@/lib/keymap";
import { openComposer, openFullEditor } from "./composerBus";
import { Kbd } from "@/ui/Kbd";
import { StatusPill } from "@/ui/StatusPill";
import { ticketStatusKind } from "@/lib/status";
import { cn } from "@/lib/cn";

interface Command {
  id: string;
  label: string;
  icon: ReactNode;
  hint?: string;
  run: () => void;
}

export function CommandPalette() {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  // Registered on the capture-phase document dispatcher so it fires even when a
  // Radix overlay has trapped focus (the old Cmd+K-does-nothing bug).
  useKeybinds([
    {
      combo: "mod+k",
      label: "Command palette",
      group: "Global",
      allowInInput: true,
      handler: () => setOpen((o) => !o),
    },
  ]);

  useEffect(() => {
    const openIt = () => setOpen(true);
    window.addEventListener("nightdesk:open-palette", openIt);
    return () => window.removeEventListener("nightdesk:open-palette", openIt);
  }, []);

  useEffect(() => {
    if (open) {
      setQuery("");
      setActive(0);
    }
  }, [open]);

  const close = () => setOpen(false);
  const go = (fn: () => void) => {
    close();
    fn();
  };

  const commands: Command[] = useMemo(
    () => [
      { id: "desk", label: "Go to Desk", icon: <LayoutDashboard size={15} />, run: () => navigate({ to: "/" }) },
      { id: "tickets", label: "Go to Tickets", icon: <ListTodo size={15} />, run: () => navigate({ to: "/tickets" }) },
      { id: "inbox", label: "Go to Inbox", icon: <Inbox size={15} />, run: () => navigate({ to: "/inbox" }) },
      { id: "scheduled", label: "Go to Scheduled", icon: <CalendarClock size={15} />, run: () => navigate({ to: "/scheduled" }) },
      { id: "analytics", label: "Go to Analytics", icon: <BarChart3 size={15} />, run: () => navigate({ to: "/analytics" }) },
      { id: "archive", label: "Go to Archive", icon: <Archive size={15} />, run: () => navigate({ to: "/archive" }) },
      { id: "settings", label: "Go to Settings", icon: <Settings size={15} />, run: () => navigate({ to: "/settings" }) },
      { id: "new", label: "New ticket (quick capture)", icon: <Plus size={15} />, hint: "c", run: () => openComposer() },
      { id: "new-full", label: "New ticket (full editor)", icon: <Plus size={15} />, run: () => openFullEditor() },
    ],
    [navigate],
  );

  const filteredCommands = useMemo(
    () => fuzzyFilter(query, commands, (c) => c.label),
    [query, commands],
  );

  const searchQ = useTicketSearch(query);
  const ticketHits = query.trim().length > 0 ? searchQ.data ?? [] : [];

  // Flatten to a single navigable list: commands first, then ticket hits.
  const rows = useMemo(() => {
    const cmdRows = filteredCommands.map((c) => ({ kind: "command" as const, cmd: c }));
    const hitRows = ticketHits
      .slice(0, 8)
      .map((h) => ({ kind: "ticket" as const, hit: h }));
    return [...cmdRows, ...hitRows];
  }, [filteredCommands, ticketHits]);

  useEffect(() => {
    if (active >= rows.length) setActive(0);
  }, [rows.length, active]);

  const execute = (i: number) => {
    const row = rows[i];
    if (!row) return;
    if (row.kind === "command") go(row.cmd.run);
    else go(() => navigate({ to: "/tickets/$id", params: { id: row.hit.id } }));
  };

  return (
    <RDialog.Root open={open} onOpenChange={setOpen}>
      <RDialog.Portal>
        <RDialog.Overlay className="overlay-in fixed inset-0 z-50 bg-ink-950/70 backdrop-blur-[2px]" />
        <RDialog.Content
          onOpenAutoFocus={(e) => {
            e.preventDefault();
            inputRef.current?.focus();
          }}
          className="pop-in fixed left-1/2 top-[15%] z-50 w-[calc(100vw-2rem)] max-w-xl -translate-x-1/2 rounded-card border border-ink-700 bg-ink-900 shadow-[var(--shadow-pop)] focus:outline-none"
        >
          <RDialog.Title className="sr-only">Command palette</RDialog.Title>
          <div className="flex items-center gap-2.5 border-b border-ink-700 px-4 py-3">
            <Search size={16} className="text-moon-400" />
            <input
              ref={inputRef}
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setActive(0);
              }}
              onKeyDown={(e) => {
                if (e.key === "ArrowDown") {
                  e.preventDefault();
                  setActive((a) => Math.min(rows.length - 1, a + 1));
                } else if (e.key === "ArrowUp") {
                  e.preventDefault();
                  setActive((a) => Math.max(0, a - 1));
                } else if (e.key === "Enter") {
                  e.preventDefault();
                  execute(active);
                }
              }}
              placeholder="Search tickets or jump to…"
              className="flex-1 bg-transparent text-sm text-moon-100 placeholder:text-moon-600 focus:outline-none"
            />
            <Kbd>esc</Kbd>
          </div>

          <div className="max-h-[50vh] overflow-y-auto p-1.5">
            {rows.length === 0 ? (
              <p className="px-3 py-6 text-center text-sm text-moon-600">No matches.</p>
            ) : (
              rows.map((row, i) => (
                <button
                  key={row.kind === "command" ? row.cmd.id : `t-${row.hit.id}`}
                  onMouseEnter={() => setActive(i)}
                  onClick={() => execute(i)}
                  className={cn(
                    "flex w-full items-center gap-3 rounded-control px-2.5 py-2 text-left text-sm",
                    i === active ? "bg-ink-800 text-moon-100" : "text-moon-400 hover:bg-ink-800",
                  )}
                >
                  {row.kind === "command" ? (
                    <>
                      <span className="text-moon-400">{row.cmd.icon}</span>
                      <span className="flex-1">{row.cmd.label}</span>
                      {row.cmd.hint && <Kbd>{row.cmd.hint}</Kbd>}
                    </>
                  ) : (
                    <>
                      <StatusPill status={ticketStatusKind(row.hit.status)} />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-moon-100">{row.hit.title}</span>
                        {row.hit.snippet && (
                          <span className="block truncate text-xs text-moon-600">{row.hit.snippet}</span>
                        )}
                      </span>
                      {row.hit.project_name && (
                        <span className="shrink-0 text-xs text-moon-600">{row.hit.project_name}</span>
                      )}
                    </>
                  )}
                  {i === active && <CornerDownLeft size={13} className="shrink-0 text-moon-600" />}
                </button>
              ))
            )}
          </div>
        </RDialog.Content>
      </RDialog.Portal>
    </RDialog.Root>
  );
}
