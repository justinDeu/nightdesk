import { useState } from "react";
import { Bookmark, MoreHorizontal, Pencil, Plus, Trash2 } from "lucide-react";
import {
  useViews,
  useCreateView,
  useRenameView,
  useDeleteView,
  type SavedView,
} from "@/api/views";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/ui/DropdownMenu";
import { Dialog } from "@/ui/Dialog";
import { Input, Field } from "@/ui/Input";
import { Button } from "@/ui/Button";
import { Tooltip } from "@/ui/Tooltip";
import { toast } from "@/ui/Toast";
import { ApiError } from "@/api/client";
import { cn } from "@/lib/cn";

export interface ViewState {
  filter: string;
  view: "board" | "list";
}

export interface SavedViewsProps {
  current: ViewState;
  activeId: string | null;
  onApply: (state: ViewState, id: string) => void;
  onClearActive: () => void;
}

function viewToState(v: SavedView): ViewState {
  const p = v.params as Record<string, unknown>;
  return {
    filter: typeof p.f === "string" ? p.f : "",
    view: p.view === "list" ? "list" : "board",
  };
}

export function SavedViews({ current, activeId, onApply, onClearActive }: SavedViewsProps) {
  const views = useViews();
  const create = useCreateView();
  const rename = useRenameView();
  const del = useDeleteView();

  const [saveOpen, setSaveOpen] = useState(false);
  const [name, setName] = useState("");
  const [renaming, setRenaming] = useState<SavedView | null>(null);

  const writable = !views.isError; // read worked; assume writes too (probe on use)

  async function doCreate() {
    if (!name.trim()) return;
    try {
      await create.mutateAsync({
        name: name.trim(),
        surface: "tickets",
        params: { f: current.filter, view: current.view },
      });
      toast.success("View saved");
      setSaveOpen(false);
      setName("");
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        toast.error("Saved views aren't available on this server yet");
      } else {
        toast.error(err instanceof ApiError ? err.message : "Could not save view");
      }
    }
  }

  async function doRename() {
    if (!renaming || !name.trim()) return;
    try {
      await rename.mutateAsync({ id: renaming.id, name: name.trim() });
      toast.success("View renamed");
      setRenaming(null);
      setName("");
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not rename");
    }
  }

  async function doDelete(v: SavedView) {
    try {
      await del.mutateAsync(v.id);
      if (activeId === v.id) onClearActive();
      toast.success("View deleted");
    } catch (err) {
      toast.error(err instanceof ApiError ? err.message : "Could not delete");
    }
  }

  const list = views.data ?? [];
  if (views.isError && list.length === 0) return null; // read endpoint absent

  return (
    <div className="flex items-center gap-1 overflow-x-auto">
      <button
        onClick={onClearActive}
        className={cn(
          "shrink-0 rounded-control px-2.5 py-1 text-sm transition-colors",
          activeId === null ? "bg-ink-800 text-moon-100" : "text-moon-400 hover:text-moon-100",
        )}
      >
        All tickets
      </button>

      {list.map((v) => (
        <div
          key={v.id}
          className={cn(
            "group flex shrink-0 items-center rounded-control transition-colors",
            activeId === v.id ? "bg-ink-800" : "hover:bg-ink-900",
          )}
        >
          <button
            onClick={() => onApply(viewToState(v), v.id)}
            className={cn(
              "flex items-center gap-1.5 py-1 pl-2.5 pr-1 text-sm",
              activeId === v.id ? "text-moon-100" : "text-moon-400 hover:text-moon-100",
            )}
          >
            <Bookmark size={12} className={activeId === v.id ? "text-lamp" : ""} />
            {v.name}
          </button>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                className="rounded-control p-1 text-moon-600 opacity-0 hover:text-moon-100 group-hover:opacity-100"
                aria-label={`${v.name} view options`}
              >
                <MoreHorizontal size={13} />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem
                icon={<Pencil size={14} />}
                onSelect={() => {
                  setRenaming(v);
                  setName(v.name);
                }}
              >
                Rename
              </DropdownMenuItem>
              <DropdownMenuItem icon={<Trash2 size={14} />} danger onSelect={() => doDelete(v)}>
                Delete
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      ))}

      {writable && (
        <Tooltip content="Save current filter as a view">
          <button
            onClick={() => {
              setName("");
              setSaveOpen(true);
            }}
            className="shrink-0 rounded-control p-1.5 text-moon-600 hover:bg-ink-800 hover:text-moon-100"
            aria-label="Save current view"
          >
            <Plus size={14} />
          </button>
        </Tooltip>
      )}

      <Dialog
        open={saveOpen}
        onOpenChange={setSaveOpen}
        title="Save view"
        description="Store the current filter and board/list mode as a reusable tab."
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={() => setSaveOpen(false)}>
              Cancel
            </Button>
            <Button variant="primary" onClick={doCreate} disabled={!name.trim()} loading={create.isPending}>
              Save
            </Button>
          </>
        }
      >
        <Field label="View name">
          <Input
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="My review queue"
            onKeyDown={(e) => e.key === "Enter" && doCreate()}
          />
        </Field>
      </Dialog>

      <Dialog
        open={!!renaming}
        onOpenChange={(o) => !o && setRenaming(null)}
        title="Rename view"
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={() => setRenaming(null)}>
              Cancel
            </Button>
            <Button variant="primary" onClick={doRename} disabled={!name.trim()} loading={rename.isPending}>
              Rename
            </Button>
          </>
        }
      >
        <Field label="View name">
          <Input
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && doRename()}
          />
        </Field>
      </Dialog>
    </div>
  );
}
