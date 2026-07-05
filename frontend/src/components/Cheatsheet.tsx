import { useMemo, useState } from "react";
import { Dialog } from "@/ui/Dialog";
import { Kbd } from "@/ui/Kbd";
import { useActiveBinds, useKeybinds, type Keybind } from "@/lib/keymap";

const GROUP_ORDER = ["Global", "Navigation", "Desk", "Tickets", "Board", "List", "Inbox", "Detail"];

/** Render a combo string as key caps. Handles modifiers, named keys, and
 *  chords ("g b" → g then b). */
function comboCaps(combo: string) {
  const label = (tok: string) => {
    switch (tok) {
      case "mod":
        return "⌘";
      case "shift":
        return "⇧";
      case "alt":
        return "⌥";
      case "space":
        return "Space";
      case "Enter":
        return "↵";
      case "Escape":
        return "Esc";
      case "ArrowUp":
        return "↑";
      case "ArrowDown":
        return "↓";
      case "ArrowLeft":
        return "←";
      case "ArrowRight":
        return "→";
      default:
        return tok;
    }
  };
  return combo.split(" ").map((step, si) => (
    <span key={si} className="inline-flex items-center gap-0.5">
      {si > 0 && <span className="px-0.5 text-moon-600">then</span>}
      {step.split("+").map((tok, ti) => (
        <Kbd key={ti}>{label(tok)}</Kbd>
      ))}
    </span>
  ));
}

export function Cheatsheet() {
  const [open, setOpen] = useState(false);
  const binds = useActiveBinds();

  // Register the cheatsheet's own toggle so it shows up in its own list.
  useKeybinds([
    { combo: "?", label: "Keyboard shortcuts", group: "Global", handler: () => setOpen((o) => !o) },
  ]);

  const groups = useMemo(() => {
    // Combos claimed by a route-scoped bind shadow any global bind on the same
    // combo (see findBind), so hide the shadowed global entry from the sheet.
    const routeCombos = new Set(
      binds.filter((b) => b.scope === "route").map((b) => b.combo),
    );
    const seen = new Set<string>();
    const byGroup = new Map<string, Keybind[]>();
    for (const b of binds) {
      if (b.hidden) continue;
      if (b.scope !== "route" && routeCombos.has(b.combo)) continue;
      const dedupeKey = `${b.group}|${b.combo}|${b.label}`;
      if (seen.has(dedupeKey)) continue;
      seen.add(dedupeKey);
      const arr = byGroup.get(b.group) ?? [];
      arr.push(b);
      byGroup.set(b.group, arr);
    }
    return [...byGroup.entries()].sort((a, b) => groupIndex(a[0]) - groupIndex(b[0]));
  }, [binds]);

  return (
    <Dialog
      open={open}
      onOpenChange={setOpen}
      title="Keyboard shortcuts"
      description="Exactly the shortcuts active on this screen. Nightdesk is keyboard-first."
      size="lg"
    >
      <div className="grid max-h-[65vh] grid-cols-1 gap-x-8 gap-y-5 overflow-y-auto pr-1 sm:grid-cols-2">
        {groups.map(([group, items]) => (
          <div key={group}>
            <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-moon-600">
              {group}
            </h3>
            <div className="space-y-1.5">
              {items.map((b) => (
                <div key={`${b.combo}-${b.label}`} className="flex items-center justify-between gap-4">
                  <span className="text-sm text-moon-400">{b.label}</span>
                  <span className="flex shrink-0 items-center gap-1">{comboCaps(b.combo)}</span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </Dialog>
  );
}

function groupIndex(g: string): number {
  const i = GROUP_ORDER.indexOf(g);
  return i === -1 ? GROUP_ORDER.length : i;
}
