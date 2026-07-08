import { useMemo, useState } from "react";
import { Check, MessageSquarePlus, Pencil, Reply, RotateCcw, Trash2, X } from "lucide-react";
import type { DiffCommentCreate, DiffCommentOut, RunDiff, RunDiffFile, RunDiffRow } from "@/api/types";
import { diffCommentsApi } from "@/api/diffComments";
import { Badge } from "@/ui/Badge";
import { Button } from "@/ui/Button";
import { Textarea } from "@/ui/Input";
import { Tooltip } from "@/ui/Tooltip";
import { toast } from "@/ui/Toast";
import { relativeTime } from "@/lib/time";
import { cn } from "@/lib/cn";

interface Thread {
  root: DiffCommentOut;
  replies: DiffCommentOut[];
}

/** The interactive surface passed down when a diff supports commenting. Bundles
 *  the live head (for anchor derivation) and the mutation handlers. */
interface Interactive {
  headSha: string | null;
  comments: DiffCommentOut[];
  onCreate: (payload: DiffCommentCreate) => Promise<void>;
  onReply: (parentId: string, body: string) => Promise<void>;
  onEdit: (id: string, body: string) => Promise<void>;
  onResolve: (id: string, resolved: boolean) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
}

/** Render the structured run diff (GET /runs/{rid}/diff): a summary strip plus
 *  per-file hunks with old/new line numbers and +/- gutter coloring.
 *
 *  When `runId`/`comments`/`onCommentsChange` are supplied the diff becomes
 *  interactive: hover a line for a "+" to open a composer, threads render under
 *  their anchor line, and stale anchors (head advanced) show as "outdated"
 *  rendered against their captured text. Without them it renders read-only. */
export function DiffView({
  diff,
  runId,
  headSha,
  comments,
  onCommentsChange,
}: {
  diff: RunDiff | undefined;
  runId?: string;
  headSha?: string | null;
  comments?: DiffCommentOut[];
  onCommentsChange?: () => void;
}) {
  const interactive = useMemo<Interactive | null>(() => {
    if (!runId || !onCommentsChange) return null;
    const run = async (label: string, fn: () => Promise<unknown>) => {
      try {
        await fn();
        onCommentsChange();
      } catch (err) {
        toast.error(`${label} failed`, { error: err });
        throw err;
      }
    };
    return {
      headSha: headSha ?? null,
      comments: comments ?? [],
      onCreate: (payload) => run("Comment", () => diffCommentsApi.create(runId, payload)),
      onReply: (parentId, body) =>
        run("Reply", () => diffCommentsApi.create(runId, { parent_id: parentId, body })),
      onEdit: (id, body) => run("Edit", () => diffCommentsApi.edit(id, body)),
      onResolve: (id, resolved) =>
        run(resolved ? "Resolve" : "Reopen", () =>
          resolved ? diffCommentsApi.resolve(id) : diffCommentsApi.unresolve(id),
        ),
      onDelete: (id) => run("Delete", () => diffCommentsApi.remove(id)),
    };
  }, [runId, headSha, comments, onCommentsChange]);

  if (!diff) {
    return <p className="px-1 py-4 text-center font-mono text-xs text-moon-600">No diff available.</p>;
  }
  if (diff.error) {
    return <p className="px-1 py-4 text-center font-mono text-xs text-moon-600">{diff.error}</p>;
  }
  if (!diff.files || diff.files.length === 0) {
    return <p className="px-1 py-4 text-center font-mono text-xs text-moon-600">No file changes.</p>;
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[11px] text-moon-400">
        <span className="text-moon-100">
          {diff.total_files} file{diff.total_files === 1 ? "" : "s"}
        </span>
        <span className="text-success">+{diff.total_added}</span>
        <span className="text-failed">-{diff.total_deleted}</span>
        {diff.branch && <span className="text-moon-600">{diff.branch}</span>}
        {diff.truncated && <span className="text-lamp">truncated</span>}
      </div>
      {diff.files.map((f) => (
        <FileDiff key={f.path} file={f} interactive={interactive} />
      ))}
    </div>
  );
}

/** Anchor side/line a row maps to, matching the backend's derivation:
 *  ins → new, del → old, ctx → new. Returns null for hunk headers. */
function rowAnchor(row: RunDiffRow): { side: "old" | "new"; line: number } | null {
  if (row.kind === "hunk") return null;
  const side = row.kind === "del" ? "old" : "new";
  const raw = side === "new" ? row.line_no_new : row.line_no_old;
  const line = Number(raw);
  if (!raw || Number.isNaN(line)) return null;
  return { side, line };
}

const anchorKey = (side: string, line: number) => `${side}:${line}`;

function FileDiff({ file, interactive }: { file: RunDiffFile; interactive: Interactive | null }) {
  const renamed = file.old_path && file.new_path && file.old_path !== file.new_path;

  // Group this file's comments into threads (root + ordered replies), attaching
  // each thread to a diff row when its anchor is current; stale/orphaned threads
  // fall into `detached` and render against their captured text.
  const { threadsByRow, detached, count } = useMemo(() => {
    const byRow = new Map<string, Thread>();
    const detachedThreads: Thread[] = [];
    let n = 0;
    if (interactive) {
      const rowKeys = new Set(
        file.hunks
          .map(rowAnchor)
          .filter((a): a is { side: "old" | "new"; line: number } => a != null)
          .map((a) => anchorKey(a.side, a.line)),
      );
      const roots = interactive.comments.filter(
        (c) => c.parent_id === null && c.file_path === file.path,
      );
      const repliesByParent = new Map<string, DiffCommentOut[]>();
      for (const c of interactive.comments) {
        if (c.parent_id) {
          const arr = repliesByParent.get(c.parent_id) ?? [];
          arr.push(c);
          repliesByParent.set(c.parent_id, arr);
        }
      }
      for (const root of roots) {
        n += 1;
        const thread: Thread = { root, replies: repliesByParent.get(root.id) ?? [] };
        const key = root.side && root.line != null ? anchorKey(root.side, root.line) : null;
        if (!root.outdated && key && rowKeys.has(key) && !byRow.has(key)) {
          byRow.set(key, thread);
        } else {
          detachedThreads.push(thread);
        }
      }
    }
    return { threadsByRow: byRow, detached: detachedThreads, count: n };
  }, [file, interactive]);

  const cols = interactive ? 5 : 4;

  return (
    <div className="overflow-hidden rounded-card border border-ink-700 bg-ink-950/60">
      <div className="flex items-center gap-2 border-b border-ink-700/60 bg-ink-900 px-3 py-1.5 font-mono text-[11px]">
        <span className="min-w-0 flex-1 truncate text-moon-100">
          {renamed ? `${file.old_path} → ${file.new_path}` : file.path}
        </span>
        {count > 0 && (
          <Badge tone="review">
            {count} comment{count === 1 ? "" : "s"}
          </Badge>
        )}
        {file.binary ? (
          <span className="text-moon-600">binary</span>
        ) : (
          <>
            <span className="text-success">+{file.lines_added}</span>
            <span className="text-failed">-{file.lines_deleted}</span>
          </>
        )}
      </div>
      {!file.binary && (
        <div className="overflow-x-auto">
          <table className="min-w-full border-collapse font-mono text-[11px] leading-relaxed">
            <tbody>
              {file.hunks.map((row, i) => (
                <DiffRow
                  key={i}
                  row={row}
                  filePath={file.path}
                  interactive={interactive}
                  thread={rowThread(row, threadsByRow)}
                />
              ))}
              {detached.length > 0 && interactive && (
                <tr>
                  <td colSpan={cols} className="border-t border-ink-700/40 bg-ink-950/40 px-3 py-2">
                    <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-lamp">
                      Outdated comments
                    </div>
                    <div className="space-y-2">
                      {detached.map((t) => (
                        <ThreadCard key={t.root.id} thread={t} interactive={interactive} showAnchor />
                      ))}
                    </div>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function rowThread(row: RunDiffRow, threadsByRow: Map<string, Thread>): Thread | undefined {
  const a = rowAnchor(row);
  if (!a) return undefined;
  return threadsByRow.get(anchorKey(a.side, a.line));
}

function DiffRow({
  row,
  filePath,
  interactive,
  thread,
}: {
  row: RunDiffRow;
  filePath: string;
  interactive: Interactive | null;
  thread: Thread | undefined;
}) {
  const [composing, setComposing] = useState(false);
  const tone =
    row.kind === "hunk"
      ? "text-review bg-review/[0.06]"
      : row.kind === "ins"
        ? "text-success bg-success/[0.06]"
        : row.kind === "del"
          ? "text-failed bg-failed/[0.06]"
          : "text-moon-400";
  const anchor = rowAnchor(row);
  const canComment = interactive != null && anchor != null;

  return (
    <>
      <tr className={cn("group", tone)}>
        {interactive ? (
          <td className="w-6 select-none border-r border-ink-700/40 p-0 text-center align-top">
            {canComment && (
              <Tooltip content="Comment on line">
                <button
                  type="button"
                  aria-label="Comment on line"
                  onClick={() => setComposing((v) => !v)}
                  className={cn(
                    "grid h-full w-6 place-items-center text-moon-600 opacity-0 transition-opacity",
                    "hover:text-lamp focus-visible:opacity-100 group-hover:opacity-100",
                    composing && "text-lamp opacity-100",
                  )}
                >
                  <MessageSquarePlus size={12} />
                </button>
              </Tooltip>
            )}
          </td>
        ) : null}
        <td className="select-none border-r border-ink-700/40 px-2 text-right align-top text-moon-600">
          {row.line_no_old}
        </td>
        <td className="select-none border-r border-ink-700/40 px-2 text-right align-top text-moon-600">
          {row.line_no_new}
        </td>
        <td className="select-none px-1 text-center align-top text-moon-600">{row.gutter}</td>
        <td className={cn("whitespace-pre px-2", row.kind === "hunk" && "font-semibold")}>
          {row.text || " "}
        </td>
      </tr>

      {composing && interactive && anchor && (
        <tr>
          <td colSpan={5} className="bg-ink-950/60 px-3 py-2">
            <Composer
              placeholder="Comment on this line…"
              onCancel={() => setComposing(false)}
              onSubmit={async (body) => {
                await interactive.onCreate({
                  file_path: filePath,
                  side: anchor.side,
                  line: anchor.line,
                  anchor_head_sha: interactive.headSha,
                  anchor_text: row.text,
                  body,
                });
                setComposing(false);
              }}
            />
          </td>
        </tr>
      )}

      {thread && interactive && (
        <tr>
          <td colSpan={5} className="bg-ink-950/40 px-3 py-2">
            <ThreadCard thread={thread} interactive={interactive} />
          </td>
        </tr>
      )}
    </>
  );
}

function ThreadCard({
  thread,
  interactive,
  showAnchor,
}: {
  thread: Thread;
  interactive: Interactive;
  showAnchor?: boolean;
}) {
  const { root, replies } = thread;
  const [collapsed, setCollapsed] = useState(root.resolved);
  const [replying, setReplying] = useState(false);

  const total = 1 + replies.length;

  if (root.resolved && collapsed) {
    return (
      <button
        type="button"
        onClick={() => setCollapsed(false)}
        className="inline-flex items-center gap-2 rounded-control border border-ink-700 bg-ink-900 px-2.5 py-1 text-[11px] text-moon-400 hover:text-moon-100"
      >
        <Check size={12} className="text-success" />
        Resolved · {total} comment{total === 1 ? "" : "s"}
      </button>
    );
  }

  return (
    <div className="space-y-2 rounded-card border border-ink-700 bg-ink-900 p-2.5">
      {(showAnchor || root.outdated) && (
        <div className="flex items-center gap-2">
          {root.outdated && <Badge tone="lamp">outdated</Badge>}
          {root.line != null && (
            <span className="font-mono text-[10px] text-moon-600">
              {root.file_path}:{root.line} ({root.side})
            </span>
          )}
        </div>
      )}
      {root.outdated && root.anchor_text && (
        <pre className="overflow-x-auto rounded-control border border-ink-700/60 bg-ink-950/60 px-2 py-1 font-mono text-[10px] text-moon-500">
          {root.anchor_text}
        </pre>
      )}

      <CommentBody comment={root} interactive={interactive} isRoot />
      {replies.map((r) => (
        <CommentBody key={r.id} comment={r} interactive={interactive} />
      ))}

      <div className="flex items-center gap-1.5">
        {!replying && (
          <Button size="sm" variant="ghost" leadingIcon={<Reply size={12} />} onClick={() => setReplying(true)}>
            Reply
          </Button>
        )}
        {root.resolved ? (
          <Button
            size="sm"
            variant="ghost"
            leadingIcon={<RotateCcw size={12} />}
            onClick={() => interactive.onResolve(root.id, false)}
          >
            Reopen
          </Button>
        ) : (
          <Button
            size="sm"
            variant="ghost"
            leadingIcon={<Check size={12} />}
            onClick={() => interactive.onResolve(root.id, true)}
          >
            Resolve
          </Button>
        )}
      </div>

      {replying && (
        <Composer
          placeholder="Reply…"
          onCancel={() => setReplying(false)}
          onSubmit={async (body) => {
            await interactive.onReply(root.id, body);
            setReplying(false);
          }}
        />
      )}
    </div>
  );
}

function CommentBody({
  comment,
  interactive,
  isRoot,
}: {
  comment: DiffCommentOut;
  interactive: Interactive;
  isRoot?: boolean;
}) {
  const [editing, setEditing] = useState(false);

  if (editing) {
    return (
      <Composer
        initial={comment.body}
        placeholder="Edit comment…"
        submitLabel="Save"
        onCancel={() => setEditing(false)}
        onSubmit={async (body) => {
          await interactive.onEdit(comment.id, body);
          setEditing(false);
        }}
      />
    );
  }

  return (
    <div className={cn("text-[12px]", !isRoot && "border-l border-ink-700 pl-2.5")}>
      <div className="flex items-center gap-2">
        <Badge tone={comment.author_kind === "agent" ? "review" : "neutral"}>
          {comment.author_kind}
        </Badge>
        <span className="text-[10px] text-moon-600">{relativeTime(comment.created_at)}</span>
        <div className="ml-auto flex items-center gap-0.5">
          <Tooltip content="Edit">
            <button
              type="button"
              aria-label="Edit comment"
              onClick={() => setEditing(true)}
              className="rounded-control p-1 text-moon-600 hover:bg-ink-800 hover:text-moon-100"
            >
              <Pencil size={11} />
            </button>
          </Tooltip>
          <Tooltip content="Delete">
            <button
              type="button"
              aria-label="Delete comment"
              onClick={() => interactive.onDelete(comment.id)}
              className="rounded-control p-1 text-moon-600 hover:bg-ink-800 hover:text-failed"
            >
              <Trash2 size={11} />
            </button>
          </Tooltip>
        </div>
      </div>
      <p className="mt-0.5 whitespace-pre-wrap text-moon-100">{comment.body}</p>
    </div>
  );
}

function Composer({
  initial = "",
  placeholder,
  submitLabel = "Comment",
  onSubmit,
  onCancel,
}: {
  initial?: string;
  placeholder: string;
  submitLabel?: string;
  onSubmit: (body: string) => Promise<void>;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(initial);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    const body = value.trim();
    if (!body || busy) return;
    setBusy(true);
    try {
      await onSubmit(body);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-card border border-ink-700 bg-ink-900 p-2">
      <Textarea
        autoFocus
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            submit();
          } else if (e.key === "Escape") {
            onCancel();
          }
        }}
        placeholder={placeholder}
        className="min-h-[56px] text-[12px]"
      />
      <div className="mt-1.5 flex items-center gap-2">
        <span className="text-[10px] text-moon-600">⌘↵ to submit · Esc to cancel</span>
        <Button
          size="sm"
          variant="ghost"
          className="ml-auto"
          leadingIcon={<X size={12} />}
          onClick={onCancel}
        >
          Cancel
        </Button>
        <Button size="sm" variant="primary" disabled={busy || !value.trim()} onClick={submit}>
          {submitLabel}
        </Button>
      </div>
    </div>
  );
}
