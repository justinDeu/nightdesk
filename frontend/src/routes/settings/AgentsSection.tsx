import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Field, Input } from "@/ui/Input";
import { Spinner } from "@/ui/Spinner";
import { toast, describeError } from "@/ui/Toast";
import { configApi, useConfig } from "@/api/config";
import { qk } from "@/api/keys";
import { SectionHeader, SettingsCard, SettingsSplit, AsidePanel } from "./parts/SettingsSection";
import { SaveBar, useEditableForm } from "./parts/SaveBar";

interface AgentsForm {
  session_idle_timeout_s: number;
  max_live_sessions: number;
  max_queued_turns: number;
  max_turn_seconds: number;
}

/** Global, live-evaluated knobs for resident interactive agents. Applied to
 *  every inheriting agent on the next reaper pass — no restart. */
export function AgentsSection() {
  const qc = useQueryClient();
  const config = useConfig();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const seed: AgentsForm | undefined = config.data
    ? {
        session_idle_timeout_s: config.data.session_idle_timeout_s,
        max_live_sessions: config.data.max_live_sessions,
        max_queued_turns: config.data.max_queued_turns,
        max_turn_seconds: config.data.max_turn_seconds,
      }
    : undefined;

  const key = config.data
    ? `${config.data.session_idle_timeout_s}:${config.data.max_live_sessions}:${config.data.max_queued_turns}:${config.data.max_turn_seconds}`
    : "loading";

  const { form, setForm, dirty, discard, commit } = useEditableForm<AgentsForm, AgentsForm>(
    seed,
    (s) => s,
    key,
  );

  if (config.isLoading || !form) {
    return (
      <div className="flex items-center gap-2 text-sm text-moon-400">
        <Spinner /> Loading settings…
      </div>
    );
  }
  if (config.isError) {
    return <p className="text-sm text-failed">Could not load config.</p>;
  }

  const num = (v: string) => Math.max(0, Number(v) || 0);

  async function save() {
    if (!form) return;
    setSaving(true);
    setError(null);
    try {
      await configApi.update({
        session_idle_timeout_s: form.session_idle_timeout_s,
        max_live_sessions: form.max_live_sessions,
        max_queued_turns: form.max_queued_turns,
        max_turn_seconds: form.max_turn_seconds,
      });
      await qc.invalidateQueries({ queryKey: qk.config });
      commit();
      toast.success("Agent settings saved");
    } catch (err) {
      setError(describeError(err));
      toast.error("Could not save", { error: err });
    } finally {
      setSaving(false);
    }
  }

  return (
    <div>
      <SectionHeader
        title="Agents"
        description="Global limits for resident interactive agents. Changes apply live on the next reaper pass — no restart."
      />

      <SettingsSplit
        aside={
          <AsidePanel title="How these apply">
            <p>
              Every agent that doesn&apos;t set its own idle-timeout override inherits{" "}
              <span className="text-moon-100">idle timeout</span>. Lowering it reaps already-warm
              inheriting agents on the next pass; agents with an explicit override are unaffected.
            </p>
            <p>
              An agent blocked on your input is never reaped or evicted — it stays warm and visible
              in the sidebar badge until you answer or end it.
            </p>
          </AsidePanel>
        }
      >
        <SettingsCard title="Lifetime & capacity">
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <Field
              label="Idle timeout (seconds)"
              hint="How long an agent stays warm with no activity before it goes cold. Default 300."
              htmlFor="agents-idle"
            >
              <Input
                id="agents-idle"
                type="number"
                min={1}
                value={form.session_idle_timeout_s}
                onChange={(e) => setForm((f) => ({ ...f, session_idle_timeout_s: num(e.target.value) }))}
              />
            </Field>
            <Field
              label="Max live agents"
              hint="Cap on simultaneously warm agents. At the cap, a wake queues for a free slot (LRU eviction)."
              htmlFor="agents-max-live"
            >
              <Input
                id="agents-max-live"
                type="number"
                min={1}
                value={form.max_live_sessions}
                onChange={(e) => setForm((f) => ({ ...f, max_live_sessions: num(e.target.value) }))}
              />
            </Field>
            <Field
              label="Max queued turns"
              hint="How many messages may wait in one agent's queue while it works."
              htmlFor="agents-max-queued"
            >
              <Input
                id="agents-max-queued"
                type="number"
                min={1}
                value={form.max_queued_turns}
                onChange={(e) => setForm((f) => ({ ...f, max_queued_turns: num(e.target.value) }))}
              />
            </Field>
            <Field
              label="Max turn seconds"
              hint="Watchdog: a single turn is interrupted after this long. 0 disables the watchdog."
              htmlFor="agents-max-turn"
            >
              <Input
                id="agents-max-turn"
                type="number"
                min={0}
                value={form.max_turn_seconds}
                onChange={(e) => setForm((f) => ({ ...f, max_turn_seconds: num(e.target.value) }))}
              />
            </Field>
          </div>
        </SettingsCard>
      </SettingsSplit>

      <SaveBar dirty={dirty} saving={saving} onSave={save} onDiscard={discard} error={error} />
    </div>
  );
}
