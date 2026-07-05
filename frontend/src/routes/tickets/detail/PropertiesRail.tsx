import { useState } from "react";
import type { ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { FolderGit2, GitBranch, Plus, X } from "lucide-react";
import { qk } from "@/api";
import { ticketsApi } from "@/api/tickets";
import { labelsApi } from "@/api/labels";
import { useProjects } from "@/api/projects";
import { useProfiles } from "@/api/profiles";
import { useLabels } from "@/api/labels";
import { useTicketDependencies } from "@/api/tickets";
import type { TicketOut } from "@/api/types";
import { Badge } from "@/ui/Badge";
import { Button } from "@/ui/Button";
import { Input } from "@/ui/Input";
import { StatusPill } from "@/ui/StatusPill";
import { Tooltip } from "@/ui/Tooltip";
import { toast } from "@/ui/Toast";
import { ApiError } from "@/api/client";
import {
  PriorityPicker,
  ProjectPicker,
  ProfilePicker,
  LabelPicker,
} from "@/components/PropertyPickers";
import { EffectiveConfigCard } from "@/components/EffectiveConfigCard";
import { useTicketActions } from "@/lib/ticketActions";
import { ticketStatusKind } from "@/lib/status";
import { relativeTime } from "@/lib/time";
import { cn } from "@/lib/cn";

/** Compact, always-visible metadata rail. Every property is editable in place;
 *  nothing here requires scrolling past content to reach. */
export function PropertiesRail({ ticket, className }: { ticket: TicketOut; className?: string }) {
  const qc = useQueryClient();
  const actions = useTicketActions();
  const projects = useProjects();
  const profiles = useProfiles();
  const labels = useLabels();
  const deps = useTicketDependencies(ticket.id);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: qk.tickets.detail(ticket.id) });
    qc.invalidateQueries({ queryKey: qk.tickets.all });
  };

  return (
    <div className={cn("space-y-4", className)}>
      <EffectiveConfigCard ticketId={ticket.id} />

      <Row label="Priority">
        <PriorityPicker value={ticket.priority} onChange={(v) => actions.setPriority(ticket, v)} />
      </Row>
      <Row label="Project">
        <ProjectPicker
          value={ticket.project_id}
          projects={projects.data ?? []}
          onChange={(pid) => actions.setProject(ticket, pid)}
        />
      </Row>
      <Row label="Profile">
        <ProfilePicker
          value={ticket.profile_id}
          profiles={profiles.data ?? []}
          onChange={(pid) =>
            ticketsApi
              .setProfile(ticket.id, pid)
              .then(invalidate)
              .catch(() => toast.error("Could not set profile"))
          }
        />
      </Row>
      <Row label="Labels" align="start">
        <LabelPicker
          value={ticket.labels.map((l) => l.id)}
          labels={labels.data ?? []}
          onChange={(ids) =>
            labelsApi
              .setTicketLabels(ticket.id, ids)
              .then(invalidate)
              .catch(() => toast.error("Could not update labels"))
          }
        >
          <div className="flex flex-wrap gap-1">
            {ticket.labels.length === 0 ? (
              <span className="text-xs text-moon-600">Add labels</span>
            ) : (
              ticket.labels.map((l) => (
                <span
                  key={l.id}
                  className="rounded-full px-1.5 py-0.5 text-[10px] font-medium text-ink-950"
                  style={{ backgroundColor: l.color }}
                >
                  {l.name}
                </span>
              ))
            )}
          </div>
        </LabelPicker>
      </Row>

      <Section title="Workspaces">
        <Workspaces ticket={ticket} />
      </Section>
      <Section title="Additional directories">
        <AdditionalDirs ticket={ticket} onChange={invalidate} />
      </Section>
      <Section title="Dependencies">
        <Dependencies ticket={ticket} deps={deps.data ?? []} onChange={() => deps.refetch()} />
      </Section>

      <div className="border-t border-ink-700/60 pt-3 text-[11px] text-moon-600">
        <div>created {relativeTime(ticket.created_at)}</div>
        <div>updated {relativeTime(ticket.updated_at)}</div>
        <div className="mt-1 font-mono text-moon-600/70">{ticket.id}</div>
      </div>
    </div>
  );
}

function Row({
  label,
  children,
  align = "center",
}: {
  label: string;
  children: ReactNode;
  align?: "center" | "start";
}) {
  return (
    <div className={cn("grid grid-cols-[72px_1fr] gap-2", align === "center" ? "items-center" : "items-start")}>
      <span className="pt-0.5 text-[11px] font-medium uppercase tracking-wide text-moon-600">
        {label}
      </span>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="border-t border-ink-700/60 pt-3">
      <div className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-moon-600">
        {title}
      </div>
      {children}
    </div>
  );
}

function Workspaces({ ticket }: { ticket: TicketOut }) {
  if (ticket.workspaces.length === 0) {
    return <p className="text-xs text-moon-600">No workspace configured.</p>;
  }
  return (
    <div className="space-y-2">
      {ticket.workspaces.map((w) => (
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

function AdditionalDirs({ ticket, onChange }: { ticket: TicketOut; onChange: () => void }) {
  const [adding, setAdding] = useState(false);
  const [path, setPath] = useState("");
  const [mode, setMode] = useState<"rw" | "ro">("rw");

  const add = async () => {
    if (!path.trim()) return;
    try {
      await ticketsApi.addAdditionalDir(ticket.id, { path: path.trim(), mode });
      setPath("");
      setAdding(false);
      onChange();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not add directory");
    }
  };
  const remove = async (p: string) => {
    try {
      await ticketsApi.removeAdditionalDir(ticket.id, p);
      onChange();
    } catch {
      toast.error("Could not remove directory");
    }
  };

  return (
    <div className="space-y-1.5">
      {ticket.additional_dirs.length === 0 && !adding && (
        <p className="text-xs text-moon-600">None.</p>
      )}
      {ticket.additional_dirs.map((d) => (
        <div
          key={d.path}
          className="flex items-center gap-2 rounded-control border border-ink-700 bg-ink-900 px-2 py-1.5"
        >
          <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-moon-100">{d.path}</span>
          <span className="text-[10px] text-moon-600">{d.mode ?? "rw"}</span>
          <button
            onClick={() => remove(d.path)}
            className="text-moon-600 hover:text-failed"
            aria-label="Remove directory"
          >
            <X size={12} />
          </button>
        </div>
      ))}
      {adding ? (
        <div className="space-y-1.5">
          <Input
            mono
            value={path}
            onChange={(e) => setPath(e.target.value)}
            placeholder="/path/to/dir…"
            autoFocus
            autoComplete="off"
            spellCheck={false}
          />
          <div className="flex items-center gap-2">
            <select
              value={mode}
              onChange={(e) => setMode(e.target.value as "rw" | "ro")}
              className="h-7 rounded-control border border-ink-700 bg-ink-950 px-2 text-xs text-moon-100"
            >
              <option value="rw">read-write</option>
              <option value="ro">read-only</option>
            </select>
            <Button size="sm" variant="primary" onClick={add}>
              Add
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setAdding(false)}>
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <button
          onClick={() => setAdding(true)}
          className="inline-flex items-center gap-1 text-xs text-moon-400 hover:text-moon-100"
        >
          <Plus size={12} /> Add directory
        </button>
      )}
    </div>
  );
}

function Dependencies({
  ticket,
  deps,
  onChange,
}: {
  ticket: TicketOut;
  deps: TicketOut["dependencies"];
  onChange: () => void;
}) {
  const remove = async (dependsOnId: string) => {
    try {
      await ticketsApi.removeDependency(ticket.id, dependsOnId);
      onChange();
    } catch {
      toast.error("Could not remove dependency");
    }
  };

  if (deps.length === 0) {
    return <p className="text-xs text-moon-600">No dependencies.</p>;
  }
  return (
    <div className="space-y-1.5">
      {deps.map((d) => (
        <div
          key={d.id}
          className="flex items-center gap-2 rounded-control border border-ink-700 bg-ink-900 px-2 py-1.5"
        >
          <StatusPill status={ticketStatusKind(d.depends_on_status)} />
          <Link
            to="/tickets/$id"
            params={{ id: d.depends_on_id }}
            className="min-w-0 flex-1 truncate text-xs text-moon-100 hover:text-lamp"
          >
            {d.depends_on_title}
          </Link>
          <button
            onClick={() => remove(d.depends_on_id)}
            className="text-moon-600 hover:text-failed"
            aria-label="Remove dependency"
          >
            <X size={12} />
          </button>
        </div>
      ))}
    </div>
  );
}
