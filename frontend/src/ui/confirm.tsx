import { useSyncExternalStore, type ReactNode } from "react";
import { Dialog } from "@/ui/Dialog";
import { Button } from "@/ui/Button";

/**
 * Imperative confirm, sibling to `toast`. Call `confirm(opts)` from anywhere
 * (event handlers, action hooks) and await a boolean — no per-call-site dialog
 * state. A single <ConfirmHost/> mounted near the app root renders the dialog.
 *
 * Used to gate one-click destructive actions (cancelling an in-flight run) so a
 * stray tap can't kill an expensive run.
 */
export interface ConfirmOptions {
  title: string;
  body?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
}

interface ConfirmState extends ConfirmOptions {
  open: boolean;
  resolve?: (v: boolean) => void;
}

let state: ConfirmState = { open: false, title: "" };
const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}

function subscribe(l: () => void) {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}

export function confirm(opts: ConfirmOptions): Promise<boolean> {
  return new Promise((resolve) => {
    // If a prompt is somehow already open, dismiss it as declined first.
    state.resolve?.(false);
    state = { ...opts, open: true, resolve };
    emit();
  });
}

function settle(v: boolean) {
  state.resolve?.(v);
  state = { ...state, open: false, resolve: undefined };
  emit();
}

export function ConfirmHost() {
  const s = useSyncExternalStore(subscribe, () => state);
  return (
    <Dialog
      open={s.open}
      onOpenChange={(o) => {
        if (!o) settle(false);
      }}
      title={s.title}
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={() => settle(false)}>
            {s.cancelLabel ?? "Cancel"}
          </Button>
          <Button variant={s.danger ? "danger" : "primary"} onClick={() => settle(true)}>
            {s.confirmLabel ?? "Confirm"}
          </Button>
        </>
      }
    >
      {s.body ? <div className="text-sm text-moon-400">{s.body}</div> : null}
    </Dialog>
  );
}
