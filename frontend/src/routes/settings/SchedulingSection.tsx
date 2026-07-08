import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ArrowDown, ArrowUp, Clock, Plus, Trash2 } from "lucide-react";
import { Button } from "@/ui/Button";
import { IconButton } from "@/ui/IconButton";
import { Input, Field } from "@/ui/Input";
import { Select } from "@/ui/Select";
import { Spinner } from "@/ui/Spinner";
import { Badge } from "@/ui/Badge";
import { Tooltip } from "@/ui/Tooltip";
import { toast, describeError } from "@/ui/Toast";
import { configApi, useConfig, useWindows } from "@/api/config";
import { qk } from "@/api/keys";
import type { ScheduleWindowCreate } from "@/api/types";
import { SectionHeader, SettingsCard, SettingsSplit, AsidePanel } from "./parts/SettingsSection";
import { SaveBar, useEditableForm } from "./parts/SaveBar";
import {
  DAYS,
  EVERY_DAY,
  WEEKDAYS,
  WEEKENDS,
  browserTimezone,
  maskSummary,
  timezoneOptions,
} from "./parts/tz";
import { cn } from "@/lib/cn";

interface WindowDraft {
  label: string;
  day_mask: number;
  start: string;
  end: string;
  max_parallel: number;
}

interface SchedulingForm {
  max_parallel: number;
  schedule_timezone: string;
  windows: WindowDraft[];
}

export function SchedulingSection() {
  const qc = useQueryClient();
  const config = useConfig();
  const windows = useWindows();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const ready = config.data && windows.data;
  const seed = ready
    ? {
        max_parallel: config.data!.max_parallel,
        schedule_timezone: config.data!.schedule_timezone,
        windows: [...windows.data!]
          .sort((a, b) => a.position - b.position)
          .map((w) => ({
            label: w.label,
            day_mask: w.day_mask,
            start: w.start,
            end: w.end,
            max_parallel: w.max_parallel,
          })),
      }
    : undefined;

  const key = ready
    ? `${config.data!.schedule_timezone}:${config.data!.max_parallel}:${windows.data!
        .map((w) => `${w.id}-${w.position}`)
        .join(",")}`
    : "loading";

  const { form, setForm, dirty, discard, commit } = useEditableForm<SchedulingForm, SchedulingForm>(
    seed,
    (s) => s,
    key,
  );

  if (config.isLoading || windows.isLoading || !form) {
    return (
      <div className="flex items-center gap-2 text-sm text-moon-400">
        <Spinner /> Loading schedule…
      </div>
    );
  }
  if (config.isError) {
    return <p className="text-sm text-failed">Could not load config.</p>;
  }

  const browserTz = browserTimezone();
  const tzMismatch = form.schedule_timezone !== browserTz;
  const tzOptions = timezoneOptions(form.schedule_timezone, browserTz);

  function patchWindow(i: number, patch: Partial<WindowDraft>) {
    setForm((f) => ({
      ...f,
      windows: f.windows.map((w, idx) => (idx === i ? { ...w, ...patch } : w)),
    }));
  }
  function move(i: number, dir: -1 | 1) {
    setForm((f) => {
      const next = [...f.windows];
      const j = i + dir;
      if (j < 0 || j >= next.length) return f;
      [next[i], next[j]] = [next[j], next[i]];
      return { ...f, windows: next };
    });
  }
  function addWindow() {
    setForm((f) => ({
      ...f,
      windows: [
        ...f.windows,
        { label: "New window", day_mask: EVERY_DAY, start: "09:00", end: "17:00", max_parallel: f.max_parallel },
      ],
    }));
  }
  function removeWindow(i: number) {
    setForm((f) => ({ ...f, windows: f.windows.filter((_, idx) => idx !== i) }));
  }

  async function save() {
    if (!form) return;
    setSaving(true);
    setError(null);
    try {
      await configApi.update({
        max_parallel: form.max_parallel,
        schedule_timezone: form.schedule_timezone,
      });
      const payloadWindows: ScheduleWindowCreate[] = form.windows.map((w, i) => ({
        label: w.label,
        day_mask: w.day_mask,
        start: w.start,
        end: w.end,
        max_parallel: w.max_parallel,
        position: i,
      }));
      await configApi.replaceWindows({ timezone: form.schedule_timezone, windows: payloadWindows });
      await Promise.all([
        qc.invalidateQueries({ queryKey: qk.config }),
        qc.invalidateQueries({ queryKey: qk.windows }),
        qc.invalidateQueries({ queryKey: qk.worker }),
      ]);
      commit();
      toast.success("Schedule saved");
    } catch (err) {
      setError(describeError(err));
      toast.error("Could not save schedule", { error: err });
    } finally {
      setSaving(false);
    }
  }

  return (
    <div>
      <SectionHeader
        title="Scheduling"
        description="When the worker dispatches queued tickets, and how many run at once."
      />

      <SettingsSplit
        aside={
          <AsidePanel title="How dispatch resolves">
            <p>
              The worker checks these rules every cycle to decide how many queued tickets it may
              start.
            </p>
            <ol className="ml-4 list-decimal space-y-1.5 text-moon-400">
              <li>Find every window whose days and hours contain the current moment.</li>
              <li>
                The <span className="text-moon-100">topmost matching window</span> wins — its cap is
                the limit right now.
              </li>
              <li>
                With no window matching, only <span className="text-moon-100">run-now</span> tickets
                dispatch; the default cap applies to those.
              </li>
            </ol>
            <p className="text-moon-600">
              Times are wall-clock in the schedule timezone, evaluated on the worker&apos;s UTC
              clock — not the viewer&apos;s local time.
            </p>
          </AsidePanel>
        }
      >
      <div className="space-y-5">
        <SettingsCard title="Capacity & timezone">
          <div className="grid grid-cols-2 gap-4">
            <Field
              label="Default max parallel"
              hint="Fallback cap when no window matches a stricter value."
              htmlFor="sched-default-max-parallel"
            >
              <Input
                id="sched-default-max-parallel"
                type="number"
                min={0}
                value={form.max_parallel}
                onChange={(e) =>
                  setForm((f) => ({ ...f, max_parallel: Math.max(0, Number(e.target.value) || 0) }))
                }
              />
            </Field>
            <Field
              label="Schedule timezone"
              hint="Window times below are wall-clock in this zone."
              htmlFor="sched-timezone"
            >
              <Select
                id="sched-timezone"
                value={form.schedule_timezone}
                onChange={(e) => setForm((f) => ({ ...f, schedule_timezone: e.target.value }))}
              >
                {tzOptions.map((z) => (
                  <option key={z} value={z}>
                    {z}
                    {z === browserTz ? "  (this browser)" : ""}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
          {tzMismatch && (
            <p className="mt-3 text-xs text-moon-600">
              Your browser is in <span className="font-mono text-moon-400">{browserTz}</span>. Window
              times are evaluated in{" "}
              <span className="font-mono text-moon-400">{form.schedule_timezone}</span>, not your
              local time.
            </p>
          )}
        </SettingsCard>

        <SettingsCard
          title="Schedule windows"
          description="Each window sets a parallelism cap for its days and hours. When windows overlap, the topmost wins — reorder to set precedence. Outside every window, only run-now tickets dispatch."
          footer={
            <Button variant="ghost" size="sm" leadingIcon={<Plus size={14} />} onClick={addWindow}>
              Add window
            </Button>
          }
        >
          {form.windows.length === 0 ? (
            <p className="text-sm text-moon-400">
              No windows. With none defined, normal tickets never auto-dispatch — add one to open a
              run window.
            </p>
          ) : (
            <ul className="space-y-3">
              {form.windows.map((w, i) => (
                <WindowRow
                  key={i}
                  index={i}
                  count={form.windows.length}
                  window={w}
                  onChange={(patch) => patchWindow(i, patch)}
                  onMove={(dir) => move(i, dir)}
                  onRemove={() => removeWindow(i)}
                />
              ))}
            </ul>
          )}
        </SettingsCard>
      </div>
      </SettingsSplit>

      <SaveBar dirty={dirty} saving={saving} onSave={save} onDiscard={discard} error={error} />
    </div>
  );
}

function WindowRow({
  index,
  count,
  window: w,
  onChange,
  onMove,
  onRemove,
}: {
  index: number;
  count: number;
  window: WindowDraft;
  onChange: (patch: Partial<WindowDraft>) => void;
  onMove: (dir: -1 | 1) => void;
  onRemove: () => void;
}) {
  const allDay = w.start === w.end;
  return (
    <li className="rounded-card border border-ink-700 bg-ink-950/40 p-3">
      <div className="mb-2.5 flex items-center gap-2">
        <div className="flex flex-col">
          <IconButton
            label="Move up (higher precedence)"
            size="sm"
            icon={<ArrowUp size={13} />}
            disabled={index === 0}
            onClick={() => onMove(-1)}
            className="h-5"
          />
          <IconButton
            label="Move down"
            size="sm"
            icon={<ArrowDown size={13} />}
            disabled={index === count - 1}
            onClick={() => onMove(1)}
            className="h-5"
          />
        </div>
        <Input
          value={w.label}
          placeholder="Window name"
          onChange={(e) => onChange({ label: e.target.value })}
          className="h-8 flex-1 font-medium"
        />
        <Badge tone="neutral" mono>
          {maskSummary(w.day_mask)}
        </Badge>
        <IconButton
          label="Remove window"
          size="sm"
          icon={<Trash2 size={14} />}
          onClick={onRemove}
          className="hover:text-failed"
        />
      </div>

      <div className="flex flex-wrap gap-1 pl-8">
        {DAYS.map((d) => {
          const on = (w.day_mask & d.bit) !== 0;
          return (
            <Tooltip key={d.bit} content={d.label}>
              <button
                type="button"
                aria-pressed={on}
                aria-label={d.label}
                onClick={() => onChange({ day_mask: on ? w.day_mask & ~d.bit : w.day_mask | d.bit })}
                className={cn(
                  "rounded-control border px-2 py-0.5 text-xs transition-colors",
                  on
                    ? "border-lamp bg-lamp/15 text-lamp"
                    : "border-ink-700 text-moon-400 hover:text-moon-100",
                )}
              >
                {d.short}
              </button>
            </Tooltip>
          );
        })}
        <div className="ml-1 flex gap-1">
          <PresetChip label="All" active={w.day_mask === EVERY_DAY} onClick={() => onChange({ day_mask: EVERY_DAY })} />
          <PresetChip label="M–F" active={w.day_mask === WEEKDAYS} onClick={() => onChange({ day_mask: WEEKDAYS })} />
          <PresetChip label="S–S" active={w.day_mask === WEEKENDS} onClick={() => onChange({ day_mask: WEEKENDS })} />
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-end gap-3 pl-8">
        <Field label="Start" className="w-36" htmlFor={`win-${index}-start`}>
          <Input
            id={`win-${index}-start`}
            type="time"
            mono
            value={w.start}
            onChange={(e) => onChange({ start: e.target.value })}
            className="h-8 px-2"
          />
        </Field>
        <Field label="End" className="w-36" htmlFor={`win-${index}-end`}>
          <Input
            id={`win-${index}-end`}
            type="time"
            mono
            value={w.end}
            onChange={(e) => onChange({ end: e.target.value })}
            className="h-8 px-2"
          />
        </Field>
        <Field label="Max parallel" className="w-28" htmlFor={`win-${index}-max`}>
          <Input
            id={`win-${index}-max`}
            type="number"
            min={0}
            value={w.max_parallel}
            onChange={(e) => onChange({ max_parallel: Math.max(0, Number(e.target.value) || 0) })}
            className="h-8"
          />
        </Field>
        <span className="flex items-center gap-1.5 pb-2 text-xs text-moon-600">
          <Clock size={12} />
          {allDay ? "All day (start = end)" : `${w.start}–${w.end}`}
        </span>
      </div>
    </li>
  );
}

function PresetChip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "rounded-control border px-2 py-0.5 text-[11px] transition-colors",
        active ? "border-moon-400 text-moon-100" : "border-ink-700 text-moon-600 hover:text-moon-100",
      )}
    >
      {label}
    </button>
  );
}
