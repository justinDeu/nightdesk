import { useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { CheckCircle2, Hexagon, RefreshCw, XCircle } from "lucide-react";
import { Button } from "@/ui/Button";
import { Input, Field } from "@/ui/Input";
import { Spinner } from "@/ui/Spinner";
import { toast } from "@/ui/Toast";
import { ApiError } from "@/api/client";
import { useDiagnostics } from "@/api/diagnosticsSettings";
import { configApi, useConfig } from "@/api/config";
import { useBackends } from "@/api/backends";
import { qk } from "@/api/keys";
import type { BackendRuntimeOut, ConfigOut } from "@/api/types";
import { SectionHeader, SettingsCard, InfoRow } from "./parts/SettingsSection";
import { SaveBar, useEditableForm } from "./parts/SaveBar";

interface HarnessBinariesForm {
  claude_binary_path: string;
  opencode_binary_path: string;
}

/** Where a binary was looked for, spelled out so "not found" is never a
 *  dead end — the operator can see exactly what path to fix. */
function lookedAtLabel(runtime: BackendRuntimeOut): string {
  if (runtime.source === "override") return `the override path (${runtime.resolved_path})`;
  if (runtime.source === "path") return "PATH";
  return `the default location (${runtime.resolved_path})`;
}

/** The hard yes/no binary status for a harness. Replaces the old ambiguous
 *  "auto" placeholder — this is the ground truth a run would actually use,
 *  mirroring the worker's own resolution chain (see BackendOut.runtime). */
function HarnessRuntimeStatus({
  runtime,
  isLoading,
}: {
  runtime: BackendRuntimeOut | null | undefined;
  isLoading: boolean;
}) {
  if (isLoading) {
    return (
      <div className="flex items-center gap-2 rounded-card border border-ink-700 bg-ink-950/40 px-3.5 py-2.5 text-sm text-moon-400">
        <Spinner size={13} /> Checking binary…
      </div>
    );
  }
  if (!runtime) {
    return (
      <div className="rounded-card border border-ink-700 bg-ink-950/40 px-3.5 py-2.5 text-sm text-moon-400">
        Runtime status unavailable.
      </div>
    );
  }
  if (runtime.found) {
    return (
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-card border border-success/25 bg-success/8 px-3.5 py-2.5 text-sm text-success">
        <CheckCircle2 size={15} className="shrink-0" />
        <span className="font-medium">Installed</span>
        <span className="text-success/50">·</span>
        <span className="truncate font-mono text-[13px] text-success/90">{runtime.resolved_path}</span>
        {runtime.version && (
          <>
            <span className="text-success/50">·</span>
            <span className="shrink-0">v{runtime.version}</span>
          </>
        )}
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-1 rounded-card border border-failed/25 bg-failed/8 px-3.5 py-2.5 text-sm text-failed">
      <div className="flex items-center gap-2">
        <XCircle size={15} className="shrink-0" />
        <span className="font-medium">Not found</span>
      </div>
      <p className="pl-[23px] text-xs text-failed/80">Looked at {lookedAtLabel(runtime)}.</p>
    </div>
  );
}

/** One bordered group per harness — status, binary override, and every
 *  other setting for that harness live together instead of being split
 *  across cards. */
function HarnessGroup({
  title,
  description,
  runtime,
  runtimeLoading,
  children,
}: {
  title: string;
  description: string;
  runtime: BackendRuntimeOut | null | undefined;
  runtimeLoading: boolean;
  children: ReactNode;
}) {
  return (
    <SettingsCard title={title} description={description}>
      <div className="space-y-4">
        <HarnessRuntimeStatus runtime={runtime} isLoading={runtimeLoading} />
        {children}
      </div>
    </SettingsCard>
  );
}

export function ClaudeSection() {
  const diag = useDiagnostics();
  const config = useConfig();
  const backends = useBackends();
  const qc = useQueryClient();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const claudeBackend = backends.data?.find((b) => b.code === "claude_sdk");
  const opencodeBackend = backends.data?.find((b) => b.code === "opencode");

  const { form, setForm, dirty, discard, commit } = useEditableForm<ConfigOut, HarnessBinariesForm>(
    config.data,
    (c) => ({
      claude_binary_path: c.claude_binary_path ?? "",
      opencode_binary_path: c.opencode_binary_path ?? "",
    }),
    config.data ? `${config.data.claude_binary_path ?? ""}|${config.data.opencode_binary_path ?? ""}` : "loading",
  );

  async function recheck() {
    await Promise.all([diag.refetch(), qc.invalidateQueries({ queryKey: qk.backends })]);
  }

  async function save() {
    if (!form) return;
    setSaving(true);
    setError(null);
    try {
      await configApi.update({
        claude_binary_path: form.claude_binary_path.trim(),
        opencode_binary_path: form.opencode_binary_path.trim(),
      });
      await Promise.all([
        qc.invalidateQueries({ queryKey: qk.config }),
        qc.invalidateQueries({ queryKey: qk.backends }),
        diag.refetch(),
      ]);
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

  const isRuntimeLoading = backends.isLoading;

  return (
    <div>
      <SectionHeader
        title="Harnesses"
        description="The agent runtimes the worker can drive, and where each one's binary lives. Model choice, tools, and credentials are configured per profile."
        actions={
          <Button
            variant="ghost"
            size="sm"
            leadingIcon={diag.isFetching || backends.isFetching ? <Spinner size={13} /> : <RefreshCw size={13} />}
            onClick={recheck}
          >
            Recheck
          </Button>
        }
      />

      <div className="space-y-5">
        <HarnessGroup
          title="Claude Code"
          description="The claude binary the worker launches. Leave blank to use whatever's on PATH."
          runtime={claudeBackend?.runtime}
          runtimeLoading={isRuntimeLoading}
        >
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
          <div className="flex items-center justify-between gap-4 border-t border-ink-700/70 pt-4">
            <p className="text-sm text-moon-400">
              Credential source (inherit / API key / auth token), a custom binary path, and the
              model are configured per profile — a run uses the profile attached to its ticket.
            </p>
            <Button asChild variant="ghost" size="sm" leadingIcon={<Hexagon size={14} />}>
              <Link to="/settings/$section" params={{ section: "profiles" }}>
                Open Profiles
              </Link>
            </Button>
          </div>
        </HarnessGroup>

        <HarnessGroup
          title="opencode"
          description="The opencode binary the worker launches for opencode-backed profiles."
          runtime={opencodeBackend?.runtime}
          runtimeLoading={isRuntimeLoading}
        >
          <Field label="Binary path override" hint="Empty auto-discovers from PATH, then ~/.opencode/bin.">
            <Input
              mono
              placeholder="auto-discover"
              value={form?.opencode_binary_path ?? ""}
              onChange={(e) => setForm((f) => ({ ...f, opencode_binary_path: e.target.value }))}
              disabled={!form}
            />
          </Field>
          <div className="flex items-center justify-between gap-4">
            <p className="text-sm text-moon-400">
              Model, provider, and per-agent configuration live on the profile.
            </p>
            <Button asChild variant="ghost" size="sm" leadingIcon={<Hexagon size={14} />}>
              <Link to="/settings/$section" params={{ section: "profiles" }}>
                Open Profiles
              </Link>
            </Button>
          </div>
        </HarnessGroup>
      </div>

      <SaveBar dirty={dirty} saving={saving} onSave={save} onDiscard={discard} error={error} />
    </div>
  );
}
