import { Plus, X } from "lucide-react";
import { Input } from "@/ui/Input";
import { IconButton } from "@/ui/IconButton";

export interface KvPair {
  key: string;
  value: string;
}

/** Edits a list of key/value pairs (environment variables). Values are
 *  write-only secrets — the caller decides when to send the whole map. */
export function KeyValueEditor({
  pairs,
  onChange,
  keyPlaceholder = "ENV_KEY",
  valuePlaceholder = "value",
}: {
  pairs: KvPair[];
  onChange: (next: KvPair[]) => void;
  keyPlaceholder?: string;
  valuePlaceholder?: string;
}) {
  function patch(i: number, patch: Partial<KvPair>) {
    onChange(pairs.map((p, idx) => (idx === i ? { ...p, ...patch } : p)));
  }
  return (
    <div className="space-y-2">
      {pairs.map((p, i) => (
        <div key={i} className="flex items-center gap-2">
          <Input
            mono
            value={p.key}
            placeholder={keyPlaceholder}
            onChange={(e) => patch(i, { key: e.target.value })}
            className="h-8 w-2/5"
          />
          <span className="text-moon-600">=</span>
          <Input
            mono
            value={p.value}
            placeholder={valuePlaceholder}
            onChange={(e) => patch(i, { value: e.target.value })}
            className="h-8 flex-1"
          />
          <IconButton
            label="Remove"
            size="sm"
            icon={<X size={14} />}
            onClick={() => onChange(pairs.filter((_, idx) => idx !== i))}
          />
        </div>
      ))}
      <button
        type="button"
        onClick={() => onChange([...pairs, { key: "", value: "" }])}
        className="inline-flex items-center gap-1 rounded-control border border-ink-700 px-2.5 py-1 text-xs text-moon-100 hover:bg-ink-800"
      >
        <Plus size={13} /> Add variable
      </button>
    </div>
  );
}
