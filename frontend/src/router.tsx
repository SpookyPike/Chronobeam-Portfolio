import { createRootRoute, createRoute, createRouter } from "@tanstack/react-router";

import { DashboardPage } from "./pages/dashboard";
import { AccessRequestsPage } from "./pages/access-requests";
import { AdminPage, adminTabSearch } from "./pages/admin";
import { MonthWorkspace } from "./pages/month";
import { NotFoundPage } from "./pages/not-found";
import { OcdPage } from "./pages/ocd";
import { OvertimePage } from "./pages/overtime";
import { PrivacyPage } from "./pages/privacy";
import { ProfilePage } from "./pages/profile";
import { RecipientQueuePage } from "./pages/recipient-queue";
import { MonthReviewPage } from "./pages/review";
import { SubmissionsPage } from "./pages/submissions";
import { TravelPage } from "./pages/travel";
import { Shell } from "./shell";

const rootRoute = createRootRoute({
  component: Shell,
  notFoundComponent: NotFoundPage,
});

const dashboardRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: DashboardPage,
});

/** UI-046: `?day=YYYY-MM-DD` opens the month workspace on one day. Only a
 *  well-formed date in the month's own shape is accepted; anything else is
 *  ignored so a hand-edited URL cannot put the panel on a foreign date. */
export function monthDaySearch(search: Record<string, unknown>): { day?: string } {
  return typeof search.day === "string" && /^\d{4}-\d{2}-\d{2}$/.test(search.day)
    ? { day: search.day }
    : {};
}

const monthRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/month/$month",
  // UI-046: a review finding can name the day it belongs to, so the workspace
  // opens on that day instead of always on the 1st. Same pattern as the admin
  // tab search param (UI-025).
  validateSearch: monthDaySearch,
  component: () => {
    const { month } = monthRoute.useParams();
    const { day } = monthRoute.useSearch();
    return <MonthWorkspace month={month} initialDay={day} />;
  },
});

const monthReviewRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/month/$month/review",
  component: () => {
    const { month } = monthReviewRoute.useParams();
    return <MonthReviewPage month={month} />;
  },
});

const monthOcdRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/month/$month/ocd",
  component: () => {
    const { month } = monthOcdRoute.useParams();
    return <OcdPage month={month} />;
  },
});

const monthOvertimeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/month/$month/overtime",
  component: () => {
    const { month } = monthOvertimeRoute.useParams();
    return <OvertimePage month={month} />;
  },
});

const monthSubmissionsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/month/$month/submissions",
  component: () => {
    const { month } = monthSubmissionsRoute.useParams();
    return <SubmissionsPage month={month} />;
  },
});

const profileRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/profile",
  component: ProfilePage,
});

const submissionsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/share",
  component: SubmissionsPage,
});
const travelRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/travel/$period",
  component: () => {
    const { period } = travelRoute.useParams();
    return <TravelPage period={period} />;
  },
});
const accessRequestsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/access-requests",
  component: AccessRequestsPage,
});
const recipientQueueRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/recipient-queue",
  component: RecipientQueuePage,
});
const adminRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/admin",
  // UI-025: the selected panel is addressable (`/admin?tab=system`).
  validateSearch: adminTabSearch,
  component: AdminPage,
});

// DP-RIGHTS-003 / FE-PRIV-006: the privacy notice is available to every
// authenticated account, independent of capabilities, so it is not gated by
// the capability-driven navigation.
const privacyRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/privacy",
  component: PrivacyPage,
});

export const routeTree = rootRoute.addChildren([
  dashboardRoute,
  monthRoute,
  monthReviewRoute,
  monthOcdRoute,
  monthOvertimeRoute,
  monthSubmissionsRoute,
  profileRoute,
  submissionsRoute,
  travelRoute,
  accessRequestsRoute,
  recipientQueueRoute,
  adminRoute,
  privacyRoute,
]);

export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
