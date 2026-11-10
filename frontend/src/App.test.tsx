import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createRouter } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { request } from "./lib/api";
import { routeTree } from "./router";

/** Shell/home tests: capability-driven start page, one
 *  deduplicated nav + identity chip, in-shell 404. */

const JSON_HEADERS = { "Content-Type": "application/json" };

function monthPayload(closingBalanceMinutes = 42): unknown {
  return {
    period_month: "2026-09-01",
    status: "OPEN",
    interval_count: 5,
    closing_balance_minutes: closingBalanceMinutes,
    days: [],
    daily_results: [],
  };
}

function stubIdentity(
  capabilities: string[],
  displayName = "Mara Klein",
  closingBalanceMinutes = 42,
): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/v1/auth/session")) {
        return new Response(
          JSON.stringify({ authenticated: true, csrf_token: null, oidc_mode: false }),
          { status: 200, headers: JSON_HEADERS },
        );
      }
      if (url.endsWith("/api/v1/me")) {
        return new Response(
          JSON.stringify({ subject: "demo-employee", display_name: displayName, capabilities }),
          {
            status: 200,
            headers: JSON_HEADERS,
          },
        );
      }
      if (url.includes("/api/v1/me/months/")) {
        return new Response(JSON.stringify(monthPayload(closingBalanceMinutes)), {
          status: 200,
          headers: JSON_HEADERS,
        });
      }
      // The month pages gate their body (and therefore the month navigation)
      // behind their own queries, so a route entered directly needs them
      // answered rather than 404'd. Every shape here is the empty one.
      if (url.includes("/api/v1/me/findings")) {
        return new Response(JSON.stringify({ findings: [] }), {
          status: 200,
          headers: JSON_HEADERS,
        });
      }
      if (url.includes("/api/v1/me/ocd?") || url.includes("/api/v1/me/overtime-authorizations?")) {
        return new Response(JSON.stringify([]), { status: 200, headers: JSON_HEADERS });
      }
      if (url.includes("/api/v1/me/shifts?")) {
        return new Response(JSON.stringify([]), { status: 200, headers: JSON_HEADERS });
      }
      if (url.includes("/api/v1/me/day-status?")) {
        return new Response(JSON.stringify({ entries: [] }), {
          status: 200,
          headers: JSON_HEADERS,
        });
      }
      if (url.includes("/api/v1/me/profile")) {
        return new Response(JSON.stringify({ preferences: {} }), {
          status: 200,
          headers: JSON_HEADERS,
        });
      }
      // Unmatched endpoints fail cleanly so pages render error states
      // instead of crashing on unexpected shapes.
      return new Response(JSON.stringify({}), { status: 404, headers: JSON_HEADERS });
    }),
  );
}

function renderShell() {
  const queryClient = new QueryClient();
  // A fresh router per test isolates navigation state (the route tree is
  // shared, the router instance is not).
  const router = createRouter({ routeTree });
  return render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

describe("Chronobeam shell and home", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubIdentity([
      "OWN_LEDGER_READ",
      "OWN_LEDGER_WRITE",
      "OWN_SUBMISSION_CREATE",
      "OWN_DISCLOSURE_GRANT",
    ]);
    window.history.replaceState({}, "", "/");
  });

  it("renders the brand, deduplicated employee navigation and identity chip", async () => {
    renderShell();
    await screen.findByLabelText("Current account");
    expect(screen.getByRole("img", { name: "Chronobeam" })).toHaveAttribute("src", "/logo.png");
    const nav = screen.getByRole("navigation", { name: "Main" });
    // Employee destinations, each exactly once in the navigation.
    expect(within(nav).getAllByRole("link", { name: "Month" })).toHaveLength(1);
    expect(within(nav).getAllByRole("link", { name: "Travel" })).toHaveLength(1);
    expect(within(nav).getAllByRole("link", { name: "Submissions & Sharing" })).toHaveLength(1);
    expect(within(nav).getAllByRole("link", { name: "Access Requests" })).toHaveLength(1);
    // Identity chip shows the person and their work area.
    expect(screen.getByLabelText("Current account")).toHaveTextContent("Mara Klein");
    expect(screen.getByLabelText("Current account")).toHaveTextContent("Employee");
  });

  it("home greets the employee with the month-named balance and one navigation system", async () => {
    renderShell();
    await screen.findByRole("heading", { name: /Welcome back, Mara/ });
    // UI-024: the balance names the month it belongs to instead of leaving the
    // reader to connect "month end" to the heading above it.
    expect(await screen.findByText(/Balance at the end of \w+ 2026: \+0:42/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open this month’s workspace" })).toBeInTheDocument();
    // UI-024: the four-link "Your private areas" row is gone — the header is the
    // single navigation system on "/", and every destination is still reachable
    // from it.
    const nav = screen.getByRole("navigation", { name: "Main" });
    for (const name of [
      "Home",
      "Month",
      "Travel",
      "Submissions & Sharing",
      "Access Requests",
      "Profile",
    ]) {
      expect(within(nav).getByRole("link", { name })).toBeInTheDocument();
    }
    expect(screen.queryByText("Your private areas")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Business travel" })).not.toBeInTheDocument();
    // No phase language, no backend-service diagnostics.
    expect(screen.queryByText(/backend health|chronobeam-backend/i)).not.toBeInTheDocument();
  });

  it("explains a minus balance beside the number, not only inside a day cell", async () => {
    // UI-024: the negative balance was the app's largest emotional spike and the
    // only thing explaining it lived inside one day cell. The seeded demo month is
    // positive, so the negative case is pinned here rather than left unverified.
    stubIdentity(["OWN_LEDGER_READ"], "Mara Klein", -114);
    renderShell();
    await screen.findByRole("heading", { name: /Welcome back, Mara/ });
    expect(await screen.findByText(/Balance at the end of \w+ 2026: −1:54/)).toBeInTheDocument();
    expect(
      screen.getByText(/A minus balance means less time recorded than required/),
    ).toBeInTheDocument();
  });

  it("marks the active route with aria-current", async () => {
    renderShell();
    const monthLink = await screen.findByRole("link", { name: "Month" });
    // The home route is active at "/"; navigate to /profile to activate it.
    window.history.pushState({}, "", "/profile");
    window.dispatchEvent(new PopStateEvent("popstate"));
    await waitFor(() => {
      const profileLink = screen.getByRole("link", { name: "Profile" });
      expect(profileLink.getAttribute("aria-current")).toBe("page");
      expect(monthLink.getAttribute("aria-current")).not.toBe("page");
    });
  });

  it("opens the current month workspace from the home dashboard without crashing", async () => {
    // Regression: the dashboard used to cache its lean {month, summary}
    // projection under the same query key as the month workspace's full
    // payload; entering the workspace afterwards rendered an incompatible
    // cached shape and crashed ("Cannot read properties of undefined
    // (reading 'map')"). Home -> workspace must render normally.
    renderShell();
    await screen.findByRole("heading", { name: /Welcome back, Mara/ });
    await screen.findByText(/Balance at the end of \w+ 2026: \+0:42/);
    window.history.pushState({}, "", "/month/2026-09");
    window.dispatchEvent(new PopStateEvent("popstate"));
    await screen.findByRole("heading", { name: "September 2026" });
    expect(screen.queryByText("Something went wrong!")).not.toBeInTheDocument();
  });

  it("renders a recovery 404 for unknown routes", async () => {
    renderShell();
    await screen.findByLabelText("Current account");
    window.history.pushState({}, "", "/no-such-page");
    window.dispatchEvent(new PopStateEvent("popstate"));
    await screen.findByRole("heading", { name: "This address does not exist" });
    expect(screen.getByText("Page not found")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to Home" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open this month’s workspace" })).toBeInTheDocument();
  });
});

describe("month context navigation", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubIdentity([
      "OWN_LEDGER_READ",
      "OWN_LEDGER_WRITE",
      "OWN_SUBMISSION_CREATE",
      "OWN_DISCLOSURE_GRANT",
    ]);
  });

  // UI-001 regression guard: `Link` marks itself current on a PREFIX match by
  // default, so every month sub-route used to leave `Month` current alongside
  // the section the reader was really on. `/month/2026-09` is the current
  // month, where the `This month` step is also a current destination — only
  // one of the two may draw the section pill.
  it.each([
    ["/month/2026-09", "Month"],
    ["/month/2026-07", "Month"],
    ["/month/2026-07/review", "Review this month"],
    ["/month/2026-07/ocd", "On-call duty"],
    ["/month/2026-07/overtime", "Overtime"],
    ["/month/2026-07/submissions", "Submissions"],
    ["/share", "Submissions"],
  ])("marks exactly one month-context section current on %s", async (path, label) => {
    window.history.replaceState({}, "", path);
    renderShell();
    const nav = await screen.findByRole("navigation", { name: "Month navigation" });
    const current = [...nav.querySelectorAll('a.month-tab[aria-current="page"]')];
    expect(current.map((a) => a.textContent)).toEqual([label]);
    // The step controls are a separate set and never draw the section pill.
    expect([...nav.querySelectorAll("a.month-step:not(.month-tab)")]).toHaveLength(3);
  });
});

describe("collapsed navigation", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubIdentity([
      "OWN_LEDGER_READ",
      "OWN_LEDGER_WRITE",
      "OWN_SUBMISSION_CREATE",
      "OWN_DISCLOSURE_GRANT",
    ]);
    window.history.replaceState({}, "", "/");
  });

  // UI-005: on a phone the nav is a menu. jsdom applies no media queries, so
  // this asserts the behaviour the ≤700px CSS reveals, not the CSS itself —
  // including the two states a screenshot cannot show (Escape, and where focus
  // ends up when the menu closes).
  it("opens and closes the navigation as a menu and never strands focus", async () => {
    const user = userEvent.setup();
    renderShell();
    await screen.findByLabelText("Current account");
    const toggle = screen.getByRole("button", { name: "Menu" });
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveAttribute("aria-controls", "main-nav");
    expect(nav).toHaveAttribute("id", "main-nav");
    expect(nav).toHaveAttribute("data-open", "false");

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(nav).toHaveAttribute("data-open", "true");

    // Escape closes it and hands focus back to the toggle.
    await user.keyboard("{Escape}");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveFocus();

    // Following a link closes it too — including the link to the route already
    // open, which the route-change effect cannot catch. Focus moves into the
    // content: the menu is hidden the moment it closes, so leaving focus on the
    // link would drop it to <body>.
    await user.click(toggle);
    await user.click(within(nav).getByRole("link", { name: "Home" }));
    await waitFor(() => expect(toggle).toHaveAttribute("aria-expanded", "false"));
    expect(screen.getByRole("main")).toHaveFocus();
  });
});

describe("role and limited personas", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    window.history.replaceState({}, "", "/");
  });

  it("HR requester home offers requests and never duplicates the nav entry", async () => {
    stubIdentity(["ACCESS_REQUEST_CREATE", "DISCLOSURE_READ_GRANTED"], "Lena Hoffmann");
    renderShell();
    await screen.findByRole("heading", { name: /Welcome, Lena/ });
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(await within(nav).findAllByRole("link", { name: "Access Requests" })).toHaveLength(1);
    expect(screen.getByLabelText("Current account")).toHaveTextContent("HR requests");
  });

  it("admin home offers Administration", async () => {
    stubIdentity(["ACCESS_ADMIN", "SYSTEM_STATUS_READ"], "Nils Brandt");
    renderShell();
    await screen.findByRole("heading", { name: /Welcome, Nils/ });
    const nav = screen.getByRole("navigation", { name: "Main" });
    expect(await within(nav).findAllByRole("link", { name: "Administration" })).toHaveLength(1);
    expect(screen.getByLabelText("Current account")).toHaveTextContent("Administration");
  });

  it("capability-less account gets a calm limited-access home", async () => {
    stubIdentity([], "Guest");
    renderShell();
    await screen.findByRole("heading", { name: /Welcome, Guest/ });
    expect(screen.getByText(/No work area is assigned/)).toBeInTheDocument();
    expect(
      screen.getByText(/Access is limited to the scopes explicitly assigned/),
    ).toBeInTheDocument();
  });

  it("shows institutional sign-in only when production session mode is unauthenticated", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        if (String(input).endsWith("/api/v1/auth/session")) {
          return new Response(
            JSON.stringify({ authenticated: false, csrf_token: null, oidc_mode: true }),
            {
              status: 200,
              headers: JSON_HEADERS,
            },
          );
        }
        return new Response(JSON.stringify({}), { status: 404, headers: JSON_HEADERS });
      }),
    );
    window.history.replaceState({}, "", "/month/2026-09?view=day");
    renderShell();
    const link = await screen.findByRole("link", { name: "Sign in" });
    expect(link.getAttribute("href")).toContain("return_to=%2Fmonth%2F2026-09%3Fview%3Dday");
  });

  it("signs out of a production session, sends the CSRF token, and returns to sign-in", async () => {
    let authenticated = true;
    let logoutHeaders: Headers | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.endsWith("/api/v1/auth/session")) {
          return new Response(
            JSON.stringify({
              authenticated,
              csrf_token: authenticated ? "csrf-1" : null,
              oidc_mode: true,
            }),
            { status: 200, headers: JSON_HEADERS },
          );
        }
        if (url.endsWith("/api/v1/auth/logout")) {
          logoutHeaders =
            init?.headers instanceof Headers ? init.headers : new Headers(init?.headers);
          authenticated = false;
          return new Response(null, { status: 204 });
        }
        if (url.endsWith("/api/v1/me")) {
          if (!authenticated)
            return new Response(JSON.stringify({}), { status: 401, headers: JSON_HEADERS });
          return new Response(
            JSON.stringify({
              subject: "demo-employee",
              display_name: "Mara Klein",
              capabilities: [
                "OWN_LEDGER_READ",
                "OWN_LEDGER_WRITE",
                "OWN_SUBMISSION_CREATE",
                "OWN_DISCLOSURE_GRANT",
              ],
            }),
            { status: 200, headers: JSON_HEADERS },
          );
        }
        if (url.includes("/api/v1/me/months/")) {
          return new Response(JSON.stringify(monthPayload()), {
            status: 200,
            headers: JSON_HEADERS,
          });
        }
        return new Response(JSON.stringify({}), { status: 404, headers: JSON_HEADERS });
      }),
    );
    renderShell();
    await screen.findByLabelText("Current account");
    const signOut = await screen.findByRole("button", { name: "Sign out" });
    signOut.click();
    await waitFor(() => expect(logoutHeaders?.get("X-CSRF-Token")).toBe("csrf-1"));
    expect(await screen.findByRole("link", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
  });
});

describe("Shell session bootstrap and expiry (F7)", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    window.history.replaceState({}, "", "/");
  });

  it("renders nothing routed until the session and its CSRF token resolve", async () => {
    // The session query never settles, so the shell is still bootstrapping.
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => {})),
    );
    renderShell();
    expect(await screen.findByText(/Preparing your session/)).toBeInTheDocument();
    // No routed content means no editor, and therefore no mutation that could
    // be sent without X-CSRF-Token (production answers those with 403).
    expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull();
    expect(screen.queryByText(/Private by default/)).toBeNull();
  });

  it("keeps the work area mounted when the session expires mid-work", async () => {
    // The FIRST session read is authenticated; any later read says otherwise, so
    // a handler that refetches (the old behaviour) is observably different from
    // one that only records the expiry.
    let sessionReads = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/auth/session")) {
          sessionReads += 1;
          return new Response(
            JSON.stringify({
              authenticated: sessionReads === 1,
              csrf_token: "csrf-1",
              oidc_mode: true,
            }),
            { status: 200, headers: JSON_HEADERS },
          );
        }
        if (url.endsWith("/api/v1/me")) {
          return new Response(
            JSON.stringify({
              subject: "demo-employee",
              display_name: "Mara Klein",
              capabilities: ["OWN_LEDGER_READ"],
            }),
            { status: 200, headers: JSON_HEADERS },
          );
        }
        if (url.includes("/api/v1/me/months/")) {
          return new Response(JSON.stringify(monthPayload()), {
            status: 200,
            headers: JSON_HEADERS,
          });
        }
        if (url.endsWith("/api/v1/me/late")) {
          return new Response(
            JSON.stringify({
              detail: { code: "AUTH_REQUIRED", message: "Authentication required." },
            }),
            { status: 401, headers: JSON_HEADERS },
          );
        }
        return new Response(JSON.stringify({}), { status: 404, headers: JSON_HEADERS });
      }),
    );
    renderShell();
    const greeting = await screen.findByRole("heading", { name: /Welcome back, Mara/ });

    // A later request is refused: the shell is told the session lapsed.
    await request("/api/v1/me/late", { method: "POST" }).catch(() => undefined);

    expect(await screen.findByRole("alert")).toHaveTextContent(/session has expired/i);
    // The work area is STILL MOUNTED. Unmounting it would discard whatever the
    // employee was editing, and nothing is written to localStorage on purpose.
    expect(screen.getByRole("heading", { name: /Welcome back, Mara/ })).toBe(greeting);
    expect(screen.queryByRole("heading", { name: /Sign in to Chronobeam/ })).toBeNull();
  });

  it("fails closed when the session cannot be checked, and offers a retry", async () => {
    // A REJECTED session request leaves the token unknown. Rendering the work
    // area here would allow a mutation with no X-CSRF-Token.
    let attempts = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/auth/session")) {
          attempts += 1;
          if (attempts === 1) return new Response("boom", { status: 503 });
          return new Response(
            JSON.stringify({ authenticated: true, csrf_token: "csrf-1", oidc_mode: false }),
            { status: 200, headers: JSON_HEADERS },
          );
        }
        if (url.endsWith("/api/v1/me")) {
          return new Response(
            JSON.stringify({
              subject: "demo-employee",
              display_name: "Mara Klein",
              capabilities: ["OWN_LEDGER_READ"],
            }),
            { status: 200, headers: JSON_HEADERS },
          );
        }
        if (url.includes("/api/v1/me/months/")) {
          return new Response(JSON.stringify(monthPayload()), {
            status: 200,
            headers: JSON_HEADERS,
          });
        }
        return new Response(JSON.stringify({}), { status: 404, headers: JSON_HEADERS });
      }),
    );
    renderShell();
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not be checked/i);
    // No routed content, so no editor can exist to send a headerless mutation.
    expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull();

    // The retry is a real way forward, not a dead end.
    await userEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(await screen.findByRole("heading", { name: /Welcome back, Mara/ })).toBeInTheDocument();
  });
});
