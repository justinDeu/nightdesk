import { Link } from "@tanstack/react-router";
import { Hexagon, RefreshCw } from "lucide-react";
import { Button } from "@/ui/Button";
import { Spinner } from "@/ui/Spinner";
import { useDiagnostics } from "@/api/diagnosticsSettings";
import { SectionHeader, SettingsCard, InfoRow } from "./parts/SettingsSection";
import { StatusCard, type CheckTone } from "./parts/StatusCard";

function ccTone(status: string | null | undefined): CheckTone {
  if (status === "ok") return "ok";
  if (!status || status === "unknown") return "unknown";
  return "fail";
}

export function ClaudeSection() {
  const diag = useDiagnostics();

  return (
    <div>
      <SectionHeader
        title="Claude runtime"
        description="The Claude Code binary the worker drives, and where per-profile credentials live."
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

      {diag.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-moon-400">
          <Spinner /> Checking install…
        </div>
      ) : diag.isError ? (
        <p className="text-sm text-failed">Could not load runtime diagnostics.</p>
      ) : (
        <div className="space-y-5">
          <div className="grid gap-3 sm:grid-cols-2">
            <StatusCard
              tone={ccTone(diag.data!.cc_check_status)}
              title="Claude Code"
              value={diag.data!.cc_version ? `v${diag.data!.cc_version}` : "not detected"}
              detail={diag.data!.cc_check_message}
            />
            <StatusCard
              tone={diag.data!.cc_binary_path ? "ok" : "fail"}
              title="Binary path"
              value={diag.data!.cc_binary_path ?? "not found on PATH"}
            />
          </div>

          <div className="grid items-start gap-4 lg:grid-cols-2">
          <SettingsCard title="Install details">
            <InfoRow label="Version" mono>
              {diag.data!.cc_version ?? "—"}
            </InfoRow>
            <InfoRow label="Check status" mono>
              {diag.data!.cc_check_status ?? "unknown"}
            </InfoRow>
            <InfoRow label="Message">{diag.data!.cc_check_message ?? "—"}</InfoRow>
          </SettingsCard>

          <SettingsCard
            title="Credentials & binary overrides"
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
        </div>
      )}
    </div>
  );
}
