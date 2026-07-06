import { useEffect, useState } from "react";
import { useBlocker } from "@tanstack/react-router";
import { Button } from "@/ui/Button";
import { cn } from "@/lib/cn";

/**
 * Editable-form state with dirty tracking against a server baseline.
 *
 * Pass the server value (or undefined while loading) and a builder that maps it
 * to the editable form shape. The form re-seeds whenever the server identity
 * changes (keyed by `key`), so an external refetch after save rebases cleanly.
 */
export function useEditableForm<S, F>(
  server: S | undefined,
  build: (s: S) => F,
  key: string,
) {
  const [form, setForm] = useState<F | null>(server ? build(server) : null);
  const [baseline, setBaseline] = useState<string>(server ? JSON.stringify(build(server)) : "");

  useEffect(() => {
    if (!server) return;
    const next = build(server);
    setForm(next);
    setBaseline(JSON.stringify(next));
    // Re-seed only when the server identity changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const dirty = form != null && JSON.stringify(form) !== baseline;

  // Guard both a full-page unload (browser's native confirm) and in-app route
  // changes (our own confirm) while there are unsaved edits, so navigating away
  // from any section that uses this hook can't silently drop changes.
  useBlocker({
    shouldBlockFn: () =>
      dirty && !window.confirm("You have unsaved changes. Leave without saving?"),
    enableBeforeUnload: () => dirty,
  });

  return {
    form,
    setForm: (updater: (prev: F) => F) =>
      setForm((prev) => (prev == null ? prev : updater(prev))),
    replace: (next: F) => setForm(next),
    dirty,
    /** Discard edits back to the last committed baseline. */
    discard: () => setForm(JSON.parse(baseline) as F),
    /** Accept the current form as the new baseline (call after a successful save). */
    commit: () => setBaseline(JSON.stringify(form)),
  };
}

/** Sticky action bar shown only when a section has unsaved edits. */
export function SaveBar({
  dirty,
  saving,
  onSave,
  onDiscard,
  saveLabel = "Save changes",
  error,
}: {
  dirty: boolean;
  saving?: boolean;
  onSave: () => void;
  onDiscard: () => void;
  saveLabel?: string;
  error?: string | null;
}) {
  if (!dirty) return null;
  return (
    <div className="sticky bottom-4 z-20 mt-5 flex items-center justify-between gap-3 rounded-card border border-ink-700 bg-ink-800/95 px-4 py-2.5 shadow-[var(--shadow-pop)] backdrop-blur pop-in">
      <span className={cn("text-sm", error ? "text-failed" : "text-moon-400")}>
        {error ?? "You have unsaved changes."}
      </span>
      <div className="flex items-center gap-2">
        <Button variant="ghost" size="sm" onClick={onDiscard} disabled={saving}>
          Discard
        </Button>
        <Button variant="primary" size="sm" loading={saving} onClick={onSave}>
          {saveLabel}
        </Button>
      </div>
    </div>
  );
}
