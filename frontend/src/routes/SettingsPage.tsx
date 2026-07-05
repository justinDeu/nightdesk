import type { ComponentType } from "react";
import { Link, useParams } from "@tanstack/react-router";
import {
  Bell,
  CalendarClock,
  FolderGit2,
  Hexagon,
  Layers,
  Settings2,
  Sparkles,
  Stethoscope,
  Tag,
  Terminal,
} from "lucide-react";
import { cn } from "@/lib/cn";
import { SchedulingSection } from "@/routes/settings/SchedulingSection";
import { ClaudeSection } from "@/routes/settings/ClaudeSection";
import { WorktreesSection } from "@/routes/settings/WorktreesSection";
import { NotificationsSection } from "@/routes/settings/NotificationsSection";
import { LabelsSection } from "@/routes/settings/LabelsSection";
import { ProjectsSection } from "@/routes/settings/ProjectsSection";
import { ProfilesSection } from "@/routes/settings/ProfilesSection";
import { ToolsetsSection } from "@/routes/settings/ToolsetsSection";
import { DiagnosticsSection } from "@/routes/settings/DiagnosticsSection";

interface SectionMeta {
  slug: string;
  label: string;
  icon: ComponentType<{ size?: number | string; className?: string }>;
  component: ComponentType;
}

const SECTIONS: SectionMeta[] = [
  { slug: "scheduling", label: "Scheduling", icon: CalendarClock, component: SchedulingSection },
  { slug: "claude", label: "Claude runtime", icon: Terminal, component: ClaudeSection },
  { slug: "worktrees", label: "Worktrees", icon: FolderGit2, component: WorktreesSection },
  { slug: "notifications", label: "Notifications", icon: Bell, component: NotificationsSection },
  { slug: "labels", label: "Labels", icon: Tag, component: LabelsSection },
  { slug: "projects", label: "Projects", icon: Layers, component: ProjectsSection },
  { slug: "profiles", label: "Profiles", icon: Hexagon, component: ProfilesSection },
  { slug: "toolsets", label: "Toolsets", icon: Sparkles, component: ToolsetsSection },
  { slug: "diagnostics", label: "Diagnostics", icon: Stethoscope, component: DiagnosticsSection },
];

export function SettingsPage() {
  let section: string | undefined;
  try {
    section = useParams({ from: "/app/settings/$section" }).section;
  } catch {
    section = undefined;
  }
  const active = SECTIONS.find((s) => s.slug === section) ?? SECTIONS[0];
  const ActiveComponent = active.component;

  return (
    <div className="flex min-h-[calc(100vh-3.5rem)] w-full">
      <nav
        aria-label="Settings sections"
        className="sticky top-0 h-[calc(100vh-3.5rem)] w-[210px] shrink-0 overflow-y-auto border-r border-ink-700/70 py-6 pl-5 pr-4"
      >
        <div className="mb-4 flex items-center gap-2 px-2.5 text-moon-400">
          <Settings2 size={15} />
          <h1 className="font-display text-sm font-semibold tracking-tight text-moon-100">
            Settings
          </h1>
        </div>
        <div className="space-y-0.5">
          {SECTIONS.map((s) => {
            const Icon = s.icon;
            const isActive = s.slug === active.slug;
            return (
              <Link
                key={s.slug}
                to="/settings/$section"
                params={{ section: s.slug }}
                className={cn(
                  "flex items-center gap-2.5 rounded-control px-2.5 py-1.5 text-sm transition-colors",
                  isActive
                    ? "bg-ink-800 text-moon-100"
                    : "text-moon-400 hover:bg-ink-800/60 hover:text-moon-100",
                )}
              >
                <Icon
                  size={15}
                  className={isActive ? "text-lamp" : "text-moon-600"}
                />
                {s.label}
              </Link>
            );
          })}
        </div>
      </nav>

      <div className="min-w-0 flex-1 px-8 py-8 2xl:px-10">
        <ActiveComponent />
      </div>
    </div>
  );
}
