import { RefreshCw } from "lucide-react";
import { Button } from "@/ui/Button";
import { Spinner } from "@/ui/Spinner";
import { useDiagnostics } from "@/api/diagnosticsSettings";
import { SectionHeader } from "./parts/SettingsSection";
import { StatusCard, type CheckTone } from "./parts/StatusCard";

function ccTone(status: string | null | undefined): CheckTone {
  if (status === "ok") return "ok";
  if (!status || status === "unknown") return "unknown";
  return "fail";
}

export function DiagnosticsSection() {
  const diag = useDiagnostics();

  return (
    <div>
      <SectionHeader
        title="Diagnostics"
        description="Runtime environment and sandbox toolchain health."
        actions={
          <Button
            variant="ghost"
            size="sm"
            leadingIcon={diag.isFetching ? <Spinner size={13} /> : <RefreshCw size={13} />}
            onClick={() => diag.refetch()}
          >
            Refresh
          </Button>
        }
      />

      {diag.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-moon-400">
          <Spinner /> Running checks…
        </div>
      ) : diag.isError ? (
        <p className="text-sm text-failed">Could not load diagnostics.</p>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          <StatusCard
            tone={ccTone(diag.data!.cc_check_status)}
            title="Claude Code"
            value={diag.data!.cc_version ? `v${diag.data!.cc_version}` : "not detected"}
            detail={diag.data!.cc_check_message}
          />
          <StatusCard
            tone={diag.data!.bwrap_version ? "ok" : "fail"}
            title="Sandbox (bubblewrap)"
            value={diag.data!.bwrap_version ?? "not installed"}
            detail={diag.data!.bwrap_version ? "Sandboxed runs available" : "Runs cannot be sandboxed"}
          />
          <StatusCard tone="ok" title="nightdesk" value={`v${diag.data!.nightdesk_version}`} />
          <StatusCard tone="ok" title="Python" value={diag.data!.python_version} />
          <StatusCard tone="unknown" title="Platform" value={diag.data!.platform} />
          <StatusCard tone="unknown" title="Kernel" value={diag.data!.kernel} />
        </div>
      )}
    </div>
  );
}
