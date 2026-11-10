import { Link, Outlet, useLocation } from "@tanstack/react-router";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import {
  ApiError,
  fetchAuthSession,
  fetchIdentity,
  logout,
  setSessionExpiredHandler,
} from "./lib/api";
import { currentBerlinMonth } from "./pages/month";

/** UI-049: one section name per route, so the tab, the history entry and a
 *  pasted link all say where the reader is. The month sub-routes are checked
 *  before the month workspace because their paths are prefixes of each other. */
function documentTitle(pathname: string): string {
  const section = (() => {
    if (pathname === "/") return "Home";
    if (pathname.startsWith("/month/")) {
      if (pathname.endsWith("/review")) return "Month review";
      if (pathname.endsWith("/ocd")) return "On-call duty";
      if (pathname.endsWith("/overtime")) return "Overtime";
      if (pathname.endsWith("/submissions")) return "Submissions";
      return "Month workspace";
    }
    if (pathname.startsWith("/travel/")) return "Business travel";
    const exact: Record<string, string> = {
      "/profile": "Profile & schedule",
      "/share": "Submissions & Sharing",
      "/access-requests": "Access Requests",
      "/recipient-queue": "Recipient queue",
      "/admin": "Administration",
      "/privacy": "Privacy notice",
    };
    return exact[pathname];
  })();
  return section ? `${section} — Chronobeam` : "Chronobeam";
}

/** Shell: brand + one deduplicated capability-driven navigation,
 *  identity context chip, active-route markers and scroll restoration.
 *  Work-area links never duplicate (F06): a destination is rendered once,
 *  employee and role areas are grouped visually by the chip label. */
export function Shell() {
  const auth = useQuery({ queryKey: ["auth-session"], queryFn: fetchAuthSession, retry: false });
  const identity = useQuery({ queryKey: ["identity"], queryFn: fetchIdentity });
  const location = useLocation();
  const [sessionExpired, setSessionExpired] = useState(false);
  // UI-005: on a phone the navigation is a collapsed menu. The state lives
  // here so the toggle, the nav and every link agree on it.
  const [navOpen, setNavOpen] = useState(false);
  const navToggle = useRef<HTMLButtonElement>(null);
  const capabilities = new Set(identity.data?.capabilities ?? []);
  const isEmployee = capabilities.has("OWN_LEDGER_READ");
  const canCreateRequests = capabilities.has("ACCESS_REQUEST_CREATE");
  const canReadPackages = capabilities.has("SUBMISSION_READ_ASSIGNED");
  const canAdminister = capabilities.has("ACCESS_ADMIN") || capabilities.has("SYSTEM_STATUS_READ");

  // F10: every route change starts at the top of the page (no stale scroll
  // positions hiding the header after navigation or tab switches).
  useEffect(() => {
    window.scrollTo(0, 0);
  }, [location.href]);

  // UI-005: the collapsed menu never survives a route change, whichever way the
  // change happened (a link, the back button, a pasted URL).
  useEffect(() => {
    setNavOpen(false);
  }, [location.pathname]);

  // UI-049: the document title is the only label a screen-reader user, a tab
  // strip or a history entry gets for "where am I" before the page is read.
  useEffect(() => {
    document.title = documentTitle(location.pathname);
  }, [location.pathname]);

  // The handler records the expiry instead of refetching straight into the
  // sign-in view: a refetch would flip `authenticated` and unmount the routed
  // subtree, destroying whatever the user was editing.
  useEffect(() => {
    setSessionExpiredHandler(() => setSessionExpired(true));
    return () => setSessionExpiredHandler(undefined);
  }, []);

  // CSRF bootstrap gate. `csrfToken` is module state that `fetchAuthSession`
  // fills in, and this component used to render <Outlet/> immediately — so a
  // fast interaction could issue a mutation without X-CSRF-Token, which
  // production rejects with 403 CSRF_REJECTED.
  //
  // The gate covers the ERROR case too, not just the pending one: a transient
  // failure of `/auth/session` leaves `data` undefined, and rendering the work
  // area then would allow exactly the headerless mutation this exists to
  // prevent. Fail closed, with a retry.
  if (auth.data === undefined) {
    return (
      <main className="sign-in-view" aria-busy={auth.isPending}>
        <h1>Chronobeam</h1>
        {auth.isPending ? (
          <p>Preparing your session…</p>
        ) : (
          <>
            <p role="alert">Your session could not be checked. Nothing was sent.</p>
            <button type="button" className="button" onClick={() => void auth.refetch()}>
              Try again
            </button>
          </>
        )}
      </main>
    );
  }

  const identityExpired = identity.error instanceof ApiError && identity.error.status === 401;
  const unauthenticated = Boolean(
    auth.data?.oidc_mode && (!auth.data.authenticated || identityExpired),
  );

  // Session expiry is NOT the same event as "not signed in at boot".
  //
  // At boot there is nothing to lose, so the sign-in view is the right answer.
  // Once the user has been working, a 401 means the session lapsed underneath
  // them: editors hold drafts in component state and nothing is written to
  // localStorage on purpose, so the tree stays mounted and the user gets an
  // explicit, non-destructive way back.
  if (unauthenticated && !sessionExpired) {
    const returnTo = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    return (
      <main className="sign-in-view">
        <h1>Sign in to Chronobeam</h1>
        <p>Your work-time record remains private by default.</p>
        <a
          className="button"
          href={`/api/v1/auth/login?return_to=${encodeURIComponent(returnTo.startsWith("/") ? returnTo : "/")}`}
        >
          Sign in
        </a>
      </main>
    );
  }

  // The notice is driven by the 401 itself, not by the session query's cached
  // answer: the query is deliberately NOT refetched on expiry (a refetch would
  // flip `authenticated` and unmount the work area), so `auth.data` still says
  // "authenticated" while the server has already said no.
  const showExpiredNotice = sessionExpired;

  const currentMonth = currentBerlinMonth();
  // UI-004: these links used to be pinned to TODAY's month, so working in July
  // marked nothing in the header as current and a click on `Month` silently
  // jumped to September. The month already in the URL wins when there is one.
  const routeMonth = /^\/(?:month|travel)\/(\d{4}-\d{2})/.exec(location.pathname)?.[1];
  const monthInContext = routeMonth ?? currentMonth;
  const links: Array<{ to: string; label: string; params?: Record<string, string> }> = [];
  const seen = new Set<string>();
  const add = (to: string, label: string, params?: Record<string, string>) => {
    if (seen.has(`${to}${params ? JSON.stringify(params) : ""}`)) return;
    seen.add(`${to}${params ? JSON.stringify(params) : ""}`);
    links.push({ to, label, params });
  };
  if (isEmployee) {
    add("/", "Home");
    // Prefix matching (the default for every link except `Home`) is what keeps
    // `Month` lit across `/month/$month/*`; `Home` is the one exact link.
    add(`/month/${monthInContext}`, "Month", { month: monthInContext });
    add(`/travel/${monthInContext}`, "Travel", { period: monthInContext });
    add("/share", "Submissions & Sharing");
    add("/access-requests", "Access Requests");
    add("/profile", "Profile");
  }
  if (canCreateRequests) add("/access-requests", "Access Requests");
  if (canReadPackages) add("/recipient-queue", "Recipient queue");
  if (canAdminister) add("/admin", "Administration");

  // The chip names the actual role instead of a generic "role work": a
  // recipient reading "Employee + role work" cannot tell what they hold, and
  // the role name is what the nav grouping already means.
  const roleNames: string[] = [];
  if (canCreateRequests) roleNames.push("HR");
  if (canReadPackages) roleNames.push("Recipient");
  if (canAdminister) roleNames.push("Administration");

  const workArea = isEmployee
    ? roleNames.length > 0
      ? `Employee · ${roleNames.join(" · ")}`
      : "Employee"
    : canCreateRequests
      ? "HR requests"
      : canReadPackages
        ? "Recipient"
        : canAdminister
          ? "Administration"
          : "Limited access";

  return (
    <div className="app-shell">
      <header
        className="app-header"
        // UI-005: Escape closes the menu from wherever focus is inside the
        // header — including the toggle itself, which is outside the nav.
        onKeyDown={(event) => {
          if (event.key !== "Escape" || !navOpen) return;
          setNavOpen(false);
          navToggle.current?.focus();
        }}
      >
        {/* UI-048: first focusable element on the page, so one Tab reaches it.
            Without it every route change costs a keyboard user the whole
            header, which the scroll-to-top on navigation makes worse. */}
        <a className="skip-link" href="#main">
          Skip to content
        </a>
        <Link to="/" className="app-brand">
          <img src="/logo.png" alt="Chronobeam" />
        </Link>
        {/* UI-005: below 700px the nav is this button's menu, so the header is
            one row instead of four wrapped ones. Above 700px the button is
            hidden and the nav is always visible. */}
        <button
          type="button"
          ref={navToggle}
          className="nav-toggle"
          aria-expanded={navOpen}
          aria-controls="main-nav"
          onClick={() => setNavOpen((open) => !open)}
        >
          Menu
        </button>
        <nav id="main-nav" aria-label="Main" data-open={navOpen ? "true" : "false"}>
          {links.map((link) => (
            <Link
              key={`${link.to}-${link.label}`}
              to={link.to}
              params={link.params as never}
              activeOptions={{ exact: link.to === "/" }}
              activeProps={{ "aria-current": "page" as const }}
              // Closing the menu hides the link that was just clicked, so focus
              // moves into the content rather than being dropped to <body>.
              onClick={() => {
                if (!navOpen) return;
                setNavOpen(false);
                document.getElementById("main")?.focus();
              }}
            >
              {link.label}
            </Link>
          ))}
        </nav>
        {identity.data && (
          // UI-026: a `div` has no role that supports naming, so the label was
          // dropped by assistive technology while five tests still query it.
          // `section` + a name is a real `region` landmark; the label is kept.
          <section className="identity-chip" aria-label="Current account">
            <strong>{identity.data.display_name}</strong>
            <span>{workArea}</span>
          </section>
        )}
        {auth.data?.oidc_mode && auth.data.authenticated && (
          <button type="button" onClick={() => void logout().then(() => void auth.refetch())}>
            Sign out
          </button>
        )}
      </header>
      {showExpiredNotice && (
        <div className="session-expired-notice" role="alert">
          <strong>Your session has expired.</strong> Nothing you typed has been discarded — copy
          anything you still need before signing in again.
          <a
            className="button"
            href={`/api/v1/auth/login?return_to=${encodeURIComponent(
              `${window.location.pathname}${window.location.search}`.startsWith("/")
                ? `${window.location.pathname}${window.location.search}`
                : "/",
            )}`}
          >
            Sign in again
          </a>
        </div>
      )}
      {/* UI-048: `tabIndex={-1}` is what makes the skip link land — a bare
          fragment link moves the scroll position and the sequential-focus
          start point but leaves focus on <body>, so nothing is announced.
          Not in the tab order: reachable only through the skip link. */}
      <main id="main" tabIndex={-1}>
        <Outlet />
      </main>
      <footer>
        {/* 7B: the employee's privacy reassurance used to appear three times on
            one screen — the page eyebrow ("Private — visible only to you"), the
            Home subhead, and this footer sentence. The eyebrow is on all seven
            employee pages and the Home subhead covers the landing page, so the
            third statement is the one that goes. The link stays: it is the only
            always-visible route to the notice, deliberately outside the
            capability-driven nav. */}
        {isEmployee
          ? null
          : "Access is limited to the scopes explicitly assigned to this account. "}
        {/* DP-RIGHTS-003 / FE-PRIV-006: stable, always-visible link to the
            privacy notice, deliberately outside the capability-driven nav so
            every authenticated account can reach it. */}
        <Link to="/privacy">Privacy / Data protection</Link>
      </footer>
    </div>
  );
}
