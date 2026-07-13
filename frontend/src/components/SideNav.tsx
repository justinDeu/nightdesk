import { useEffect, useState } from "react";
import { Link } from "@tanstack/react-router";
import { LogOut, PanelLeftClose, PanelLeft } from "lucide-react";
import { cn } from "@/lib/cn";
import { Tooltip } from "@/ui/Tooltip";
import { useAgentAttention } from "@/api/agents";
import { ENTRIES, signOut } from "./navEntries";
import { ProjectsNavGroup } from "./ProjectsNavGroup";

const COLLAPSE_KEY = "nightdesk:nav-collapsed";

export function SideNav() {
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem(COLLAPSE_KEY) === "1",
  );
  // Attention = structured needs-input + unread replies; shares the Desk
  // band's queryKey so the two surfaces cost one poll.
  const attention = useAgentAttention();
  const pendingCount = attention.data?.total ?? 0;

  const toggle = () => {
    setCollapsed((c) => {
      const next = !c;
      localStorage.setItem(COLLAPSE_KEY, next ? "1" : "0");
      return next;
    });
  };

  // The global "[" shortcut toggles the sidebar (dispatched from the keymap).
  useEffect(() => {
    window.addEventListener("nightdesk:toggle-sidebar", toggle);
    return () => window.removeEventListener("nightdesk:toggle-sidebar", toggle);
  }, []);

  return (
    <nav
      aria-label="Primary"
      className={cn(
        // Desktop-only rail; below md the nav lives in the TopStrip drawer.
        "hidden h-full flex-col border-r border-ink-700 bg-ink-900 transition-[width] duration-150 md:flex",
        collapsed ? "w-[60px]" : "w-[212px]",
      )}
    >
      <div
        className={cn(
          "flex h-14 items-center gap-2.5 px-3.5",
          collapsed && "justify-center px-0",
        )}
      >
        <span className="dawn-edge grid h-7 w-7 shrink-0 place-items-center rounded-[9px] text-ink-950">
          <span className="font-display text-sm font-bold">n</span>
        </span>
        {!collapsed && (
          <span className="font-display text-[15px] font-semibold tracking-tight text-moon-100">
            nightdesk
          </span>
        )}
      </div>

      <div className="flex-1 space-y-0.5 px-2.5 py-2">
        {ENTRIES.map((entry) => {
          // The Projects entry is an expandable group when the rail is open;
          // collapsed (icon-only) falls back to a plain Projects link that
          // hops to the highest-attention project (via the /projects redirect).
          if (entry.group === "projects") {
            if (collapsed) {
              const link = (
                <Link
                  key={entry.to}
                  to={entry.to}
                  activeOptions={{ exact: entry.exact }}
                  className={cn(
                    "group relative flex items-center justify-center rounded-control px-0 py-2 text-sm font-medium",
                    "text-moon-400 transition-colors duration-100",
                    "hover:bg-ink-800 hover:text-moon-100",
                  )}
                  activeProps={{
                    className:
                      "!text-moon-100 bg-ink-800 shadow-[inset_2px_0_0_var(--color-lamp)]",
                  }}
                >
                  <entry.icon size={17} className="shrink-0" />
                </Link>
              );
              return (
                <Tooltip key={entry.to} content={entry.label} side="right">
                  {link}
                </Tooltip>
              );
            }
            return <ProjectsNavGroup key={entry.to} />;
          }
          const Icon = entry.icon;
          const badge = entry.badgeKey === "agents-pending" ? pendingCount : 0;
          const link = (
            <Link
              key={entry.to}
              to={entry.to}
              activeOptions={{ exact: entry.exact }}
              className={cn(
                "group relative flex items-center gap-3 rounded-control px-2.5 py-2 text-sm font-medium",
                "text-moon-400 transition-colors duration-100",
                "hover:bg-ink-800 hover:text-moon-100",
                collapsed && "justify-center px-0",
              )}
              activeProps={{
                className:
                  "!text-moon-100 bg-ink-800 shadow-[inset_2px_0_0_var(--color-lamp)]",
              }}
            >
              <span className="relative shrink-0">
                <Icon size={17} />
                {badge > 0 && collapsed && (
                  <span className="absolute -right-1.5 -top-1.5 h-2 w-2 rounded-full bg-lamp ring-2 ring-ink-900" />
                )}
              </span>
              {!collapsed && <span>{entry.label}</span>}
              {!collapsed && badge > 0 && (
                <span className="ml-auto rounded-full bg-lamp/15 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-lamp">
                  {badge}
                </span>
              )}
            </Link>
          );
          return collapsed ? (
            <Tooltip key={entry.to} content={badge > 0 ? `${entry.label} · ${badge} waiting` : entry.label} side="right">
              {link}
            </Tooltip>
          ) : (
            link
          );
        })}
      </div>

      <div
        className={cn(
          "space-y-0.5 border-t border-ink-700 p-2.5",
          collapsed && "px-0 text-center",
        )}
      >
        <Tooltip content="Sign out" side="right">
          <button
            onClick={signOut}
            aria-label="Sign out"
            className={cn(
              "inline-flex h-8 items-center gap-2.5 rounded-control px-2.5 text-sm text-moon-400",
              "hover:bg-ink-800 hover:text-moon-100",
              collapsed ? "w-8 justify-center px-0" : "w-full",
            )}
          >
            <LogOut size={17} className="shrink-0" />
            {!collapsed && <span>Sign out</span>}
          </button>
        </Tooltip>
        <Tooltip content={collapsed ? "Expand" : "Collapse"} side="right">
          <button
            onClick={toggle}
            aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}
            className={cn(
              "inline-flex h-8 items-center gap-2.5 rounded-control px-2.5 text-sm text-moon-400",
              "hover:bg-ink-800 hover:text-moon-100",
              collapsed ? "w-8 justify-center px-0" : "w-full",
            )}
          >
            {collapsed ? <PanelLeft size={17} /> : <PanelLeftClose size={17} />}
            {!collapsed && <span>Collapse</span>}
          </button>
        </Tooltip>
      </div>
    </nav>
  );
}
