import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Pencil, Plus, Tag, Trash2 } from "lucide-react";
import { Button } from "@/ui/Button";
import { IconButton } from "@/ui/IconButton";
import { Input, Field } from "@/ui/Input";
import { Dialog } from "@/ui/Dialog";
import { Spinner } from "@/ui/Spinner";
import { EmptyState } from "@/ui/EmptyState";
import { toast } from "@/ui/Toast";
import { labelsApi, useLabels } from "@/api/labels";
import { qk } from "@/api/keys";
import type { LabelOut } from "@/api/types";
import { SectionHeader } from "./parts/SettingsSection";
import { ColorPicker, SWATCHES } from "./parts/ColorPicker";
import { ConfirmDialog } from "./parts/ConfirmDialog";

export function LabelsSection() {
  const qc = useQueryClient();
  const labels = useLabels();
  const [editing, setEditing] = useState<LabelOut | "new" | null>(null);
  const [toDelete, setToDelete] = useState<LabelOut | null>(null);
  const [busy, setBusy] = useState(false);

  const invalidate = () => qc.invalidateQueries({ queryKey: qk.labels });

  async function remove() {
    if (!toDelete) return;
    setBusy(true);
    try {
      await labelsApi.remove(toDelete.id);
      invalidate();
      toast.success(`Deleted “${toDelete.name}”`);
      setToDelete(null);
    } catch (err) {
      toast.error("Could not delete label", { error: err });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <SectionHeader
        title="Labels"
        description="Reusable tags for organizing and filtering tickets."
        actions={
          <Button variant="primary" size="sm" leadingIcon={<Plus size={14} />} onClick={() => setEditing("new")}>
            New label
          </Button>
        }
      />

      {labels.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-moon-400">
          <Spinner /> Loading labels…
        </div>
      ) : labels.data && labels.data.length > 0 ? (
        <ul className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4">
          {labels.data.map((l) => (
            <li
              key={l.id}
              className="flex items-center gap-3 rounded-card border border-ink-700 bg-ink-900 px-3.5 py-2.5"
            >
              <span
                className="h-3.5 w-3.5 shrink-0 rounded-full ring-1 ring-inset ring-black/20"
                style={{ backgroundColor: l.color }}
              />
              <span className="min-w-0 flex-1 truncate text-sm text-moon-100">{l.name}</span>
              <span className="font-mono text-xs text-moon-600">{l.color}</span>
              <IconButton label="Edit" size="sm" icon={<Pencil size={14} />} onClick={() => setEditing(l)} />
              <IconButton
                label="Delete"
                size="sm"
                icon={<Trash2 size={14} />}
                onClick={() => setToDelete(l)}
                className="hover:text-failed"
              />
            </li>
          ))}
        </ul>
      ) : (
        <EmptyState
          icon={<Tag size={18} />}
          title="No labels yet"
          description="Create labels to tag tickets and filter the board."
          action={
            <Button variant="ghost" leadingIcon={<Plus size={14} />} onClick={() => setEditing("new")}>
              New label
            </Button>
          }
        />
      )}

      {editing && (
        <LabelDialog
          label={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            invalidate();
            setEditing(null);
          }}
        />
      )}

      <ConfirmDialog
        open={!!toDelete}
        onOpenChange={(o) => !o && setToDelete(null)}
        title={`Delete “${toDelete?.name}”?`}
        body="The label is removed from every ticket that carries it. This cannot be undone."
        confirmLabel="Delete label"
        danger
        busy={busy}
        onConfirm={remove}
      />
    </div>
  );
}

function LabelDialog({
  label,
  onClose,
  onSaved,
}: {
  label: LabelOut | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(label?.name ?? "");
  const [color, setColor] = useState(label?.color ?? SWATCHES[0]);
  const [busy, setBusy] = useState(false);

  async function save() {
    if (!name.trim()) return;
    setBusy(true);
    try {
      if (label) {
        await labelsApi.update(label.id, { name: name.trim(), color });
      } else {
        await labelsApi.create({ name: name.trim(), color });
      }
      toast.success(label ? "Label updated" : "Label created");
      onSaved();
    } catch (err) {
      toast.error("Could not save label", { error: err });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={label ? "Edit label" : "New label"}
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant="primary" loading={busy} disabled={!name.trim()} onClick={save}>
            {label ? "Save" : "Create"}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="Name">
          <Input
            autoFocus
            placeholder="bug"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && save()}
          />
        </Field>
        <Field label="Color">
          <div className="flex items-center gap-3">
            <span
              className="inline-flex items-center gap-1.5 rounded-full border border-ink-700 px-2 py-0.5 text-[11px] font-medium"
              style={{ backgroundColor: `${color}22`, color, borderColor: `${color}55` }}
            >
              <span className="h-2 w-2 rounded-full" style={{ backgroundColor: color }} />
              {name.trim() || "preview"}
            </span>
          </div>
          <div className="mt-2">
            <ColorPicker value={color} onChange={setColor} />
          </div>
        </Field>
      </div>
    </Dialog>
  );
}
