import type { ReactNode } from "react";
import { Dialog } from "@/ui/Dialog";
import { Button } from "@/ui/Button";

/** A yes/no confirmation, styled to the dusk console. Used for destructive
 *  settings actions (delete label / project / profile, archive). */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  body,
  confirmLabel = "Confirm",
  danger,
  busy,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  title: string;
  body?: ReactNode;
  confirmLabel?: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
}) {
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={busy}>
            Cancel
          </Button>
          <Button
            variant={danger ? "danger" : "primary"}
            loading={busy}
            onClick={onConfirm}
          >
            {confirmLabel}
          </Button>
        </>
      }
    >
      <div className="text-sm text-moon-400">{body}</div>
    </Dialog>
  );
}
