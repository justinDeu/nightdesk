import { useEffect, useRef, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowRight,
  Check,
  CircleDot,
  Pencil,
  Plus,
  Tag,
  X,
} from "lucide-react";
import { Page } from "@/components/Page";
import { Button } from "@/ui/Button";
import { Textarea } from "@/ui/Input";
import { Kbd } from "@/ui/Kbd";
import { EmptyState } from "@/ui/EmptyState";
import { Tooltip } from "@/ui/Tooltip";
import { toast } from "@/ui/Toast";
import {
  ProfilePicker,
  PriorityPicker,
  ProjectPicker,
  LabelPicker,
} from "@/components/PropertyPickers";
import { ProjectTag } from "@/components/ProjectDot";
import { PathInput } from "@/components/PathInput";
import { useInbox } from "@/api/inbox";
import { ticketsApi } from "@/api/tickets";
import { useProfiles } from "@/api/profiles";
import { useProjectMap, useProjects } from "@/api/projects";
import { useLabels, labelsApi } from "@/api/labels";
import { ApiError } from "@/api";
import type { InboxItemOut, TicketCreate, TicketOut } from "@/api/types";
import { useTicketActions } from "@/lib/ticketActions";
import { useKeybinds } from "@/lib/keymap";
import { relativeTime } from "@/lib/time";
import { openFullEditor } from "@/components/composerBus";
import { cn } from "@/lib/cn";

const POLL = 4000;

// Bordered trigger chrome for the inline triage pickers — keeps the shared
// PropertyPickers looking like the boxed property chips this pane used before.
const triggerCls =
  "inline-flex items-center rounded-control border border-ink-700 bg-ink-950 px-2 py-1 hover:bg-ink-800";

// The three completeness blockers the domain emits (domain/tickets.py
// ticket_completeness). Each maps to an inline fix control on the right pane.
type BlockerKind = "title" | "profile" | "workspace";
function blockerKind(text: string): BlockerKind | null {
  if (text.includes("title")) return "title";
  if (text.includes("profile")) return "profile";
  if (text.includes("workspace") || text.includes("source path")) return "workspace";
  return null;
}

export function InboxPage() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const actions = useTicketActions();
  const projects = useProjectMap();

  const inboxQ = useInbox(null, { refetchInterval: POLL });
  const items = inboxQ.data ?? [];

  const [cursor, setCursor] = useState(0);
  useEffect(() => {
    if (cursor >= items.length) setCursor(Math.max(0, items.length - 1));
  }, [items.length, cursor]);
  const focused: InboxItemOut | undefined = items[cursor];

  const rowRefs = useRef<(HTMLButtonElement | null)[]>([]);
  useEffect(() => {
    rowRefs.current[cursor]?.scrollIntoView({ block: "nearest" });
  }, [cursor]);

  const invalidateInbox = () => qc.invalidateQueries({ queryKey: ["inbox"] });

  // On a blocked item the API 422s and useTicketActions surfaces the reason;
  // the completeness checklist on the right is already the persistent feedback.
  function promote(item: InboxItemOut, target: "draft" | "queued") {
    void actions.promote(item.ticket, target);
  }

  useKeybinds([
    { combo: "j", label: "Cursor down", group: "Inbox", handler: () => setCursor((c) => Math.min(items.length - 1, c + 1)) },
    { combo: "k", label: "Cursor up", group: "Inbox", handler: () => setCursor((c) => Math.max(0, c - 1)) },
    { combo: "Enter", label: "Open ticket", group: "Inbox", handler: () => focused && navigate({ to: "/tickets/$id", params: { id: focused.ticket.id } }) },
    { combo: "a", label: "Promote to draft", group: "Inbox", handler: () => focused && promote(focused, "draft") },
    { combo: "q", label: "Promote to queued", group: "Inbox", handler: () => focused && promote(focused, "queued") },
    { combo: "d", label: "Decline", group: "Inbox", handler: () => focused && actions.decline(focused.ticket) },
    { combo: "e", label: "Open full editor", group: "Inbox", handler: () => focused && openFullEditor({ title: focused.ticket.title }) },
  ]);

  return (
    <Page
      bleed
      className="flex h-full flex-col"
      title="Inbox"
      subtitle="Triage captures into drafts or the queue. Every item passes a completeness check first."
    >
      <QuickCapture onCaptured={invalidateInbox} />

      <div className="flex min-h-0 flex-1 flex-col">
        {inboxQ.isLoading ? (
          <div className="grid flex-1 place-items-center text-sm text-moon-600">Loading inbox…</div>
        ) : items.length === 0 ? (
          <div className="grid flex-1 place-items-center">
            <div className="max-w-2xl">
              <EmptyState
                icon={<Check size={18} />}
                title="Inbox zero"
                description="Nothing to triage. Agent- and human-captured items land here for a completeness check before they become drafts or hit the queue."
              />
            </div>
          </div>
        ) : (
          <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[minmax(0,22rem)_1fr]">
            {/* Left: triage list */}
            <div className="flex min-h-0 flex-col overflow-hidden rounded-card border border-ink-700/50 shadow-[var(--shadow-raised)]">
              <div className="flex shrink-0 items-center justify-between bg-ink-900 px-3 py-1.5">
                <span className="font-display text-[11px] font-semibold uppercase tracking-wide text-moon-400">
                  Captured
                </span>
                <span className="font-mono text-[10px] text-moon-600">{items.length}</span>
              </div>
              <div className="flex-1 overflow-y-auto">
              {items.map((item, i) => {
                const project = item.ticket.project_id
                  ? projects.get(item.ticket.project_id)
                  : undefined;
                const ready = item.blockers.length === 0;
                return (
                  <button
                    key={item.ticket.id}
                    ref={(el) => (rowRefs.current[i] = el)}
                    onClick={() => setCursor(i)}
                    className={cn(
                      "flex w-full items-start gap-2.5 border-t border-ink-700/60 px-3 py-2.5 text-left transition-colors",
                      i === cursor ? "bg-ink-800" : "hover:bg-ink-800/60",
                      i === cursor && "ring-1 ring-inset ring-lamp/40",
                    )}
                  >
                    <span className="mt-0.5 shrink-0">
                      {ready ? (
                        <Check size={14} className="text-success" />
                      ) : (
                        <CircleDot size={14} className="text-lamp" />
                      )}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-medium text-moon-100">
                        {item.ticket.title || "Untitled capture"}
                      </span>
                      <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-moon-600">
                        <ProjectTag project={project} showNone />
                        <span>{relativeTime(item.ticket.created_at)}</span>
                      </span>
                    </span>
                    {!ready && (
                      <Tooltip content={`${item.blockers.length} blocker${item.blockers.length === 1 ? "" : "s"}`}>
                        <span className="mt-0.5 inline-flex shrink-0 items-center gap-1 rounded-full border border-lamp/25 bg-lamp/10 px-1.5 py-0.5 text-[10px] font-semibold text-lamp">
                          <AlertTriangle size={10} />
                          {item.blockers.length}
                        </span>
                      </Tooltip>
                    )}
                  </button>
                );
              })}
            </div>
          </div>

          {/* Right: focused item */}
          {focused && (
            <TriageDetail
              key={focused.ticket.id}
              item={focused}
              onChanged={invalidateInbox}
              onPromote={(target) => promote(focused, target)}
              onDecline={() => actions.decline(focused.ticket)}
            />
          )}
          </div>
        )}
      </div>

      <p className="mt-3 flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-moon-600">
        <span>
          <Kbd>j</Kbd> <Kbd>k</Kbd> move
        </span>
        <span>
          <Kbd>a</Kbd> promote to draft
        </span>
        <span>
          <Kbd>q</Kbd> promote + queue
        </span>
        <span>
          <Kbd>d</Kbd> decline
        </span>
        <span>
          <Kbd>e</Kbd> edit full
        </span>
        <span>
          <Kbd>l</Kbd> labels
        </span>
        <span>
          <Kbd>p</Kbd> project
        </span>
        <span>
          <Kbd>P</Kbd> priority
        </span>
      </p>
    </Page>
  );
}

// --- Quick capture -------------------------------------------------------------

function QuickCapture({ onCaptured }: { onCaptured: () => void }) {
  const [title, setTitle] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit() {
    const t = title.trim();
    if (!t || busy) return;
    setBusy(true);
    try {
      // Inbox captures are deliberately profile-less: the API accepts a null
      // profile for status=inbox, and the "profile required" completeness
      // blocker then drives the choice during triage (inline ProfilePicker).
      const body = { title: t, status: "inbox", profile_id: null } as unknown as TicketCreate;
      await ticketsApi.create(body);
      setTitle("");
      onCaptured();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Capture failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mb-4 flex items-center gap-2 rounded-card border border-ink-700/50 bg-ink-900 px-2 py-2 shadow-[var(--shadow-raised)] transition-colors focus-within:border-lamp">
      <Plus size={16} aria-hidden className="ml-1 shrink-0 text-moon-600" />
      <input
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") submit();
        }}
        placeholder="Capture a thought — press Enter to drop it in the inbox…"
        aria-label="Quick capture"
        autoComplete="off"
        className="h-8 flex-1 bg-transparent text-sm text-moon-100 placeholder:text-moon-600 focus:outline-none"
      />
      <Button size="sm" variant="primary" onClick={submit} loading={busy} disabled={!title.trim()}>
        Capture
      </Button>
    </div>
  );
}

// --- Triage detail (right pane) ------------------------------------------------

function TriageDetail({
  item,
  onChanged,
  onPromote,
  onDecline,
}: {
  item: InboxItemOut;
  onChanged: () => void;
  onPromote: (target: "draft" | "queued") => void;
  onDecline: () => void;
}) {
  const { ticket, blockers } = item;
  const ready = blockers.length === 0;
  const projects = useProjects();
  const labels = useLabels();

  // Set ticket metadata during triage without opening the full editor.
  async function patchMeta(fn: () => Promise<unknown>, msg: string) {
    try {
      await fn();
      onChanged();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : msg);
    }
  }

  const labelIds = ticket.labels.map((l) => l.id);

  // Keyboard triage of metadata: l / p / Shift+P open the matching picker for
  // the focused item. The dropdowns are controlled so keyboard and click share
  // one open-state path — Radix opens on pointerdown, so a synthetic click on
  // the uncontrolled shared PropertyPickers wouldn't fire.
  const [openPicker, setOpenPicker] = useState<null | "priority" | "project" | "label">(null);
  useKeybinds([
    { combo: "l", label: "Set labels", group: "Inbox", handler: () => setOpenPicker("label") },
    { combo: "p", label: "Set project", group: "Inbox", handler: () => setOpenPicker("project") },
    { combo: "shift+p", label: "Set priority", group: "Inbox", handler: () => setOpenPicker("priority") },
  ]);

  return (
    <div className="flex min-h-0 flex-col gap-4 overflow-y-auto rounded-card border border-ink-700/50 bg-ink-900 p-5 shadow-[var(--shadow-raised)]">
      <InlineTitle ticket={ticket} onSaved={onChanged} />
      <InlinePrompt ticket={ticket} onSaved={onChanged} />

      {/* Properties — set metadata inline without leaving triage */}
      <div>
        <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-moon-600">
          Properties
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {/* Priority (Shift+P), Project (p), Labels (l) — the shared pickers,
              opened programmatically from the keybinds via controlled open. */}
          <PriorityPicker
            value={ticket.priority}
            onChange={(v) =>
              patchMeta(() => ticketsApi.setPriority(ticket.id, v), "Could not set priority")
            }
            open={openPicker === "priority"}
            onOpenChange={(o) => setOpenPicker(o ? "priority" : null)}
            triggerClassName={triggerCls}
          />

          <ProjectPicker
            value={ticket.project_id}
            projects={projects.data ?? []}
            onChange={(id) =>
              patchMeta(() => ticketsApi.setProject(ticket.id, id), "Could not set project")
            }
            open={openPicker === "project"}
            onOpenChange={(o) => setOpenPicker(o ? "project" : null)}
            triggerClassName={triggerCls}
          />

          <LabelPicker
            value={labelIds}
            labels={labels.data ?? []}
            onChange={(ids) =>
              patchMeta(() => labelsApi.setTicketLabels(ticket.id, ids), "Could not set labels")
            }
            open={openPicker === "label"}
            onOpenChange={(o) => setOpenPicker(o ? "label" : null)}
            triggerClassName={triggerCls}
          >
            {ticket.labels.length > 0 ? (
              <span className="flex items-center gap-1">
                {ticket.labels.slice(0, 3).map((l) => (
                  <span
                    key={l.id}
                    className="rounded-full px-1.5 py-0.5 text-[10px] font-medium text-ink-950"
                    style={{ backgroundColor: l.color }}
                  >
                    {l.name}
                  </span>
                ))}
                {ticket.labels.length > 3 && (
                  <span className="text-[10px] text-moon-400">+{ticket.labels.length - 3}</span>
                )}
              </span>
            ) : (
              <span className="flex items-center gap-1 text-[11px] text-moon-400">
                <Tag size={12} /> Labels
              </span>
            )}
          </LabelPicker>
        </div>
      </div>

      {/* Completeness checklist */}
      <div>
        <div className="mb-2 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-moon-600">
          Completeness
          {ready ? (
            <span className="inline-flex items-center gap-1 rounded-full border border-success/30 bg-success/10 px-1.5 py-0.5 text-success">
              <Check size={11} /> ready
            </span>
          ) : (
            <span className="rounded-full border border-lamp/25 bg-lamp/10 px-1.5 py-0.5 text-lamp">
              {blockers.length} to resolve
            </span>
          )}
        </div>
        {ready ? (
          <p className="rounded-control border border-success/25 bg-success/[0.06] px-3 py-2 text-sm text-moon-400">
            All checks pass. Promote to a draft or straight to the queue.
          </p>
        ) : (
          <div className="space-y-2">
            {blockers.map((b) => (
              <BlockerFix key={b} text={b} ticket={ticket} onSaved={onChanged} />
            ))}
          </div>
        )}
      </div>

      {/* Actions */}
      <div className="flex flex-wrap items-center gap-2 border-t border-ink-700 pt-4">
        <Button
          variant="primary"
          leadingIcon={<ArrowRight size={14} />}
          disabled={!ready}
          onClick={() => onPromote("draft")}
        >
          Promote to draft
        </Button>
        <Button variant="subtle" disabled={!ready} onClick={() => onPromote("queued")}>
          Promote + queue
        </Button>
        <div className="flex-1" />
        <Button
          variant="ghost"
          leadingIcon={<Pencil size={13} />}
          onClick={() => openFullEditor({ title: ticket.title })}
        >
          Edit full
        </Button>
        <Button variant="danger" leadingIcon={<X size={14} />} onClick={onDecline}>
          Decline
        </Button>
      </div>
      {!ready && (
        <p className="-mt-1 text-[11px] text-moon-600">
          Resolve the blockers above to unlock promotion — or open the full editor for the rest of
          the ticket.
        </p>
      )}
    </div>
  );
}

function InlineTitle({ ticket, onSaved }: { ticket: TicketOut; onSaved: () => void }) {
  const [value, setValue] = useState(ticket.title);
  useEffect(() => setValue(ticket.title), [ticket.id, ticket.title]);

  async function commit() {
    const next = value.trim();
    if (next === ticket.title) return;
    try {
      await ticketsApi.update(ticket.id, { title: next });
      onSaved();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not save title");
      setValue(ticket.title);
    }
  }

  return (
    <div>
      <label className="mb-1.5 block text-[11px] font-semibold uppercase tracking-wide text-moon-600">
        Title
      </label>
      <input
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
        }}
        placeholder="Untitled capture"
        autoComplete="off"
        className="w-full rounded-control border border-transparent bg-transparent px-0 py-0.5 font-display text-lg font-semibold text-moon-100 placeholder:text-moon-600 hover:border-ink-700 hover:px-2 focus:border-lamp focus:px-2 focus:outline-none"
      />
    </div>
  );
}

function InlinePrompt({ ticket, onSaved }: { ticket: TicketOut; onSaved: () => void }) {
  const [value, setValue] = useState(ticket.prompt);
  useEffect(() => setValue(ticket.prompt), [ticket.id, ticket.prompt]);

  async function commit() {
    if (value === ticket.prompt) return;
    try {
      await ticketsApi.update(ticket.id, { prompt: value });
      onSaved();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not save prompt");
      setValue(ticket.prompt);
    }
  }

  return (
    <div>
      <label className="mb-1.5 block text-[11px] font-semibold uppercase tracking-wide text-moon-600">
        Prompt
      </label>
      <Textarea
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onBlur={commit}
        placeholder="What should the agent do? Describe the task…"
        className="min-h-[120px]"
      />
    </div>
  );
}

// Each blocker names the missing field and offers the fix control inline.
function BlockerFix({
  text,
  ticket,
  onSaved,
}: {
  text: string;
  ticket: TicketOut;
  onSaved: () => void;
}) {
  const kind = blockerKind(text);
  const profiles = useProfiles();
  const primary = ticket.workspaces.find((w) => w.role === "primary");
  const [path, setPath] = useState(primary?.source_path ?? "");
  useEffect(() => setPath(primary?.source_path ?? ""), [ticket.id, primary?.source_path]);

  async function patch(body: Parameters<typeof ticketsApi.update>[1], msg: string) {
    try {
      await ticketsApi.update(ticket.id, body);
      onSaved();
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : msg);
    }
  }

  return (
    <div className="rounded-control border border-lamp/25 bg-lamp/[0.05] px-3 py-2.5">
      <div className="mb-2 flex items-center gap-2 text-sm text-moon-100">
        <AlertTriangle size={13} className="shrink-0 text-lamp" />
        <span className="first-letter:uppercase">{text}</span>
      </div>
      {kind === "profile" && (
        <ProfilePicker
          value={ticket.profile_id}
          profiles={profiles.data ?? []}
          onChange={(id) => patch({ profile_id: id }, "Could not set profile")}
        />
      )}
      {kind === "workspace" && (
        <div className="flex items-end gap-2">
          <div className="flex-1">
            <PathInput
              value={path}
              onChange={setPath}
              placeholder="/home/you/project"
            />
          </div>
          <Button
            size="sm"
            variant="subtle"
            disabled={!path.trim()}
            onClick={() => patch({ source_path: path.trim() }, "Could not set workspace")}
          >
            Set path
          </Button>
        </div>
      )}
      {kind === "title" && (
        <p className="text-[11px] text-moon-600">Give the ticket a title in the field above.</p>
      )}
    </div>
  );
}
