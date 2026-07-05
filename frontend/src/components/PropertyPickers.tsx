import type { ReactNode } from "react";
import { Check } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/ui/DropdownMenu";
import { PriorityChip } from "./PriorityChip";
import { ProjectDot, ProjectTag } from "./ProjectDot";
import { PRIORITY_SCALE } from "@/lib/priority";
import type { LabelOut, ProfileOut, ProjectOut } from "@/api/types";
import { cn } from "@/lib/cn";

/** Optional controlled-open + trigger-styling props shared by every picker, so
 *  a keyboard-driven surface (e.g. inbox triage) can open them programmatically
 *  and match its own trigger chrome. Omit them for the default uncontrolled
 *  click-to-open behavior. */
interface PickerChrome {
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  /** Fully replaces the default trigger button styling when provided. */
  triggerClassName?: string;
}

function Trigger({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <DropdownMenuTrigger asChild>
      <button
        type="button"
        onClick={(e) => e.stopPropagation()}
        className={className ?? "inline-flex items-center rounded-control px-1 py-0.5 hover:bg-ink-800"}
      >
        {children}
      </button>
    </DropdownMenuTrigger>
  );
}

export function PriorityPicker({
  value,
  onChange,
  hideNone,
  open,
  onOpenChange,
  triggerClassName,
}: {
  value: number;
  onChange: (v: number) => void;
  hideNone?: boolean;
} & PickerChrome) {
  return (
    <DropdownMenu open={open} onOpenChange={onOpenChange}>
      <Trigger className={triggerClassName}>
        <PriorityChip value={value} hideNone={hideNone} />
      </Trigger>
      <DropdownMenuContent align="start">
        <DropdownMenuLabel>Priority</DropdownMenuLabel>
        {[...PRIORITY_SCALE].reverse().map((p) => (
          <DropdownMenuItem key={p.value} onSelect={() => onChange(p.value)}>
            <span className="flex flex-1 items-center gap-2">
              <PriorityChip value={p.value} />
              {p.label}
            </span>
            {value === p.value && <Check size={14} className="text-lamp" />}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function ProjectPicker({
  value,
  projects,
  onChange,
  open,
  onOpenChange,
  triggerClassName,
}: {
  value: string | null;
  projects: ProjectOut[];
  onChange: (id: string | null) => void;
} & PickerChrome) {
  const current = value ? projects.find((p) => p.id === value) : null;
  return (
    <DropdownMenu open={open} onOpenChange={onOpenChange}>
      <Trigger className={triggerClassName}>
        <ProjectTag project={current} showNone />
      </Trigger>
      <DropdownMenuContent align="start">
        <DropdownMenuLabel>Project</DropdownMenuLabel>
        <DropdownMenuItem onSelect={() => onChange(null)}>
          <span className="flex flex-1 items-center gap-2">
            <ProjectDot color={null} /> No project
          </span>
          {value === null && <Check size={14} className="text-lamp" />}
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        {projects.map((p) => (
          <DropdownMenuItem key={p.id} onSelect={() => onChange(p.id)}>
            <span className="flex flex-1 items-center gap-2">
              <ProjectDot color={p.color} /> {p.name}
            </span>
            {value === p.id && <Check size={14} className="text-lamp" />}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function ProfilePicker({
  value,
  profiles,
  onChange,
}: {
  value: string | null;
  profiles: ProfileOut[];
  onChange: (id: string) => void;
}) {
  const current = value ? profiles.find((p) => p.id === value) : null;
  return (
    <DropdownMenu>
      <Trigger>
        <span className="inline-flex items-center rounded-full border border-ink-700 bg-ink-800 px-2 py-0.5 text-[11px] text-moon-100">
          {current?.name ?? "Set profile"}
        </span>
      </Trigger>
      <DropdownMenuContent align="start">
        <DropdownMenuLabel>Profile</DropdownMenuLabel>
        {profiles.map((p) => (
          <DropdownMenuItem key={p.id} onSelect={() => onChange(p.id)}>
            <span className="flex-1">{p.name}</span>
            {value === p.id && <Check size={14} className="text-lamp" />}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export function LabelPicker({
  value,
  labels,
  onChange,
  children,
  open,
  onOpenChange,
  triggerClassName,
}: {
  value: string[];
  labels: LabelOut[];
  onChange: (ids: string[]) => void;
  children: ReactNode;
} & PickerChrome) {
  const toggle = (id: string) =>
    onChange(value.includes(id) ? value.filter((x) => x !== id) : [...value, id]);
  return (
    <DropdownMenu open={open} onOpenChange={onOpenChange}>
      <Trigger className={triggerClassName}>{children}</Trigger>
      <DropdownMenuContent align="start">
        <DropdownMenuLabel>Labels</DropdownMenuLabel>
        {labels.length === 0 && (
          <div className="px-2 py-1.5 text-xs text-moon-600">No labels defined</div>
        )}
        {labels.map((l) => {
          const on = value.includes(l.id);
          return (
            // keepOpen: toggling several labels in one visit shouldn't close the menu.
            <DropdownMenuItem key={l.id} keepOpen onSelect={() => toggle(l.id)}>
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
  );
}
