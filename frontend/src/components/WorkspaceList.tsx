import { FolderGit2, GitBranch } from "lucide-react";
import type { TicketWorkspaceOut } from "@/api/types";
import { Badge } from "@/ui/Badge";
import { Tooltip } from "@/ui/Tooltip";

/** Read-only render of a ticket's workspaces (primary + additional). Shared by
 *  the detail PropertiesRail and the board side-peek so both describe where a
 *  run will land identically. */
export function WorkspaceList({ workspaces }: { workspaces: TicketWorkspaceOut[] }) {
  if (workspaces.length === 0) {
    return <p className="text-xs text-moon-600">No workspace configured.</p>;
  }
  return (
    <div className="space-y-2">
      {workspaces.map((w) => (
        <div key={w.id} className="rounded-control border border-ink-700 bg-ink-900 p-2.5">
          <div className="mb-1 flex items-center gap-2">
            <Badge tone={w.role === "primary" ? "lamp" : "neutral"}>{w.role}</Badge>
            <span className="text-[11px] text-moon-400">{w.kind}</span>
            <span className="ml-auto text-[10px] text-moon-600">{w.access}</span>
          </div>
          <div className="flex items-center gap-1.5 font-mono text-[11px] text-moon-100">
            <FolderGit2 size={11} className="shrink-0 text-moon-600" />
            <Tooltip content={w.resolved_path ?? w.source_path ?? ""} mono side="top">
              <span className="truncate">{w.resolved_path ?? w.source_path ?? "—"}</span>
            </Tooltip>
          </div>
          {w.branch && (
            <div className="mt-0.5 flex items-center gap-1.5 font-mono text-[11px] text-moon-400">
              <GitBranch size={11} className="shrink-0 text-moon-600" />
              <span className="truncate">{w.branch}</span>
              {w.base_ref && <span className="text-moon-600">← {w.base_ref}</span>}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
