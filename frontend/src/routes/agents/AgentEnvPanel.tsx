import { useEffect, useState } from "react";
import { Eye, EyeOff, Plus, RotateCw, Save, X } from "lucide-react";
import { Button } from "@/ui/Button";
import { Input } from "@/ui/Input";
import { IconButton } from "@/ui/IconButton";
import { Tooltip } from "@/ui/Tooltip";
import { toast, describeError } from "@/ui/Toast";
import { confirm } from "@/ui/confirm";
import { ApiError } from "@/api/client";
import { usePutEnv, useRestartRuntime } from "@/api/agents";
import { cn } from "@/lib/cn";
import type { AgentEnvEntryIn, AgentEnvEntryOut, AgentLiveness } from "@/api/types";

interface Row {
  key: string;
  /** Editable value. For a previously-set secret, empty means "keep the stored
   *  cipher"; a non-empty value replaces it. */
  value: string;
  secret: boolean;
  /** A secret that already has a cipher stored server-side. */
  hadCipher: boolean;
  reveal: boolean;
}

function seed(env: AgentEnvEntryOut[]): Row[] {
  return env.map((e) => ({
    key: e.key,
    value: e.secret ? "" : e.value ?? "",
    secret: e.secret,
    hadCipher: e.secret && e.set,
    reveal: false,
  }));
}

/** Build the PUT env map. A secret left blank but previously set sends
 *  `{value: null, secret: true}` to preserve the stored cipher (write-only
 *  secret contract). */
function toEnvMap(rows: Row[]): Record<string, AgentEnvEntryIn> {
  const out: Record<string, AgentEnvEntryIn> = {};
  for (const r of rows) {
    const key = r.key.trim();
    if (!key) continue;
    if (r.secret) {
      out[key] = r.value ? { value: r.value, secret: true } : { value: null, secret: true };
    } else {
      out[key] = { value: r.value, secret: false };
    }
  }
  return out;
}

/**
 * Per-agent environment. Values are merged over the process env at (re)spawn.
 * Editing alone does not restart — "Apply and restart runtime" hands the agent
 * a fresh env mid-conversation by resuming the same session id. A restart is
 * refused (409) while a turn is streaming unless forced.
 */
export function AgentEnvPanel({
  agentId,
  env,
  liveness,
}: {
  agentId: string;
  env: AgentEnvEntryOut[];
  liveness: AgentLiveness;
}) {
  const [rows, setRows] = useState<Row[]>(() => seed(env));
  const putEnv = usePutEnv(agentId);
  const restart = useRestartRuntime(agentId);

  // Re-seed when the server env identity changes (after a successful save).
  const envKey = env.map((e) => `${e.key}:${e.secret}:${e.set}:${e.value ?? ""}`).join("|");
  useEffect(() => setRows(seed(env)), [envKey]);

  const patch = (i: number, p: Partial<Row>) =>
    setRows((rs) => rs.map((r, idx) => (idx === i ? { ...r, ...p } : r)));

  const save = async (): Promise<boolean> => {
    try {
      await putEnv.mutateAsync({ env: toEnvMap(rows) });
      return true;
    } catch (err) {
      toast.error("Could not save environment", { description: describeError(err) });
      return false;
    }
  };

  const applyAndRestart = async (force = false) => {
    if (!(await save())) return;
    try {
      await restart.mutateAsync({ force });
      toast.success("Runtime restarting with the new environment");
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        const ok = await confirm({
          title: "A turn is streaming",
          body: "The agent is mid-turn. Interrupt it and restart the runtime now?",
          confirmLabel: "Interrupt & restart",
          danger: true,
        });
        if (ok) await applyAndRestart(true);
        return;
      }
      toast.error("Could not restart runtime", { description: describeError(err) });
    }
  };

  const streaming = liveness === "alive";
  const busy = putEnv.isPending || restart.isPending;

  return (
    <div className="space-y-3">
      <div className="space-y-2">
        {rows.length === 0 && (
          <p className="text-[12px] text-moon-600">
            No environment variables. Add one to inject config or a token at spawn.
          </p>
        )}
        {rows.map((r, i) => (
          <div key={i} className="flex items-center gap-2">
            <Input
              mono
              value={r.key}
              placeholder="ENV_KEY"
              onChange={(e) => patch(i, { key: e.target.value })}
              className="h-8 w-2/5"
            />
            <span className="text-moon-600">=</span>
            <div className="relative flex-1">
              <Input
                mono
                type={r.secret && !r.reveal ? "password" : "text"}
                value={r.value}
                placeholder={r.hadCipher ? "•••••• (unchanged)" : "value"}
                onChange={(e) => patch(i, { value: e.target.value })}
                className="h-8 pr-8"
              />
              {r.secret && (
                <button
                  type="button"
                  aria-label={r.reveal ? "Hide value" : "Reveal value"}
                  onClick={() => patch(i, { reveal: !r.reveal })}
                  className="absolute inset-y-0 right-1.5 grid place-items-center text-moon-600 hover:text-moon-100"
                >
                  {r.reveal ? <EyeOff size={13} /> : <Eye size={13} />}
                </button>
              )}
            </div>
            <Tooltip content={r.secret ? "Secret (encrypted, write-only)" : "Mark as secret"}>
              <button
                type="button"
                aria-pressed={r.secret}
                aria-label="Toggle secret"
                onClick={() => patch(i, { secret: !r.secret, value: "", hadCipher: false })}
                className={cn(
                  "grid h-8 w-8 shrink-0 place-items-center rounded-control border transition-colors",
                  r.secret
                    ? "border-lamp/40 bg-lamp/10 text-lamp"
                    : "border-ink-700 text-moon-500 hover:text-moon-100",
                )}
              >
                {r.secret ? <EyeOff size={14} /> : <Eye size={14} />}
              </button>
            </Tooltip>
            <IconButton
              label="Remove"
              size="sm"
              icon={<X size={14} />}
              onClick={() => setRows((rs) => rs.filter((_, idx) => idx !== i))}
            />
          </div>
        ))}
        <button
          type="button"
          onClick={() => setRows((rs) => [...rs, { key: "", value: "", secret: false, hadCipher: false, reveal: false }])}
          className="inline-flex items-center gap-1 rounded-control border border-ink-700 px-2.5 py-1 text-xs text-moon-100 hover:bg-ink-800"
        >
          <Plus size={13} /> Add variable
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-2 border-t border-ink-700/60 pt-3">
        <Button size="sm" variant="ghost" leadingIcon={<Save size={13} />} loading={putEnv.isPending} onClick={save}>
          Save
        </Button>
        <Button
          size="sm"
          variant="primary"
          leadingIcon={<RotateCw size={13} />}
          loading={busy}
          onClick={() => applyAndRestart(false)}
        >
          Apply &amp; restart runtime
        </Button>
        <span className="text-[11px] text-moon-600">
          {streaming
            ? "A turn is streaming — restart will ask to interrupt first."
            : "Saving alone applies on the next spawn."}
        </span>
      </div>
    </div>
  );
}
