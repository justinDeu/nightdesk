import { useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { CalendarClock, MoreHorizontal, Pencil, Play, Plus, Trash2, Zap } from "lucide-react";
import { Page } from "@/components/Page";
import { Button } from "@/ui/Button";
import { Dialog } from "@/ui/Dialog";
import { EmptyState } from "@/ui/EmptyState";
import { Tooltip } from "@/ui/Tooltip";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/ui/DropdownMenu";
import { useCronJobs, cronApi } from "@/api/cron";
import { qk } from "@/api";
import type { CronJobOut } from "@/api/types";
import { toast } from "@/ui/Toast";
import { absoluteTime, relativeTime } from "@/lib/time";
import { cn } from "@/lib/cn";
import { CronEditorDialog } from "./CronEditorDialog";

export function ScheduledPage() {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const jobsQ = useCronJobs();
  const jobs = jobsQ.data ?? [];

  const [editing, setEditing] = useState<CronJobOut | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [deleting, setDeleting] = useState<CronJobOut | null>(null);

  const invalidate = () => qc.invalidateQueries({ queryKey: qk.cron.all });

  function openNew() {
    setEditing(null);
    setEditorOpen(true);
  }
  function openEdit(job: CronJobOut) {
    setEditing(job);
    setEditorOpen(true);
  }

  async function run(label: string, fn: () => Promise<unknown>, ok: string) {
    try {
      await fn();
      invalidate();
      toast.success(ok);
    } catch (err) {
      toast.error(`${label} failed`, { error: err });
    }
  }

  async function toggleEnabled(job: CronJobOut) {
    await run(
      "Toggle",
      () => (job.enabled ? cronApi.disable(job.id) : cronApi.enable(job.id)),
      job.enabled ? "Schedule paused" : "Schedule enabled",
    );
  }

  async function fireNow(job: CronJobOut, andRun: boolean) {
    try {
      const ticket = andRun
        ? await cronApi.fireNowAndRun(job.id)
        : await cronApi.fireNow(job.id);
      invalidate();
      qc.invalidateQueries({ queryKey: qk.tickets.all });
      toast.success(andRun ? "Ticket minted and dispatched" : "Ticket minted", {
        action: {
          label: "Open",
          onClick: () => navigate({ to: "/tickets/$id", params: { id: ticket.id } }),
        },
      });
    } catch (err) {
      toast.error("Fire failed", { error: err });
    }
  }

  async function confirmDelete() {
    if (!deleting) return;
    const job = deleting;
    await run("Delete", () => cronApi.remove(job.id), "Schedule deleted");
    setDeleting(null);
  }

  return (
    <Page
      bleed
      title="Scheduled"
      subtitle="Cron jobs that mint tickets on a recurring schedule."
      actions={
        <Button variant="primary" leadingIcon={<Plus size={15} />} onClick={openNew}>
          New schedule
        </Button>
      }
    >
      {jobsQ.isLoading ? (
        <div className="py-20 text-center text-sm text-moon-600">Loading schedules…</div>
      ) : jobs.length === 0 ? (
        <div className="mx-auto max-w-2xl">
          <EmptyState
            icon={<CalendarClock size={18} />}
            title="No scheduled jobs"
            description="Use New schedule to create a cron job that mints tickets automatically. You get a live preview of the next fire times as you type the expression."
          />
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
          {jobs.map((job) => (
            <JobCard
              key={job.id}
              job={job}
              onEdit={() => openEdit(job)}
              onToggle={() => toggleEnabled(job)}
              onFire={(andRun) => fireNow(job, andRun)}
              onDelete={() => setDeleting(job)}
              onOpenTicket={(tid) => navigate({ to: "/tickets/$id", params: { id: tid } })}
            />
          ))}
        </div>
      )}

      <CronEditorDialog open={editorOpen} job={editing} onOpenChange={setEditorOpen} />

      <Dialog
        open={deleting != null}
        onOpenChange={(o) => !o && setDeleting(null)}
        size="sm"
        title="Delete schedule"
        description="The cron job stops firing. Tickets it already created stay put."
        footer={
          <>
            <Button variant="ghost" onClick={() => setDeleting(null)}>
              Cancel
            </Button>
            <Button variant="danger" leadingIcon={<Trash2 size={14} />} onClick={confirmDelete}>
              Delete
            </Button>
          </>
        }
      >
        <p className="truncate rounded-control border border-ink-700 bg-ink-950 px-3 py-2 font-mono text-[13px] text-moon-100">
          {deleting?.title}
        </p>
      </Dialog>
    </Page>
  );
}

function JobCard({
  job,
  onEdit,
  onToggle,
  onFire,
  onDelete,
  onOpenTicket,
}: {
  job: CronJobOut;
  onEdit: () => void;
  onToggle: () => void;
  onFire: (andRun: boolean) => void;
  onDelete: () => void;
  onOpenTicket: (tid: string) => void;
}) {
  const muted = !job.enabled;
  return (
    <div
      className={cn(
        "rounded-card border border-ink-700/50 bg-ink-900 p-4 shadow-[var(--shadow-raised)] transition-opacity",
        muted && "opacity-60",
      )}
    >
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="truncate font-display text-sm font-semibold text-moon-100">
              {job.title}
            </h3>
            {muted && (
              <span className="rounded-full border border-ink-700 bg-ink-800 px-1.5 py-0.5 text-[10px] font-medium text-moon-600">
                Paused
              </span>
            )}
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[11px] text-moon-600">
            <span className="rounded border border-ink-700 bg-ink-800 px-1.5 py-0.5 text-moon-400">
              {job.schedule}
            </span>
            <span>{job.timezone}</span>
          </div>
        </div>

        <Tooltip content={job.enabled ? "Enabled — click to pause" : "Paused — click to enable"}>
          <button
            onClick={onToggle}
            aria-label={job.enabled ? "Pause schedule" : "Enable schedule"}
            className={cn(
              "inline-flex h-5 w-9 shrink-0 items-center rounded-full border px-0.5 transition-colors",
              job.enabled ? "border-lamp/40 bg-lamp/25" : "border-ink-700 bg-ink-800",
            )}
          >
            <span
              className={cn(
                "h-3.5 w-3.5 rounded-full transition-transform",
                job.enabled ? "translate-x-4 bg-lamp" : "translate-x-0 bg-moon-600",
              )}
            />
          </button>
        </Tooltip>
      </div>

      {job.prompt && (
        <p className="mt-2.5 line-clamp-2 text-xs leading-relaxed text-moon-400">{job.prompt}</p>
      )}

      <div className="mt-3 grid grid-cols-2 gap-2 border-t border-ink-700 pt-2.5 text-[11px]">
        <div>
          <div className="text-moon-600">Next fire</div>
          <div className="text-moon-100">
            {job.next_fire_at ? (
              <Tooltip content={absoluteTime(job.next_fire_at)}>
                <span>{relativeTime(job.next_fire_at)}</span>
              </Tooltip>
            ) : (
              <span className="text-moon-600">{job.enabled ? "—" : "paused"}</span>
            )}
          </div>
        </div>
        <div>
          <div className="text-moon-600">Last fire</div>
          <div className="text-moon-100">
            {job.last_fire_at ? (
              job.last_ticket_id ? (
                <button
                  onClick={() => onOpenTicket(job.last_ticket_id as string)}
                  className="text-moon-100 hover:text-lamp"
                >
                  {relativeTime(job.last_fire_at)}
                </button>
              ) : (
                <span>{relativeTime(job.last_fire_at)}</span>
              )
            ) : (
              <span className="text-moon-600">never</span>
            )}
          </div>
        </div>
      </div>

      <div className="mt-3 flex items-center gap-2">
        <Button size="sm" variant="subtle" leadingIcon={<Play size={12} />} onClick={() => onFire(false)}>
          Fire now
        </Button>
        <Button size="sm" variant="ghost" leadingIcon={<Zap size={12} />} onClick={() => onFire(true)}>
          Fire + run
        </Button>
        <div className="flex-1" />
        <Tooltip content="Edit">
          <button
            onClick={onEdit}
            aria-label="Edit schedule"
            className="rounded-control p-1.5 text-moon-400 hover:bg-ink-800 hover:text-moon-100"
          >
            <Pencil size={14} />
          </button>
        </Tooltip>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              aria-label="More actions"
              className="rounded-control p-1.5 text-moon-400 hover:bg-ink-800 hover:text-moon-100"
            >
              <MoreHorizontal size={14} />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem icon={<Pencil size={13} />} onSelect={onEdit}>
              Edit
            </DropdownMenuItem>
            <DropdownMenuItem icon={<Trash2 size={13} />} danger onSelect={onDelete}>
              Delete
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  );
}
