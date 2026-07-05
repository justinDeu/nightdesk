import { cn } from "@/lib/cn";
import type { ProjectOut } from "@/api/types";

const DEFAULT_COLOR = "#66748c"; // moon-600

export function ProjectDot({ color, className }: { color?: string | null; className?: string }) {
  return (
    <span
      className={cn("inline-block h-2 w-2 shrink-0 rounded-full", className)}
      style={{ backgroundColor: color || DEFAULT_COLOR }}
    />
  );
}

export interface ProjectTagProps {
  project?: ProjectOut | null;
  /** Show a neutral "No project" when unset instead of nothing. */
  showNone?: boolean;
  className?: string;
}

/** Colored dot + project name — the project lens marker used on cards/rows. */
export function ProjectTag({ project, showNone, className }: ProjectTagProps) {
  if (!project && !showNone) return null;
  return (
    <span className={cn("inline-flex items-center gap-1.5 text-xs text-moon-400", className)}>
      <ProjectDot color={project?.color} />
      <span className="truncate">{project?.name ?? "No project"}</span>
    </span>
  );
}
