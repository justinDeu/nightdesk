import { useNavigate } from "@tanstack/react-router";
import { useKeybinds, type Keybind } from "@/lib/keymap";
import { openComposer } from "./composerBus";

/** App-wide shortcuts, registered once in the shell. Route-level binds (Tickets,
 *  Desk, Inbox) register on top and override where combos collide (e.g. "/"). */
export function GlobalKeybinds() {
  const navigate = useNavigate();

  const binds: Keybind[] = [
    { combo: "c", label: "New ticket", group: "Global", handler: () => openComposer() },
    {
      combo: "shift+c",
      label: "Quick-capture to inbox",
      group: "Global",
      handler: () => openComposer(),
    },
    {
      combo: "[",
      label: "Toggle sidebar",
      group: "Global",
      handler: () => window.dispatchEvent(new CustomEvent("nightdesk:toggle-sidebar")),
    },
    {
      combo: "/",
      label: "Search (command palette)",
      group: "Global",
      handler: () => window.dispatchEvent(new CustomEvent("nightdesk:open-palette")),
    },
    // g-chord navigation (press g then the letter, within 800ms).
    { combo: "g d", label: "Go to Desk", group: "Navigation", handler: () => navigate({ to: "/" }) },
    { combo: "g t", label: "Go to Tickets", group: "Navigation", handler: () => navigate({ to: "/tickets" }) },
    {
      combo: "g b",
      label: "Go to Tickets · board",
      group: "Navigation",
      handler: () => navigate({ to: "/tickets", search: (p) => ({ ...p, view: "board" }) }),
    },
    {
      combo: "g l",
      label: "Go to Tickets · list",
      group: "Navigation",
      handler: () => navigate({ to: "/tickets", search: (p) => ({ ...p, view: "list" }) }),
    },
    {
      combo: "g v",
      label: "Go to Tickets · saved views",
      group: "Navigation",
      handler: () => {
        navigate({ to: "/tickets" });
        window.dispatchEvent(new CustomEvent("nightdesk:focus-saved-views"));
      },
    },
    { combo: "g i", label: "Go to Inbox", group: "Navigation", handler: () => navigate({ to: "/inbox" }) },
    { combo: "g s", label: "Go to Scheduled", group: "Navigation", handler: () => navigate({ to: "/scheduled" }) },
    { combo: "g a", label: "Go to Archive", group: "Navigation", handler: () => navigate({ to: "/archive" }) },
    { combo: "g n", label: "Go to Analytics", group: "Navigation", handler: () => navigate({ to: "/analytics" }) },
  ];

  useKeybinds(binds);
  return null;
}
