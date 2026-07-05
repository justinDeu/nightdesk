import { Check } from "lucide-react";
import { cn } from "@/lib/cn";

/** Token-derived swatch palette for labels and project dots. Values are the
 *  literal hex the API stores on the record. */
export const SWATCHES: string[] = [
  "#e5a54b", // lamp
  "#d96c5f", // dawn
  "#d9605a", // ember
  "#8f86e8", // review violet
  "#5fae8b", // sage
  "#7b8aa0", // steel
  "#9aa6bb", // moon
  "#66748c", // faint
  "#c98bd9", // orchid
  "#5fa8d9", // sky
  "#d9a05f", // amber-2
  "#7cae5f", // moss
];

export function ColorPicker({
  value,
  onChange,
}: {
  value: string | null | undefined;
  onChange: (hex: string) => void;
}) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {SWATCHES.map((hex) => {
        const on = (value ?? "").toLowerCase() === hex.toLowerCase();
        return (
          <button
            key={hex}
            type="button"
            aria-label={hex}
            onClick={() => onChange(hex)}
            className={cn(
              "flex h-6 w-6 items-center justify-center rounded-full border transition-transform",
              on ? "border-moon-100 scale-110" : "border-ink-700 hover:scale-105",
            )}
            style={{ backgroundColor: hex }}
          >
            {on && <Check size={13} className="text-ink-950" strokeWidth={3} />}
          </button>
        );
      })}
    </div>
  );
}
