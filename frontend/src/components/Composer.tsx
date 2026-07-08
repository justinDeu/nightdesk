import { useEffect, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { Maximize2, Rocket } from "lucide-react";
import { Dialog } from "@/ui/Dialog";
import { Button } from "@/ui/Button";
import { Input, Textarea, Field } from "@/ui/Input";
import { Select } from "@/ui/Select";
import { toast } from "@/ui/Toast";
import { PathInput } from "./PathInput";
import { PRIORITY_SCALE } from "@/lib/priority";
import { onComposer, type ComposerPrefill } from "./composerBus";
import { ticketsApi, useInvalidateTickets } from "@/api/tickets";
import { labelsApi, useLabels } from "@/api/labels";
import { useProfiles } from "@/api/profiles";
import { useProjects } from "@/api/projects";
import type { TicketCreate, WorkspaceKind } from "@/api/types";
import { cn } from "@/lib/cn";

type Mode = "quick" | "full" | null;

const WORKSPACE_MODES: { value: WorkspaceKind; label: string }[] = [
  { value: "directory", label: "Directory (work in place)" },
  { value: "git_worktree", label: "Git worktree (isolated branch)" },
  { value: "in_place", label: "In place" },
];

export function Composer() {
  const [mode, setMode] = useState<Mode>(null);
  const [prefill, setPrefill] = useState<ComposerPrefill>({});

  useEffect(
    () =>
      onComposer((m, pf) => {
        setPrefill(pf ?? {});
        setMode(m);
      }),
    [],
  );

  const close = () => setMode(null);

  return (
    <>
      <QuickComposer
        open={mode === "quick"}
        prefill={prefill}
        onClose={close}
        onExpand={() => setMode("full")}
      />
      <FullEditor open={mode === "full"} prefill={prefill} onClose={close} />
    </>
  );
}

// --- Quick composer ------------------------------------------------------------

function QuickComposer({
  open,
  prefill,
  onClose,
  onExpand,
}: {
  open: boolean;
  prefill: ComposerPrefill;
  onClose: () => void;
  onExpand: () => void;
}) {
  const invalidate = useInvalidateTickets();
  const projects = useProjects();
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [projectId, setProjectId] = useState<string>("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) {
      setTitle(prefill.title ?? "");
      setPrompt("");
      setProjectId(prefill.project_id ?? "");
    }
  }, [open, prefill]);

  async function submit() {
    if (!title.trim()) return;
    setBusy(true);
    try {
      // Inbox captures carry no profile — triage's completeness gate demands
      // one at promote time. Only title is required here.
      await ticketsApi.create({
        title: title.trim(),
        prompt,
        status: "inbox",
        profile_id: null,
        project_id: projectId || null,
      });
      invalidate();
      toast.success("Sent to Inbox", { description: "Flesh it out and promote to run." });
      onClose();
    } catch (err) {
      toast.error("Could not create ticket", { error: err });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => !o && onClose()}
      title="New ticket"
      description="Captured to the Inbox for a completeness check before it can run."
      size="md"
      footer={
        <>
          <Button variant="ghost" leadingIcon={<Maximize2 size={14} />} onClick={onExpand}>
            Open full editor
          </Button>
          <Button variant="primary" loading={busy} disabled={!title.trim()} onClick={submit}>
            Capture
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="Title">
          <Input
            autoFocus
            placeholder="Fix the flaky worker heartbeat"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit();
            }}
          />
        </Field>
        <Field label="Prompt" hint="What should the agent do? Optional now — profile and prompt are required when you promote it to run.">
          <Textarea
            placeholder="Describe the task…"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
          />
        </Field>
        <Field label="Project">
          <Select value={projectId} onChange={(e) => setProjectId(e.target.value)}>
            <option value="">No project</option>
            {(projects.data ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </Select>
        </Field>
      </div>
    </Dialog>
  );
}

// --- Full editor ---------------------------------------------------------------

function FullEditor({
  open,
  prefill,
  onClose,
}: {
  open: boolean;
  prefill: ComposerPrefill;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const invalidate = useInvalidateTickets();
  const profiles = useProfiles();
  const projects = useProjects();
  const labels = useLabels();

  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [projectId, setProjectId] = useState("");
  const [profileId, setProfileId] = useState("");
  const [priority, setPriority] = useState(0);
  const [sourcePath, setSourcePath] = useState("");
  const [workspaceMode, setWorkspaceMode] = useState<WorkspaceKind>("directory");
  const [labelIds, setLabelIds] = useState<string[]>([]);
  const [initialStatus, setInitialStatus] = useState<"draft" | "queued">("draft");
  const [runNow, setRunNow] = useState(false);
  const [commitOnFinish, setCommitOnFinish] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) {
      setTitle(prefill.title ?? "");
      setPrompt("");
      setProjectId(prefill.project_id ?? "");
      setProfileId(profiles.data?.[0]?.id ?? "");
      setPriority(0);
      setSourcePath("");
      setWorkspaceMode("directory");
      setLabelIds([]);
      setInitialStatus("draft");
      setRunNow(false);
      setCommitOnFinish(false);
    }
  }, [open, prefill, profiles.data]);

  const needsWorkspace = !projectId; // a project supplies default source_path
  const canSubmit = title.trim() && profileId && (!needsWorkspace || sourcePath.trim());

  async function submit() {
    if (!canSubmit) return;
    setBusy(true);
    try {
      const body: TicketCreate = {
        title: title.trim(),
        prompt,
        status: initialStatus,
        profile_id: profileId,
        project_id: projectId || null,
        priority,
        run_now: runNow,
        commit_on_finish: commitOnFinish,
      };
      if (sourcePath.trim()) {
        body.source_path = sourcePath.trim();
        body.workspace_mode = workspaceMode;
      }
      const t = await ticketsApi.create(body);
      if (labelIds.length) {
        await labelsApi.setTicketLabels(t.id, labelIds).catch(() => undefined);
      }
      invalidate();
      toast.success("Ticket created", {
        description: runNow ? "Run dispatched." : `Added to ${initialStatus}.`,
      });
      onClose();
      navigate({ to: "/tickets/$id", params: { id: t.id } });
    } catch (err) {
      toast.error("Could not create ticket", { error: err });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => !o && onClose()}
      title="New ticket — full editor"
      size="lg"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            loading={busy}
            disabled={!canSubmit}
            leadingIcon={runNow ? <Rocket size={14} /> : undefined}
            onClick={submit}
          >
            {runNow ? "Create & run" : "Create ticket"}
          </Button>
        </>
      }
    >
      <div className="max-h-[65vh] space-y-4 overflow-y-auto overscroll-contain pr-1">
        <Field label="Title">
          <Input
            autoFocus
            placeholder="Fix the flaky worker heartbeat"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
        </Field>
        <Field label="Prompt">
          <Textarea
            className="min-h-[120px]"
            placeholder="Describe the task for the agent…"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
          />
        </Field>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="Project">
            <Select value={projectId} onChange={(e) => setProjectId(e.target.value)}>
              <option value="">No project</option>
              {(projects.data ?? []).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Profile">
            <Select value={profileId} onChange={(e) => setProfileId(e.target.value)}>
              {(profiles.data ?? []).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </Select>
          </Field>
        </div>

        <Field
          label="Source path"
          hint={
            projectId
              ? "Optional — the project's default workspace is used when blank."
              : "Required — the primary workspace the agent runs against."
          }
        >
          <PathInput value={sourcePath} onChange={setSourcePath} invalid={needsWorkspace && !sourcePath.trim()} />
        </Field>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="Workspace mode">
            <Select
              value={workspaceMode}
              onChange={(e) => setWorkspaceMode(e.target.value as WorkspaceKind)}
            >
              {WORKSPACE_MODES.map((w) => (
                <option key={w.value} value={w.value}>
                  {w.label}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Priority">
            <Select value={String(priority)} onChange={(e) => setPriority(Number(e.target.value))}>
              {PRIORITY_SCALE.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.short} · {p.label}
                </option>
              ))}
            </Select>
          </Field>
        </div>

        {labels.data && labels.data.length > 0 && (
          <Field label="Labels">
            <div className="flex flex-wrap gap-1.5">
              {labels.data.map((l) => {
                const on = labelIds.includes(l.id);
                return (
                  <button
                    key={l.id}
                    type="button"
                    onClick={() =>
                      setLabelIds((ids) =>
                        on ? ids.filter((x) => x !== l.id) : [...ids, l.id],
                      )
                    }
                    className={cn(
                      "rounded-full border px-2 py-0.5 text-[11px] font-medium transition-colors",
                      on
                        ? "border-transparent text-ink-950"
                        : "border-ink-700 text-moon-400 hover:text-moon-100",
                    )}
                    style={on ? { backgroundColor: l.color } : undefined}
                  >
                    {l.name}
                  </button>
                );
              })}
            </div>
          </Field>
        )}

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="Initial status">
            <Select
              value={initialStatus}
              onChange={(e) => setInitialStatus(e.target.value as "draft" | "queued")}
            >
              <option value="draft">Draft</option>
              <option value="queued">Queued</option>
            </Select>
          </Field>
          <div className="flex flex-col justify-end gap-2 pb-1">
            <label className="flex items-center gap-2 text-sm text-moon-100">
              <input
                type="checkbox"
                checked={runNow}
                onChange={(e) => setRunNow(e.target.checked)}
                className="accent-lamp"
              />
              Run immediately
            </label>
            <label className="flex items-center gap-2 text-sm text-moon-100">
              <input
                type="checkbox"
                checked={commitOnFinish}
                onChange={(e) => setCommitOnFinish(e.target.checked)}
                className="accent-lamp"
              />
              Commit on finish
            </label>
          </div>
        </div>
      </div>
    </Dialog>
  );
}
