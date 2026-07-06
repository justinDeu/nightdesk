import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Hexagon, RefreshCw } from "lucide-react";
import { Button } from "@/ui/Button";
import { Input, Field } from "@/ui/Input";
import { Spinner } from "@/ui/Spinner";
import { toast } from "@/ui/Toast";
import { ApiError } from "@/api/client";
import { useDiagnostics } from "@/api/diagnosticsSettings";
import { configApi, useConfig } from "@/api/config";
import { qk } from "@/api/keys";
import type { ConfigOut } from "@/api/types";
import { SectionHeader, SettingsCard, InfoRow } from "./parts/SettingsSection";
import { StatusCard, type CheckTone } from "./parts/StatusCard";
import { SaveBar, useEditableForm } from "./parts/SaveBar";

function ccTone(status: string | null | undefined): CheckTone {
  if (status === "ok") return "ok";
  if (!status || status === "unknown") return "unknown";
  return "fail";
}

interface HarnessBinariesForm {
  claude_binary_path: string;
  opencode_binary_path: string;
}

export function ClaudeSection() {
  const diag = useDiagnostics();
  const config = useConfig();
  const qc = useQueryClient();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { form, setForm, dirty, discard, commit } = useEditableForm<ConfigOut, HarnessBinariesForm>(
    config.data,
    (c) => ({
      claude_binary_path: c.claude_binary_path ?? "",
      opencode_binary_path: c.opencode_binary_path ?? "",
    }),
    config.data ? `${config.data.claude_binary_path ?? ""}|${config.data.opencode_binary_path ?? ""}` : "loading",
  );

  async function save() {
    if (!form) return;
    setSaving(true);
    setError(null);
    try {
      await configApi.update({
        claude_binary_path: form.claude_binary_path.trim(),
        opencode_binary_path: form.opencode_binary_path.trim(),
      });
      await qc.invalidateQueries({ queryKey: qk.config });
      commit();
      toast.success("Harness settings saved");
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Could not save";
      setError(msg);
      toast.error(msg);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div>
      <SectionHeader
        title="Harnesses"
        description="The agent runtimes the worker can drive, and where each one's binary lives. Model choice, tools, and credentials are configured per profile."
        actions={
          <Button
            variant="ghost"
            size="sm"
            leadingIcon={diag.isFetching ? <Spinner size={13} /> : <RefreshCw size={13} />}
            onClick={() => diag.refetch()}
          >
            Recheck
          </Button>
        }
      />

      <div className="space-y-5">
        <div className="grid gap-3 sm:grid-cols-2">
          <StatusCard
            tone={diag.isLoading ? "unknown" : ccTone(diag.data?.cc_check_status)}
            title="Claude Code"
            value={diag.data?.cc_version ? `v${diag.data.cc_version}` : "not detected"}
            detail={diag.data?.cc_check_message}
          />
          <StatusCard
            tone={diag.isLoading ? "unknown" : diag.data?.cc_binary_path ? "ok" : "fail"}
            title="Binary path"
            value={diag.data?.cc_binary_path ?? "not found on PATH"}
          />
        </div>

        <div className="grid items-start gap-4 lg:grid-cols-2">
          <SettingsCard
            title="Claude Code"
            description="The claude binary the worker launches. Leave blank to use whatever's on PATH."
          >
            <div className="space-y-4">
              <Field label="Binary path override" hint="Global default. A profile's own override takes precedence.">
                <Input
                  mono
                  placeholder="/usr/local/bin/claude"
                  value={form?.claude_binary_path ?? ""}
                  onChange={(e) => setForm((f) => ({ ...f, claude_binary_path: e.target.value }))}
                  disabled={!form}
                />
              </Field>
              <InfoRow label="Version" mono>{diag.data?.cc_version ?? "—"}</InfoRow>
              <InfoRow label="Check status" mono>{diag.data?.cc_check_status ?? "unknown"}</InfoRow>
              <InfoRow label="Message">{diag.data?.cc_check_message ?? "—"}</InfoRow>
            </div>
          </SettingsCard>

          <SettingsCard
            title="opencode"
            description="The opencode binary the worker launches for opencode-backed profiles."
          >
            <div className="space-y-4">
              <Field
                label="Binary path override"
                hint="Empty auto-discovers from PATH, then ~/.opencode/bin."
              >
                <Input
                  mono
                  placeholder="auto-discover"
                  value={form?.opencode_binary_path ?? ""}
                  onChange={(e) => setForm((f) => ({ ...f, opencode_binary_path: e.target.value }))}
                  disabled={!form}
                />
              </Field>
              <p className="text-sm text-moon-400">
                Model, provider, and per-agent configuration live on the profile.
              </p>
              <Button asChild variant="ghost" size="sm" leadingIcon={<Hexagon size={14} />}>
                <Link to="/settings/$section" params={{ section: "profiles" }}>
                  Open Profiles
                </Link>
              </Button>
            </div>
          </SettingsCard>
        </div>

        <SettingsCard
          title="Credentials & per-profile overrides"
          description="Credential source (inherit / API key / auth token), a custom binary path, and the model are configured per profile — a run uses the profile attached to its ticket."
        >
          <div className="flex items-center justify-between gap-4">
            <p className="text-sm text-moon-400">
              Set these under a profile's identity and credentials groups.
            </p>
            <Button asChild variant="ghost" size="sm" leadingIcon={<Hexagon size={14} />}>
              <Link to="/settings/$section" params={{ section: "profiles" }}>
                Open Profiles
              </Link>
            </Button>
          </div>
        </SettingsCard>
      </div>

      <SaveBar dirty={dirty} saving={saving} onSave={save} onDiscard={discard} error={error} />
    </div>
  );
}
