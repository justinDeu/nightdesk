import {
  Archive,
  BarChart3,
  CalendarClock,
  Inbox,
  LayoutDashboard,
  ListTodo,
  MessagesSquare,
  Settings,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { logout } from "@/api/auth";

export interface NavEntry {
  to: string;
  label: string;
  icon: LucideIcon;
  /** exact match only (the Desk root) */
  exact?: boolean;
}

/** Primary navigation, shared by the desktop rail (SideNav) and the mobile
 *  drawer (NavDrawer) so the two never drift. */
export const ENTRIES: NavEntry[] = [
  { to: "/", label: "Desk", icon: LayoutDashboard, exact: true },
  { to: "/tickets", label: "Tickets", icon: ListTodo },
  { to: "/sessions", label: "Sessions", icon: MessagesSquare },
  { to: "/inbox", label: "Inbox", icon: Inbox },
  { to: "/scheduled", label: "Scheduled", icon: CalendarClock },
  { to: "/analytics", label: "Analytics", icon: BarChart3 },
  { to: "/archive", label: "Archive", icon: Archive },
  { to: "/settings", label: "Settings", icon: Settings },
];

/** Sign out and hard-redirect so all in-memory query state is dropped and the
 *  session cookie's absence is re-evaluated by the login route. BASE_URL is "/"
 *  in dev and "/app/" in prod. */
export async function signOut() {
  await logout();
  window.location.assign(`${import.meta.env.BASE_URL}login`);
}
