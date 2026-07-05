import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Send } from "lucide-react";
import { Button } from "@/ui/Button";
import { Input, Field } from "@/ui/Input";
import { Spinner } from "@/ui/Spinner";
import { toast } from "@/ui/Toast";
import { ApiError } from "@/api/client";
import { configApi, useConfig } from "@/api/config";
import { notificationsApi } from "@/api/notificationsApi";
import { qk } from "@/api/keys";
import { SectionHeader, SettingsCard, SettingsSplit, AsidePanel } from "./parts/SettingsSection";
import { SaveBar, useEditableForm } from "./parts/SaveBar";

interface NotifyForm {
  notify_webhook_url: string;
}

export function NotificationsSection() {
  const qc = useQueryClient();
  const config = useConfig();
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const seed = config.data ? { notify_webhook_url: config.data.notify_webhook_url ?? "" } : undefined;
  const { form, setForm, dirty, discard, commit } = useEditableForm<NotifyForm, NotifyForm>(
    seed,
    (s) => s,
    config.data ? `${config.data.notify_webhook_url ?? ""}` : "loading",
  );

  if (config.isLoading || !form) {
    return (
      <div className="flex items-center gap-2 text-sm text-moon-400">
        <Spinner /> Loading…
      </div>
    );
  }
  if (config.isError) return <p className="text-sm text-failed">Could not load config.</p>;

  const url = form.notify_webhook_url.trim();
  const validUrl = /^https?:\/\//.test(url);

  async function save() {
    if (!form) return;
    setSaving(true);
    setError(null);
    try {
      await configApi.update({ notify_webhook_url: form.notify_webhook_url.trim() });
      await qc.invalidateQueries({ queryKey: qk.config });
      commit();
      toast.success("Webhook saved");
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Could not save";
      setError(msg);
      toast.error(msg);
    } finally {
      setSaving(false);
    }
  }

  async function sendTest() {
    setTesting(true);
    try {
      await notificationsApi.test(url);
      toast.success("Test payload sent", { description: "Check the destination for a synthetic run-completion event." });
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Test failed");
    } finally {
      setTesting(false);
    }
  }

  return (
    <div>
      <SectionHeader
        title="Notifications"
        description="Fire a webhook when a run finishes. The payload is a JSON run summary."
      />

      <SettingsSplit
        aside={
          <AsidePanel title="Payload shape">
            <p>
              Each finished run POSTs a JSON summary. Point it at Slack, Discord, a serverless
              function, or your own endpoint.
            </p>
            <pre className="overflow-x-auto rounded-control border border-ink-700 bg-ink-950/70 p-2.5 font-mono text-[11px] leading-relaxed text-moon-100">
{`{
  "event": "run.completed",
  "ticket_id": "…",
  "title": "…",
  "outcome": "success",
  "duration_seconds": 812,
  "cost_usd": 0.42,
  "tokens": 1284000
}`}
            </pre>
            <p className="text-moon-600">
              Delivery is best-effort and fire-and-forget — a failing endpoint never blocks a run.
            </p>
          </AsidePanel>
        }
      >
        <SettingsCard title="Completion webhook">
          <div className="space-y-4">
            <Field
              label="Webhook URL"
              hint="Any http(s) endpoint. Leave blank to disable notifications."
              error={url && !validUrl ? "Must start with http:// or https://" : undefined}
            >
              <Input
                mono
                type="url"
                placeholder="https://hooks.example.com/nightdesk"
                value={form.notify_webhook_url}
                invalid={!!url && !validUrl}
                onChange={(e) => setForm((f) => ({ ...f, notify_webhook_url: e.target.value }))}
              />
            </Field>
            <div className="flex items-center gap-3">
              <Button
                variant="ghost"
                leadingIcon={testing ? <Spinner size={14} /> : <Send size={14} />}
                disabled={!validUrl || testing}
                onClick={sendTest}
              >
                Send test
              </Button>
              <span className="text-xs text-moon-600">
                Sends a synthetic run-completion payload to the URL above (unsaved value is used).
              </span>
            </div>
          </div>
        </SettingsCard>
      </SettingsSplit>

      <SaveBar dirty={dirty} saving={saving} onSave={save} onDiscard={discard} error={error} />
    </div>
  );
}
