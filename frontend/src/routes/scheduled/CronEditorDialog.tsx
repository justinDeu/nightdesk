import { useEffect, useId, useMemo, useState } from "react";
import { CalendarClock, Loader2 } from "lucide-react";
import { Dialog } from "@/ui/Dialog";
import { Button } from "@/ui/Button";
import { Input, Textarea, Field } from "@/ui/Input";
import { Select } from "@/ui/Select";
import { Switch } from "@/ui/Switch";
import { PathInput } from "@/components/PathInput";
import { ProfilePicker } from "@/components/PropertyPickers";
import { useProfiles } from "@/api/profiles";
import { useProjects } from "@/api/projects";
import { cronApi } from "@/api/cron";
import { useCronPreview } from "@/api/diagnostics";
import { qk, ApiError } from "@/api";
import { useQueryClient } from "@tanstack/react-query";
import type { CronJobCreate, CronJobOut, CronWorkspaceMode } from "@/api/types";
import { toast } from "@/ui/Toast";
import { absoluteTime, relativeTime } from "@/lib/time";
import { cn } from "@/lib/cn";

const PRESETS: { label: string; expr: string }[] = [
  { label: "Every hour", expr: "0 * * * *" },
  { label: "Daily 9am", expr: "0 9 * * *" },
  { label: "Weekdays 8am", expr: "0 8 * * 1-5" },
  { label: "Weekly Mon", expr: "0 9 * * 1" },
];

function timezones(): string[] {
  const anyIntl = Intl as unknown as { supportedValuesOf?: (k: string) => string[] };
  if (typeof anyIntl.supportedValuesOf === "function") {
    try {
      return anyIntl.supportedValuesOf("timeZone");
    } catch {
      /* fall through */
    }
  }
  return ["UTC", "America/New_York", "America/Los_Angeles", "Europe/London", "Asia/Tokyo"];
}

const localTz = () => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
};

interface Draft {
  title: string;
  schedule: string;
  timezone: string;
  prompt: string;
  source_path: string;
  profile_id: string | null;
  workspace_mode: CronWorkspaceMode;
  force_run: boolean;
  overlap_policy: "skip_if_active" | "always";
  enabled: boolean;
}

function toDraft(job: CronJobOut | null): Draft {
  return {
    title: job?.title ?? "",
    schedule: job?.schedule ?? "0 9 * * *",
    timezone: job?.timezone ?? localTz(),
    prompt: job?.prompt ?? "",
    source_path: job?.source_path ?? "",
    profile_id: job?.profile_id ?? null,
    workspace_mode: (job?.workspace_mode as CronWorkspaceMode) ?? "directory",
    force_run: job?.force_run ?? false,
    overlap_policy: (job?.overlap_policy as Draft["overlap_policy"]) ?? "skip_if_active",
    enabled: job?.enabled ?? true,
  };
}

export function CronEditorDialog({
  open,
  job,
  onOpenChange,
}: {
  open: boolean;
  job: CronJobOut | null;
  onOpenChange: (open: boolean) => void;
}) {
  const qc = useQueryClient();
  const profiles = useProfiles();
  const projects = useProjects();
  const [draft, setDraft] = useState<Draft>(() => toDraft(job));
  const [saving, setSaving] = useState(false);

  // Re-seed the form whenever the dialog opens for a different job.
  useEffect(() => {
    if (open) setDraft(toDraft(job));
  }, [open, job]);

  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setDraft((d) => ({ ...d, [k]: v }));

  const preview = useCronPreview(draft.schedule, draft.timezone, 5);
  const tzList = useMemo(timezones, []);

  const valid =
    draft.title.trim().length > 0 &&
    draft.schedule.trim().length > 0 &&
    draft.source_path.trim().length > 0 &&
    !!draft.profile_id;

  async function save() {
    if (!valid || !draft.profile_id) return;
    setSaving(true);
    const body: CronJobCreate = {
      title: draft.title.trim(),
      schedule: draft.schedule.trim(),
      timezone: draft.timezone,
      prompt: draft.prompt,
      source_path: draft.source_path.trim(),
      profile_id: draft.profile_id,
      workspace_mode: draft.workspace_mode,
      force_run: draft.force_run,
      overlap_policy: draft.overlap_policy,
      enabled: draft.enabled,
    };
    try {
      if (job) await cronApi.update(job.id, body);
      else await cronApi.create(body);
      qc.invalidateQueries({ queryKey: qk.cron.all });
      toast.success(job ? "Schedule updated" : "Schedule created");
      onOpenChange(false);
    } catch (err) {
      toast.error("Could not save schedule", { error: err });
    } finally {
      setSaving(false);
    }
  }

  const fires = preview.data?.next_fire_times ?? [];

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      size="lg"
      title={job ? "Edit schedule" : "New schedule"}
      description="A cron job mints a ticket on a recurring schedule."
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button variant="primary" loading={saving} disabled={!valid} onClick={save}>
            {job ? "Save changes" : "Create schedule"}
          </Button>
        </>
      }
    >
      <div className="max-h-[70vh] space-y-4 overflow-y-auto pr-1">
        <Field label="Name" htmlFor="cron-name">
          <Input
            id="cron-name"
            autoFocus
            value={draft.title}
            onChange={(e) => set("title", e.target.value)}
            placeholder="Nightly dependency audit"
            autoComplete="off"
          />
        </Field>

        {/* Schedule + live preview */}
        <div className="rounded-card border border-ink-700 bg-ink-950/40 p-3">
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-[1fr_1fr]">
            <Field label="Schedule (cron)" hint="minute hour day month weekday" htmlFor="cron-schedule">
              <Input
                id="cron-schedule"
                mono
                value={draft.schedule}
                onChange={(e) => set("schedule", e.target.value)}
                placeholder="0 9 * * *"
                invalid={preview.isError}
                autoComplete="off"
                spellCheck={false}
                translate="no"
              />
            </Field>
            <Field label="Timezone" htmlFor="cron-timezone">
              <Select id="cron-timezone" value={draft.timezone} onChange={(e) => set("timezone", e.target.value)}>
                {tzList.map((tz) => (
                  <option key={tz} value={tz}>
                    {tz}
                  </option>
                ))}
              </Select>
            </Field>
          </div>

          <div className="mt-2 flex flex-wrap gap-1.5">
            {PRESETS.map((p) => (
              <button
                key={p.expr}
                onClick={() => set("schedule", p.expr)}
                className={cn(
                  "rounded-full border px-2 py-0.5 text-[11px] transition-colors",
                  draft.schedule === p.expr
                    ? "border-lamp/40 bg-lamp/10 text-lamp"
                    : "border-ink-700 bg-ink-800 text-moon-400 hover:text-moon-100",
                )}
              >
                {p.label}
              </button>
            ))}
          </div>

          <div className="mt-3 border-t border-ink-700 pt-2.5">
            <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-moon-600">
              <CalendarClock size={12} /> Next 5 fires
              {preview.isFetching && <Loader2 size={11} className="animate-spin text-moon-600" />}
            </div>
            {preview.isError ? (
              <p className="text-xs text-failed">
                {preview.error instanceof ApiError ? preview.error.message : "Invalid expression"}
              </p>
            ) : fires.length === 0 ? (
              <p className="text-xs text-moon-600">Enter a valid expression to preview fire times.</p>
            ) : (
              <ul className="space-y-1">
                {fires.map((f, i) => (
                  <li key={i} className="flex items-center gap-3 text-xs">
                    <span className="font-mono text-moon-100">{absoluteTime(f)}</span>
                    <span className="text-moon-600">{relativeTime(f)}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        <Field label="Prompt" hint="What the minted ticket asks the agent to do." htmlFor="cron-prompt">
          <Textarea
            id="cron-prompt"
            value={draft.prompt}
            onChange={(e) => set("prompt", e.target.value)}
            placeholder="Review dependencies for CVEs and open a summary…"
          />
        </Field>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="Working directory" htmlFor="cron-source-path">
            <PathInput
              id="cron-source-path"
              value={draft.source_path}
              onChange={(v) => set("source_path", v)}
              invalid={draft.source_path.trim().length === 0}
            />
          </Field>
          <Field label="Profile">
            <div className="flex h-9 items-center rounded-control border border-ink-700 bg-ink-950 px-2">
              <ProfilePicker
                value={draft.profile_id}
                profiles={profiles.data ?? []}
                onChange={(id) => set("profile_id", id)}
              />
            </div>
          </Field>
        </div>

        {projects.data && projects.data.length > 0 && (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[11px] text-moon-600">Fill from project:</span>
            {projects.data.map((p) => (
              <button
                key={p.id}
                onClick={() => set("source_path", p.source_path)}
                className="rounded-full border border-ink-700 bg-ink-800 px-2 py-0.5 text-[11px] text-moon-400 hover:text-moon-100"
              >
                {p.name}
              </button>
            ))}
          </div>
        )}

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="Workspace mode" htmlFor="cron-workspace-mode">
            <Select
              id="cron-workspace-mode"
              value={draft.workspace_mode}
              onChange={(e) => set("workspace_mode", e.target.value as CronWorkspaceMode)}
            >
              <option value="directory">Directory (copy)</option>
              <option value="in_place">In place</option>
            </Select>
          </Field>
          <Field label="If a run is already active" hint="Overlap policy" htmlFor="cron-overlap-policy">
            <Select
              id="cron-overlap-policy"
              value={draft.overlap_policy}
              onChange={(e) => set("overlap_policy", e.target.value as Draft["overlap_policy"])}
            >
              <option value="skip_if_active">Skip this fire</option>
              <option value="always">Queue anyway</option>
            </Select>
          </Field>
        </div>

        <div className="flex flex-wrap gap-4 border-t border-ink-700 pt-3">
          <Toggle
            checked={draft.enabled}
            onChange={(v) => set("enabled", v)}
            label="Enabled"
            hint="Off = paused; won't fire."
          />
          <Toggle
            checked={draft.force_run}
            onChange={(v) => set("force_run", v)}
            label="Force run"
            hint="Run immediately, ignoring the work-hours window."
          />
        </div>
      </div>
    </Dialog>
  );
}

function Toggle({
  checked,
  onChange,
  label,
  hint,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  hint?: string;
}) {
  const labelId = useId();
  return (
    <div className="flex items-start gap-2.5 text-left">
      <Switch checked={checked} onChange={onChange} aria-labelledby={labelId} className="mt-0.5" />
      <span className="cursor-pointer" onClick={() => onChange(!checked)}>
        <span id={labelId} className="block text-sm text-moon-100">
          {label}
        </span>
        {hint && <span className="block text-[11px] text-moon-600">{hint}</span>}
      </span>
    </div>
  );
}
