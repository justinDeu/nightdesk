import {
  createRootRoute,
  createRoute,
  createRouter,
  Link,
  Outlet,
} from "@tanstack/react-router";
import { Button } from "@/ui/Button";
import { ErrorState } from "@/ui/ErrorState";
import { AppShell } from "@/components/AppShell";
import { DeskPage } from "@/routes/desk/DeskPage";
import { TicketsPage } from "@/routes/tickets/TicketsPage";
import { TicketDetailPage } from "@/routes/tickets/TicketDetailPage";
import { RunTheater } from "@/routes/tickets/RunTheater";
import { InboxPage } from "@/routes/InboxPage";
import { ArchivePage } from "@/routes/ArchivePage";
import { ScheduledPage } from "@/routes/ScheduledPage";
import { AnalyticsPage } from "@/routes/AnalyticsPage";
import { SettingsPage } from "@/routes/SettingsPage";
import { LoginPage } from "@/routes/login";
import { KitchenSink } from "@/routes/kitchenSink";

const rootRoute = createRootRoute({
  component: () => <Outlet />,
});

// Pathless layout: everything under here renders inside the authenticated shell.
const appLayoutRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: "app",
  component: AppShell,
});

const deskRoute = createRoute({
  getParentRoute: () => appLayoutRoute,
  path: "/",
  component: DeskPage,
});

const ticketsRoute = createRoute({
  getParentRoute: () => appLayoutRoute,
  path: "/tickets",
  validateSearch: (
    search: Record<string, unknown>,
  ): { view?: "board" | "list"; f?: string; viewId?: string } => ({
    view: search.view === "list" ? "list" : search.view === "board" ? "board" : undefined,
    f: typeof search.f === "string" ? search.f : undefined,
    viewId: typeof search.viewId === "string" ? search.viewId : undefined,
  }),
  component: TicketsPage,
});

const ticketDetailRoute = createRoute({
  getParentRoute: () => appLayoutRoute,
  path: "/tickets/$id",
  component: TicketDetailPage,
});

const runTheaterRoute = createRoute({
  getParentRoute: () => appLayoutRoute,
  path: "/tickets/$id/runs/$rid",
  validateSearch: (search: Record<string, unknown>): { tab?: "transcript" | "diff" | "log" } => ({
    tab:
      search.tab === "diff" || search.tab === "log" || search.tab === "transcript"
        ? search.tab
        : undefined,
  }),
  component: RunTheater,
});

const inboxRoute = createRoute({
  getParentRoute: () => appLayoutRoute,
  path: "/inbox",
  component: InboxPage,
});

const archiveRoute = createRoute({
  getParentRoute: () => appLayoutRoute,
  path: "/archive",
  component: ArchivePage,
});

const scheduledRoute = createRoute({
  getParentRoute: () => appLayoutRoute,
  path: "/scheduled",
  component: ScheduledPage,
});

export type AnalyticsRange = "today" | "7d" | "30d";
export interface AnalyticsSearch {
  range?: AnalyticsRange;
  project?: string;
  /** Selected model ids. Serialized comma-encoded (`models=a,b`); an array in
   *  the raw params is also accepted. */
  models?: string[];
}

const analyticsRoute = createRoute({
  getParentRoute: () => appLayoutRoute,
  path: "/analytics",
  validateSearch: (search: Record<string, unknown>): AnalyticsSearch => {
    const range =
      search.range === "today" || search.range === "7d" || search.range === "30d"
        ? search.range
        : undefined;
    const raw = search.models;
    let models: string[] | undefined;
    if (Array.isArray(raw)) {
      models = raw.map(String).filter(Boolean);
    } else if (typeof raw === "string" && raw.length > 0) {
      models = raw.split(",").filter(Boolean);
    }
    return {
      range,
      project: typeof search.project === "string" && search.project ? search.project : undefined,
      models: models && models.length > 0 ? models : undefined,
    };
  },
  component: AnalyticsPage,
});

const settingsIndexRoute = createRoute({
  getParentRoute: () => appLayoutRoute,
  path: "/settings",
  component: SettingsPage,
});

const settingsSectionRoute = createRoute({
  getParentRoute: () => appLayoutRoute,
  path: "/settings/$section",
  component: SettingsPage,
});

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  validateSearch: (search: Record<string, unknown>): { redirect?: string } => ({
    redirect: typeof search.redirect === "string" ? search.redirect : undefined,
  }),
  component: LoginPage,
});

const kitchenSinkRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/dev/kitchen-sink",
  component: KitchenSink,
});

const routeTree = rootRoute.addChildren([
  appLayoutRoute.addChildren([
    deskRoute,
    ticketsRoute,
    ticketDetailRoute,
    runTheaterRoute,
    inboxRoute,
    archiveRoute,
    scheduledRoute,
    analyticsRoute,
    settingsIndexRoute,
    settingsSectionRoute,
  ]),
  loginRoute,
  kitchenSinkRoute,
]);

/** Full-viewport centered error surface for the router boundaries. */
function RouterBoundary({
  title,
  description,
}: {
  title: string;
  description: string;
}) {
  return (
    <div className="grid min-h-screen place-items-center bg-ink-950 px-4">
      <ErrorState
        className="w-full max-w-md"
        title={title}
        description={description}
        action={
          <Button asChild variant="primary">
            <Link to="/">Back to desk</Link>
          </Button>
        }
      />
    </div>
  );
}

export const router = createRouter({
  // BASE_URL is "/app/" in prod builds (FastAPI mounts the SPA at /app) and "/"
  // in dev, so dev behavior is unchanged.
  basepath: import.meta.env.BASE_URL,
  routeTree,
  defaultPreload: "intent",
  defaultNotFoundComponent: () => (
    <RouterBoundary
      title="Page not found"
      description="That route doesn't exist. It may have moved, or the link is out of date."
    />
  ),
  defaultErrorComponent: () => (
    <RouterBoundary
      title="Something went wrong"
      description="An unexpected error interrupted this page. Head back to the desk and try again."
    />
  ),
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
