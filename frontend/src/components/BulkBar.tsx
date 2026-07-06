import { Archive, Check, Tag, X } from "lucide-react";
import { useState } from "react";
import type { LabelOut, ProjectOut, TicketStatus } from "@/api/types";
import { ticketsApi } from "@/api/tickets";
import { Button } from "@/ui/Button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/ui/DropdownMenu";
import { toast } from "@/ui/Toast";
import { PRIORITY_SCALE } from "@/lib/priority";
import { PriorityChip } from "@/components/PriorityChip";
import { ProjectDot } from "@/components/ProjectDot";
import { StatusPill } from "@/ui/StatusPill";
import { cn } from "@/lib/cn";

/** Which bulk menu is open. Controlled so the Tickets page keybinds (p / Shift+P
 *  / s / Shift+L when a selection exists) can open the matching one at the bar. */
export type BulkMenu = "status" | "priority" | "project" | "label";

/** Floating bulk-action bar for a multi-selection. Rendered by the Tickets page
 *  so it works in both board and list views. */
export function BulkBar({
  ids,
  projects = [],
  labels = [],
  openMenu = null,
  onOpenMenuChange,
  onClear,
  onDone,
}: {
  ids: string[];
  projects?: ProjectOut[];
  labels?: LabelOut[];
  /** Programmatically-opened menu (from keyboard), or null. */
  openMenu?: BulkMenu | null;
  onOpenMenuChange: (menu: BulkMenu | null) => void;
  onClear: () => void;
  onDone: () => void;
}) {
  const count = ids.length;
  // Bulk labels replaces the label set on every selected ticket; build it up
  // across toggles while the menu stays open, then apply on each toggle.
  const [pendingLabels, setPendingLabels] = useState<string[]>([]);
  if (count === 0) return null;

  const toggle = (menu: BulkMenu, open: boolean) => onOpenMenuChange(open ? menu : null);

  async function run(label: string, fn: () => Promise<unknown>) {
    try {
      await fn();
      onDone();
      // Keep the selection so further bulk ops (set priority, then project, …)
      // can chain without re-selecting. Escape or the X button clears it.
      toast.success(`${label} · ${count} ticket${count === 1 ? "" : "s"}`);
    } catch (err) {
      toast.error(`${label} failed`, { error: err });
    }
  }

  async function applyLabels(next: string[]) {
    setPendingLabels(next);
    try {
      await ticketsApi.bulkLabels({ ticket_ids: ids, label_ids: next });
      onDone();
    } catch (err) {
      toast.error("Labels failed", { error: err });
    }
  }

  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-6 z-30 flex justify-center px-4">
      <div className="pointer-events-auto flex items-center gap-2 rounded-card border border-ink-700 bg-ink-800 px-3 py-2 shadow-[var(--shadow-pop)]">
        <span className="pr-1 text-sm text-moon-100">
          <span className="font-mono">{count}</span> selected
        </span>

        <DropdownMenu open={openMenu === "status"} onOpenChange={(o) => toggle("status", o)}>
          <DropdownMenuTrigger asChild>
            <Button size="sm" variant="subtle">
              Status
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent side="top">
            <DropdownMenuLabel>Set status</DropdownMenuLabel>
            {(["draft", "queued", "review"] as TicketStatus[]).map((s) => (
              <DropdownMenuItem
                key={s}
                onSelect={() => run("Status", () => ticketsApi.bulkStatus({ ticket_ids: ids, status: s }))}
              >
                <StatusPill status={s} />
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>

        <DropdownMenu open={openMenu === "priority"} onOpenChange={(o) => toggle("priority", o)}>
          <DropdownMenuTrigger asChild>
            <Button size="sm" variant="subtle">
              Priority
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent side="top">
            <DropdownMenuLabel>Set priority</DropdownMenuLabel>
            {[...PRIORITY_SCALE].reverse().map((p) => (
              <DropdownMenuItem
                key={p.value}
                onSelect={() =>
                  run("Priority", () => ticketsApi.bulkPriority({ ticket_ids: ids, priority: p.value }))
                }
              >
                <span className="flex items-center gap-2">
                  <PriorityChip value={p.value} /> {p.label}
                </span>
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>

        <DropdownMenu open={openMenu === "project"} onOpenChange={(o) => toggle("project", o)}>
          <DropdownMenuTrigger asChild>
            <Button size="sm" variant="subtle">
              Project
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent side="top">
            <DropdownMenuLabel>Move to project</DropdownMenuLabel>
            <DropdownMenuItem
              onSelect={() => run("Project", () => ticketsApi.bulkProject({ ticket_ids: ids, project_id: null }))}
            >
              <span className="flex items-center gap-2">
                <ProjectDot color={null} /> No project
              </span>
            </DropdownMenuItem>
            {projects.map((p) => (
              <DropdownMenuItem
                key={p.id}
                onSelect={() => run("Project", () => ticketsApi.bulkProject({ ticket_ids: ids, project_id: p.id }))}
              >
                <span className="flex items-center gap-2">
                  <ProjectDot color={p.color} /> {p.name}
                </span>
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>

        <DropdownMenu
          open={openMenu === "label"}
          onOpenChange={(o) => {
            // Seed a fresh label set each time the menu opens; the toggles below
            // apply immediately, so no work is needed on close.
            if (o) setPendingLabels([]);
            toggle("label", o);
          }}
        >
          <DropdownMenuTrigger asChild>
            <Button size="sm" variant="subtle" leadingIcon={<Tag size={13} />}>
              Labels
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent side="top">
            <DropdownMenuLabel>Set labels (replaces on all)</DropdownMenuLabel>
            {labels.length === 0 && (
              <div className="px-2 py-1.5 text-xs text-moon-600">No labels defined</div>
            )}
            {labels.map((l) => {
              const on = pendingLabels.includes(l.id);
              return (
                <DropdownMenuItem
                  key={l.id}
                  keepOpen
                  onSelect={() =>
                    applyLabels(on ? pendingLabels.filter((x) => x !== l.id) : [...pendingLabels, l.id])
                  }
                >
                  <span className="flex flex-1 items-center gap-2">
                    <span
                      className={cn("h-2.5 w-2.5 rounded-full", !on && "opacity-40")}
                      style={{ backgroundColor: l.color }}
                    />
                    {l.name}
                  </span>
                  {on && <Check size={14} className="text-lamp" />}
                </DropdownMenuItem>
              );
            })}
          </DropdownMenuContent>
        </DropdownMenu>

        <Button
          size="sm"
          variant="ghost"
          leadingIcon={<Archive size={13} />}
          onClick={() => run("Archived", () => ticketsApi.bulkArchive({ ticket_ids: ids }))}
        >
          Archive
        </Button>

        <button
          onClick={onClear}
          className="ml-1 rounded-control p-1 text-moon-400 hover:bg-ink-700 hover:text-moon-100"
          aria-label="Clear selection"
        >
          <X size={15} />
        </button>
      </div>
    </div>
  );
}
