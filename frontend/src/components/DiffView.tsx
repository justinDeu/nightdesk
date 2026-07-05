import type { RunDiff, RunDiffFile, RunDiffRow } from "@/api/types";
import { cn } from "@/lib/cn";

/** Render the structured run diff (GET /runs/{rid}/diff): a summary strip plus
 *  per-file hunks with old/new line numbers and +/- gutter coloring. */
export function DiffView({ diff }: { diff: RunDiff | undefined }) {
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
        <FileDiff key={f.path} file={f} />
      ))}
    </div>
  );
}

function FileDiff({ file }: { file: RunDiffFile }) {
  const renamed = file.old_path && file.new_path && file.old_path !== file.new_path;
  return (
    <div className="overflow-hidden rounded-card border border-ink-700 bg-ink-950/60">
      <div className="flex items-center gap-2 border-b border-ink-700/60 bg-ink-900 px-3 py-1.5 font-mono text-[11px]">
        <span className="min-w-0 flex-1 truncate text-moon-100">
          {renamed ? `${file.old_path} → ${file.new_path}` : file.path}
        </span>
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
                <DiffRow key={i} row={row} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function DiffRow({ row }: { row: RunDiffRow }) {
  const tone =
    row.kind === "hunk"
      ? "text-review bg-review/[0.06]"
      : row.kind === "ins"
        ? "text-success bg-success/[0.06]"
        : row.kind === "del"
          ? "text-failed bg-failed/[0.06]"
          : "text-moon-400";
  return (
    <tr className={tone}>
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
  );
}
