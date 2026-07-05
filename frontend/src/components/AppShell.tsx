import { useEffect } from "react";
import { Outlet, useNavigate, useRouterState } from "@tanstack/react-router";
import { UNAUTHORIZED_EVENT } from "@/api/client";
import { SideNav } from "./SideNav";
import { TopStrip } from "./TopStrip";
import { Composer } from "./Composer";
import { CommandPalette } from "./CommandPalette";
import { Cheatsheet } from "./Cheatsheet";
import { GlobalKeybinds } from "./GlobalKeybinds";

/** The authenticated app frame: slim nav + top strip + routed content.
 *  Listens for the global 401 event and bounces to /login. */
export function AppShell() {
  const navigate = useNavigate();
  const pathname = useRouterState({ select: (s) => s.location.pathname });

  useEffect(() => {
    const onUnauthorized = () => {
      navigate({ to: "/login", search: { redirect: pathname } });
    };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, [navigate, pathname]);

  return (
    <div className="flex h-screen w-screen overflow-hidden">
      <SideNav />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopStrip />
        <main className="min-h-0 flex-1 overflow-auto">
          <Outlet />
        </main>
      </div>

      {/* App-wide overlays + global shortcuts. */}
      <GlobalKeybinds />
      <Composer />
      <CommandPalette />
      <Cheatsheet />
    </div>
  );
}
