import { useEffect, useRef, useSyncExternalStore } from "react";

/**
 * Single source of truth for keyboard shortcuts. Every page/component registers
 * its binds here via {@link useKeybinds}; a single capture-phase dispatcher
 * matches them, and the cheatsheet renders exactly the active set via
 * {@link useActiveBinds}. Capture phase means the palette toggle wins even when
 * a Radix overlay has trapped focus.
 */

export interface Keybind {
  /** e.g. "mod+k", "shift+p", "?", "g b" (space = chord: press g then b). */
  combo: string;
  label: string;
  /** Cheatsheet section, e.g. "Global", "Navigation", "Board". */
  group: string;
  handler: (e: KeyboardEvent) => void;
  /** Fire even when focus is in a text field (e.g. the palette toggle). */
  allowInInput?: boolean;
  /** Register the handler but hide it from the cheatsheet. */
  hidden?: boolean;
  /**
   * "route" binds shadow "global" binds for the same combo while both are
   * registered, regardless of registration order (route pages mount inside the
   * shell's <Outlet>, so their effects fire *before* the shell's global binds —
   * we can't rely on "last registered wins" for route-vs-global precedence).
   * Defaults to "global".
   */
  scope?: "global" | "route";
}

// --- Combo normalization -------------------------------------------------------

/** True when focus is in a text field / editable region. */
export function isTypingTarget(el: EventTarget | null): boolean {
  const node = el as HTMLElement | null;
  if (!node) return false;
  const tag = node.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  if (node.isContentEditable) return true;
  return false;
}

/**
 * Normalize a KeyboardEvent to a combo string.
 * - mod = Cmd or Ctrl; alt appended when held.
 * - shift is appended whenever held AND the key is a letter or a named key, so
 *   "Shift+A" -> "shift+a" (distinct from "a"). Shifted punctuation like "?" is
 *   left as-is (the char already encodes shift), so "?" stays "?".
 * - Space normalizes to "space".
 */
export function comboOf(e: KeyboardEvent): string {
  const parts: string[] = [];
  if (e.metaKey || e.ctrlKey) parts.push("mod");
  if (e.altKey) parts.push("alt");
  const key = e.key;
  const isLetter = key.length === 1 && /[a-z]/i.test(key);
  if (e.shiftKey && (isLetter || key.length > 1)) parts.push("shift");
  const base = key === " " ? "space" : key.length === 1 ? key.toLowerCase() : key;
  parts.push(base);
  return parts.join("+");
}

// --- Registry ------------------------------------------------------------------

let registry: Keybind[] = [];
const subscribers = new Set<() => void>();

function notify() {
  for (const s of subscribers) s();
}

function register(binds: Keybind[]): () => void {
  registry = [...registry, ...binds];
  notify();
  return () => {
    registry = registry.filter((b) => !binds.includes(b));
    notify();
  };
}

function subscribe(fn: () => void): () => void {
  subscribers.add(fn);
  return () => subscribers.delete(fn);
}

/** The currently-registered binds (visible ones), for the cheatsheet. */
export function useActiveBinds(): Keybind[] {
  return useSyncExternalStore(
    subscribe,
    () => registry,
    () => registry,
  );
}

// --- Dispatcher (installed once, capture phase) --------------------------------

const CHORD_TIMEOUT_MS = 800;
let pendingPrefix: string | null = null;
let pendingTimer: ReturnType<typeof setTimeout> | undefined;

function clearPending() {
  pendingPrefix = null;
  if (pendingTimer) clearTimeout(pendingTimer);
  pendingTimer = undefined;
}

function chordPrefixes(): Set<string> {
  const out = new Set<string>();
  for (const b of registry) {
    const sp = b.combo.indexOf(" ");
    if (sp > 0) out.add(b.combo.slice(0, sp));
  }
  return out;
}

/**
 * Resolve a combo to the bind that should fire. A route-scoped bind always
 * shadows a global one for the same combo (order-independent); within the same
 * scope, the last-registered wins.
 */
function findBind(combo: string): Keybind | undefined {
  let fallback: Keybind | undefined;
  for (let i = registry.length - 1; i >= 0; i--) {
    const b = registry[i];
    if (b.combo !== combo) continue;
    if (b.scope === "route") return b;
    if (fallback === undefined) fallback = b;
  }
  return fallback;
}

/** An open menu/listbox popover (any property picker or Select). These trap
 *  focus and own the keyboard; tooltips (role="tooltip") are deliberately
 *  excluded so hovering never disables shortcuts. */
function pickerOverlayOpen(): boolean {
  return !!document.querySelector(
    '[role="menu"][data-state="open"], [role="listbox"][data-state="open"]',
  );
}

function dispatch(e: KeyboardEvent) {
  // Ignore bare modifier presses.
  if (e.key === "Shift" || e.key === "Meta" || e.key === "Control" || e.key === "Alt") return;

  // Let an open Radix overlay (menu/dialog/popover) handle Escape itself before
  // our page-level Esc chain runs — the capture-phase listener would otherwise
  // preempt it.
  if (
    e.key === "Escape" &&
    document.querySelector('[data-radix-popper-content-wrapper], [role="dialog"][data-state="open"]')
  ) {
    return;
  }

  // While a picker menu is open, Radix owns the keyboard: suspend every page
  // bind so arrows / Enter / typeahead reach the menu natively instead of moving
  // the board cursor or opening the focused ticket. j/k are aliased to Arrow
  // Down/Up (muscle memory) by re-dispatching to the focused menu element.
  if (pickerOverlayOpen()) {
    if (e.key === "j" || e.key === "k") {
      e.preventDefault();
      e.stopImmediatePropagation();
      const el = (document.activeElement as HTMLElement | null) ?? document.body;
      el.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: e.key === "j" ? "ArrowDown" : "ArrowUp",
          bubbles: true,
          cancelable: true,
        }),
      );
    }
    return;
  }

  const combo = comboOf(e);
  const typing = isTypingTarget(e.target);

  // Resolve a pending chord first.
  if (pendingPrefix) {
    const chord = `${pendingPrefix} ${combo}`;
    clearPending();
    const bind = findBind(chord);
    if (bind && (!typing || bind.allowInInput)) {
      e.preventDefault();
      bind.handler(e);
      return;
    }
    // fall through: the second key may itself be a normal bind
  }

  const direct = findBind(combo);
  if (direct && (!typing || direct.allowInInput)) {
    e.preventDefault();
    direct.handler(e);
    return;
  }

  // Start a chord if this key is a known prefix (and we're not typing).
  if (!typing && chordPrefixes().has(combo)) {
    pendingPrefix = combo;
    pendingTimer = setTimeout(clearPending, CHORD_TIMEOUT_MS);
    e.preventDefault();
  }
}

let installed = false;
function ensureDispatcher() {
  if (installed || typeof document === "undefined") return;
  installed = true;
  document.addEventListener("keydown", dispatch, true);
}

// --- Hook ----------------------------------------------------------------------

/**
 * Register keybinds for the lifetime of the component. Handlers are read through
 * a ref so callers may pass a fresh array each render without re-binding.
 */
export function useKeybinds(binds: Keybind[], enabled = true) {
  const ref = useRef(binds);
  ref.current = binds;

  useEffect(() => {
    if (!enabled) return;
    ensureDispatcher();
    // Register stable wrapper binds that read the latest handlers from the ref,
    // so identity is stable across renders but behavior stays current.
    const wrapped: Keybind[] = binds.map((b, i) => ({
      ...b,
      handler: (e) => ref.current[i]?.handler(e),
    }));
    return register(wrapped);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, binds.map((b) => `${b.combo}|${b.label}`).join(",")]);
}
