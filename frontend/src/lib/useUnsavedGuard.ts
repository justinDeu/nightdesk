import { useBlocker } from "@tanstack/react-router";

const DEFAULT_MSG = "You have unsaved changes. Leave and discard them?";

/**
 * Guard against silently losing unsaved edits. While `dirty` is true this both
 * confirms before an in-app route change (TanStack's blocker) and arms the
 * browser's native `beforeunload` prompt for reloads / tab close. One hook so
 * every inline editor gets the same behavior without duplicating listeners.
 *
 * Must be called unconditionally (it's a hook); pass the dirty flag rather than
 * gating the call site.
 */
export function useUnsavedGuard(dirty: boolean, message: string = DEFAULT_MSG) {
  useBlocker({
    disabled: !dirty,
    enableBeforeUnload: dirty,
    shouldBlockFn: () => !window.confirm(message),
  });
}
