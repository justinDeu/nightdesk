/** Tiny event bus so the quick-composer / full editor can be opened from
 *  anywhere (the `c` shortcut, the shell "New ticket" button, the command
 *  palette) while the actual dialog is mounted once in the app shell. */

type Mode = "quick" | "full";
type Listener = (mode: Mode, prefill?: ComposerPrefill) => void;

export interface ComposerPrefill {
  title?: string;
  project_id?: string | null;
}

const listeners = new Set<Listener>();

export function onComposer(fn: Listener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function openComposer(prefill?: ComposerPrefill): void {
  for (const fn of listeners) fn("quick", prefill);
}

export function openFullEditor(prefill?: ComposerPrefill): void {
  for (const fn of listeners) fn("full", prefill);
}
