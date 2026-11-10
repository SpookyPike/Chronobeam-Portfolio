import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  Outlet,
  RouterProvider,
  createRootRoute,
  createRoute,
  createRouter,
} from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AdminPage, adminTabSearch } from "./admin";
import { AdminSystemPage } from "./admin-system";
import { AdminUsersPage } from "./admin-users";

vi.mock("@tanstack/react-router", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@tanstack/react-router")>()),
  Link: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

function renderPage(page: ReactNode) {
  return render(<QueryClientProvider client={new QueryClient()}>{page}</QueryClientProvider>);
}

/** UI-025: AdminPage takes its selected panel from the route search param, so
 *  the tests need a real router. The route reuses the same exported validator
 *  the application route uses, so the URL contract under test is the real one. */
function renderAdminRoute(url = "/admin") {
  window.history.replaceState({}, "", url);
  const rootRoute = createRootRoute({ component: () => <Outlet /> });
  const adminRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/admin",
    validateSearch: adminTabSearch,
    component: AdminPage,
  });
  const router = createRouter({ routeTree: rootRoute.addChildren([adminRoute]) });
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

const USER_ONE = {
  id: "user-1",
  external_employee_id: "emp-0001",
  legal_name: "Alex Example (synthetic)",
  email: "alex@example.invalid",
  active: true,
  // ACTIVE roles only — the revoked SUBMISSION_RECIPIENT in USER_DETAIL's history
  // must not appear here.
  roles: ["HR_REQUESTER"],
  created_at: "2026-09-01T08:00:00Z",
  // Fully provisioned: binding, contract and schedule all present.
  has_idp_subject: true,
  has_effective_contract: true,
  has_effective_schedule: true,
};

const USER_DETAIL = {
  ...USER_ONE,
  idp_subject: "alex-subject",
  role_assignments: [
    {
      id: "assignment-1",
      role: "HR_REQUESTER",
      granted_by_principal: "employee",
      granted_at: "2026-09-02T08:00:00Z",
      revoked_at: null,
    },
    {
      id: "assignment-2",
      role: "SUBMISSION_RECIPIENT",
      granted_by_principal: "employee",
      granted_at: "2026-09-01T08:00:00Z",
      revoked_at: "2026-09-03T08:00:00Z",
    },
  ],
  effective_capabilities: ["ACCESS_REQUEST_CREATE", "DISCLOSURE_READ_GRANTED"],
};

let fetchMock: ReturnType<typeof vi.fn>;
const requested: Array<{ url: string; init?: RequestInit }> = [];

beforeEach(() => {
  requested.length = 0;
  fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    requested.push({ url, init });
    if (url === "/api/v1/me") {
      return new Response(
        JSON.stringify({
          subject: "employee",
          display_name: "Employee",
          capabilities: ["ACCESS_ADMIN", "SYSTEM_STATUS_READ"],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    if (url.startsWith("/api/v1/admin/users?")) {
      return new Response(JSON.stringify([USER_ONE]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (url === "/api/v1/admin/users") {
      if (init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        return new Response(
          JSON.stringify({
            ...USER_DETAIL,
            ...body,
            id: "user-new",
            role_assignments: [],
            effective_capabilities: [],
            has_idp_subject: Boolean(body.idp_subject),
            has_effective_contract: false,
            has_effective_schedule: false,
          }),
          { status: 201, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(JSON.stringify([USER_ONE]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (
      url.startsWith("/api/v1/admin/users/user-") &&
      !url.includes("/roles/") &&
      init?.method !== "PATCH"
    ) {
      if (url === "/api/v1/admin/users/user-new") {
        return new Response(
          JSON.stringify({
            ...USER_DETAIL,
            id: "user-new",
            legal_name: "New Hire (synthetic)",
            idp_subject: "new-hire",
            active: false,
            role_assignments: [],
            effective_capabilities: [],
            has_idp_subject: true,
            has_effective_contract: false,
            has_effective_schedule: false,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(JSON.stringify(USER_DETAIL), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (url.includes("/roles/")) {
      return new Response(
        JSON.stringify({
          id: "assignment-new",
          role: "APPLICATION_ADMIN",
          granted_by_principal: "employee",
          granted_at: "2026-09-04T08:00:00Z",
          revoked_at: null,
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      );
    }
    if (url === "/api/v1/admin/system") {
      return new Response(
        JSON.stringify({
          application: { name: "chronobeam-backend", version: "0.3.0" },
          database: {
            reachable: true,
            schema_revision: "0010_role_assignment",
            expected_revision: "0010_role_assignment",
            schema_current: true,
          },
          auth: { mode: "DEVELOPMENT_HEADER", oidc_configured: false },
          integrations: {
            shift_plan: "SEED_SEAM",
            hr_absence: "BLOCKED_INTEGRATION",
            travel_feed: "BLOCKED_INTEGRATION",
            recipient_registry: "INCONSISTENT",
            calendar: "NOT_LOADED",
          },
          // Nothing published: the calendar card has to say so AND answer when the
          // gap starts, which the boolean alone never could.
          calendar: { editions: [], first_uncovered_month: "2026-09" },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }
    return new Response(JSON.stringify([]), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
});

describe("Administration UI", () => {
  it("shows Users & Access and System tabs; renders the user list", async () => {
    renderAdminRoute();
    expect(await screen.findByText("Users & Access")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "System" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Users & Access" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(await screen.findByText("Alex Example (synthetic)")).toBeInTheDocument();
  });

  it("associates each Administration tab with the panel it controls", async () => {
    const user = userEvent.setup();
    renderAdminRoute();
    const usersTab = await screen.findByRole("tab", { name: "Users & Access" });
    const systemTab = screen.getByRole("tab", { name: "System" });
    const panel = document.getElementById(usersTab.getAttribute("aria-controls") ?? "");
    expect(panel).toHaveAttribute("role", "tabpanel");
    expect(usersTab).toHaveAttribute("aria-controls", systemTab.getAttribute("aria-controls"));
    expect(panel).toHaveAttribute("aria-labelledby", usersTab.id);
    await user.click(systemTab);
    expect(document.getElementById(systemTab.getAttribute("aria-controls") ?? "")).toHaveAttribute(
      "aria-labelledby",
      systemTab.id,
    );
  });

  it("opens the panel named in the URL", async () => {
    renderAdminRoute("/admin?tab=system");
    const systemTab = await screen.findByRole("tab", { name: "System" });
    expect(systemTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Users & Access" })).toHaveAttribute(
      "aria-selected",
      "false",
    );
    expect(await screen.findByText(/chronobeam-backend/)).toBeInTheDocument();
  });

  it("moves selection, focus and the URL with the arrow keys", async () => {
    const user = userEvent.setup();
    renderAdminRoute();
    const usersTab = await screen.findByRole("tab", { name: "Users & Access" });
    const systemTab = screen.getByRole("tab", { name: "System" });
    usersTab.focus();

    await user.keyboard("{ArrowRight}");
    expect(systemTab).toHaveAttribute("aria-selected", "true");
    expect(document.activeElement).toBe(systemTab);
    expect(window.location.search).toBe("?tab=system");

    await user.keyboard("{ArrowLeft}");
    expect(usersTab).toHaveAttribute("aria-selected", "true");
    expect(document.activeElement).toBe(usersTab);
    expect(window.location.search).toBe("?tab=users");
  });

  it("renders a forbidden state without administration capabilities", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/me") {
        return new Response(
          JSON.stringify({
            subject: "employee",
            display_name: "Employee",
            capabilities: ["OWN_LEDGER_READ"],
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(JSON.stringify([]), { status: 200 });
    });
    renderAdminRoute();
    expect(
      await screen.findByText("Access to this area is restricted to administrators."),
    ).toBeInTheDocument();
  });

  it("provisions a user and shows the created account", async () => {
    const user = userEvent.setup();
    renderPage(<AdminUsersPage />);
    await user.click(await screen.findByRole("button", { name: "Provision user" }));
    await user.type(screen.getByLabelText(/Legal name/), "New Hire (synthetic)");
    await user.type(screen.getByLabelText(/Identity subject/), "new-hire");
    await user.click(screen.getByRole("button", { name: "Provision user" }));
    expect(
      await screen.findByRole("heading", { name: "New Hire (synthetic)" }),
    ).toBeInTheDocument();
    // a freshly provisioned account is inactive with no profile data
    expect(screen.getByText("Inactive", { selector: "span" })).toBeInTheDocument();
    expect(await screen.findByText(/Missing contract, Missing schedule/)).toBeInTheDocument();
    const post = requested.find(
      (entry) => entry.url === "/api/v1/admin/users" && entry.init?.method === "POST",
    );
    expect(post).toBeDefined();
    const body = JSON.parse(String(post?.init?.body));
    expect(body.legal_name).toBe("New Hire (synthetic)");
    // the initial binding IS part of provisioning
    expect(body.idp_subject).toBe("new-hire");
    expect(body).not.toHaveProperty("active"); // inactive-by-default is enforced server-side
  });

  it("states why the provision primary cannot proceed", async () => {
    const user = userEvent.setup();
    renderPage(<AdminUsersPage />);
    await user.click(await screen.findByRole("button", { name: "Provision user" }));
    const submit = screen.getByRole("button", { name: "Provision user" });
    const legalName = screen.getByLabelText(/Legal name/);
    const email = screen.getByLabelText(/Email/);

    expect(submit).toBeDisabled();
    const requirement = screen.getByText(/A legal name is required/);
    expect(submit).toHaveAttribute("aria-describedby", requirement.id);
    expect(legalName).toHaveAttribute("aria-invalid", "true");

    await user.type(legalName, "New Hire (synthetic)");
    expect(submit).toBeEnabled();
    expect(legalName).toHaveAttribute("aria-invalid", "false");

    // A malformed email is a native `type="email"` mismatch, which would block
    // the submit event with no message at all — so the app states it first.
    await user.type(email, "not-an-email");
    expect(submit).toBeDisabled();
    expect(email).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText(/Email must look like name@example.com/)).toBeInTheDocument();

    await user.clear(email);
    await user.type(email, "new.hire@example.invalid");
    expect(submit).toBeEnabled();
    expect(email).toHaveAttribute("aria-invalid", "false");
  });

  it("inspects a user: roles, effective capabilities, activation confirmations", async () => {
    const user = userEvent.setup();
    renderPage(<AdminUsersPage />);
    await user.click(await screen.findByRole("button", { name: "View" }));
    expect(await screen.findByText(/granted .* by employee/)).toBeInTheDocument();
    expect(screen.getByText("ACCESS_REQUEST_CREATE")).toBeInTheDocument();
    // revoked assignment is shown as revoked, not actionable
    expect(screen.getByText(/revoked/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Revoke" })).toBeInTheDocument();

    // deactivation requires confirmation
    await user.click(screen.getByRole("button", { name: "Deactivate account" }));
    await user.click(await screen.findByRole("button", { name: /Deactivate Alex Example/ }));
    const patch = requested.find(
      (entry) => entry.url === "/api/v1/admin/users/user-1" && entry.init?.method === "PATCH",
    );
    expect(patch).toBeDefined();
    expect(JSON.parse(String(patch?.init?.body))).toEqual({ active: false });
  });

  it("exposes the selected row toggle as pressed, without implying a load", async () => {
    const user = userEvent.setup();
    renderPage(<AdminUsersPage />);
    const view = await screen.findByRole("button", { name: "View" });
    expect(view).toHaveAttribute("aria-pressed", "false");

    await user.click(view);

    // The label is the selected-row indicator, not a spinner: no ellipsis.
    const viewing = await screen.findByRole("button", { name: "Viewing" });
    expect(viewing).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByText("Viewing…")).toBeNull();
  });

  it("assigns an available role through the selector", async () => {
    const user = userEvent.setup();
    renderPage(<AdminUsersPage />);
    await user.click(await screen.findByRole("button", { name: "View" }));
    const select = await screen.findByLabelText("Assign role");
    await user.selectOptions(select, "APPLICATION_ADMIN");
    await user.click(screen.getByRole("button", { name: "Assign" }));
    expect(
      requested.find(
        (entry) => entry.url.includes("/roles/APPLICATION_ADMIN") && entry.init?.method === "PUT",
      ),
    ).toBeUndefined();
    // Arming is announced in a live region and moves focus to the armed control,
    // from the same hook every other inline confirm uses (WCAG 2.2 SC 4.1.3 +
    // 2.4.3). Without it the second click could land on a state the user was
    // never told about.
    const armed = screen.getByRole("button", { name: "Assign Application administrator?" });
    expect(screen.getByText("Confirm: Assign Application administrator")).toBeInTheDocument();
    expect(document.activeElement).toBe(armed);
    await user.click(screen.getByRole("button", { name: "Assign Application administrator?" }));
    await waitFor(() => {
      const put = requested.find(
        (entry) => entry.url.includes("/roles/APPLICATION_ADMIN") && entry.init?.method === "PUT",
      );
      expect(put).toBeDefined();
    });
  });

  it("does not offer an identity-remap control on an existing account", async () => {
    const user = userEvent.setup();
    renderPage(<AdminUsersPage />);
    await user.click(await screen.findByRole("button", { name: "View" }));
    // the binding is visible read-only
    expect(await screen.findByText("alex-subject")).toBeInTheDocument();
    // and there is no control that could change it
    expect(screen.queryByLabelText("Identity subject")).toBeNull();
    expect(screen.queryByRole("button", { name: /identity mapping/i })).toBeNull();
    expect(
      requested.find(
        (entry) => entry.url === "/api/v1/admin/users/user-1" && entry.init?.method === "PATCH",
      ),
    ).toBeUndefined();
  });

  // The remedy is not gated on the account being inactive: the ACTIVE account that
  // cannot work is exactly the one an operator must act on, so it gets the same
  // instructions rather than a dead end.
  it("names the master-data remedy for an active account without profile data", async () => {
    const user = userEvent.setup();
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/v1/admin/users?") || url === "/api/v1/admin/users") {
        return new Response(JSON.stringify([{ ...USER_ONE, active: true }]), { status: 200 });
      }
      if (url === "/api/v1/admin/users/user-1") {
        return new Response(
          JSON.stringify({
            ...USER_DETAIL,
            active: true,
            has_effective_contract: false,
            has_effective_schedule: false,
          }),
          { status: 200 },
        );
      }
      return new Response(JSON.stringify([]), { status: 200 });
    });
    renderPage(<AdminUsersPage />);
    await user.click(await screen.findByRole("button", { name: "View" }));
    expect(await screen.findByText(/master_data_admin import/)).toBeInTheDocument();
  });

  it("does not offer roles that are already active", async () => {
    const user = userEvent.setup();
    renderPage(<AdminUsersPage />);
    await user.click(await screen.findByRole("button", { name: "View" }));
    const select = await screen.findByLabelText("Assign role");
    const options = Array.from(select.querySelectorAll("option")).map((option) => option.value);
    expect(options).toContain("APPLICATION_ADMIN");
    expect(options).not.toContain("HR_REQUESTER"); // already active
  });

  it("system tab renders operational status without employee data", async () => {
    const user = userEvent.setup();
    renderAdminRoute();
    await user.click(await screen.findByRole("tab", { name: "System" }));
    expect(await screen.findByText(/chronobeam-backend/)).toBeInTheDocument();
    expect(screen.getByText(/0\.3\.0/)).toBeInTheDocument();
    expect(screen.getByText("Development identity header (dev/test only)")).toBeInTheDocument();
    // The schema state is a fact, not a sentence: the revisions behind Technical
    // details are the evidence, and a current schema says nothing extra.
    expect(screen.getByText("Schema revision")).toBeInTheDocument();
    expect(screen.getByText("Shift plan")).toBeInTheDocument();
    expect(screen.queryByText("alex@example.invalid")).not.toBeInTheDocument();
  });

  it("list search and empty state are reachable", async () => {
    const user = userEvent.setup();
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/me") {
        return new Response(
          JSON.stringify({
            subject: "employee",
            display_name: "E",
            capabilities: ["ACCESS_ADMIN"],
          }),
          { status: 200 },
        );
      }
      if (url.startsWith("/api/v1/admin/users?")) {
        return new Response(JSON.stringify([]), { status: 200 });
      }
      return new Response(JSON.stringify([]), { status: 200 });
    });
    renderPage(<AdminUsersPage />);
    await user.type(screen.getByLabelText("Search"), "nobody");
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText(/No users match/)).toBeInTheDocument();
  });
  it("shows each user's active roles in the list, and a dash when there are none", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/v1/admin/users?")) {
        return new Response(
          JSON.stringify([
            USER_ONE,
            {
              ...USER_ONE,
              id: "user-2",
              legal_name: "Robin Plain (synthetic)",
              roles: [],
            },
          ]),
          { status: 200 },
        );
      }
      return new Response(JSON.stringify([]), { status: 200 });
    });
    renderPage(<AdminUsersPage />);
    const rows = within(await screen.findByRole("table")).getAllByRole("row");
    // Row 0 is the header. The label is the product's own words, not the code.
    expect(rows[1]).toHaveTextContent("HR requester");
    expect(rows[1]).not.toHaveTextContent("HR_REQUESTER");
    expect(rows[2]).toHaveTextContent("—");
  });

  it("sorts from the server and switches the page size", async () => {
    const user = userEvent.setup();
    renderPage(<AdminUsersPage />);

    // The first request states the server's own default order explicitly, so the
    // client never silently re-imposes a different one.
    await waitFor(() =>
      expect(
        requested.some((entry) => entry.url.includes("sort=provisioned&direction=asc&limit=25")),
      ).toBe(true),
    );

    // Clicking a header sorts by it, ascending, and marks the column for AT.
    await user.click(await screen.findByRole("button", { name: "Name" }));
    await waitFor(() =>
      expect(requested.some((entry) => entry.url.includes("sort=name&direction=asc"))).toBe(true),
    );
    expect(screen.getByRole("columnheader", { name: "Name" })).toHaveAttribute(
      "aria-sort",
      "ascending",
    );

    // Clicking the column already sorted FLIPS it rather than re-selecting it.
    await user.click(screen.getByRole("button", { name: "Name" }));
    await waitFor(() =>
      expect(requested.some((entry) => entry.url.includes("sort=name&direction=desc"))).toBe(true),
    );
    expect(screen.getByRole("columnheader", { name: "Name" })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
    // …and the columns that are not in force say so.
    expect(screen.getByRole("columnheader", { name: "Role" })).toHaveAttribute("aria-sort", "none");

    // Every header is sortable, including the Role column.
    await user.click(screen.getByRole("button", { name: "Role" }));
    await waitFor(() =>
      expect(requested.some((entry) => entry.url.includes("sort=role&direction=asc"))).toBe(true),
    );

    // "All" is the explicit 0, not an arbitrarily large page.
    await user.selectOptions(screen.getByLabelText("Rows"), "0");
    await waitFor(() =>
      expect(requested.some((entry) => entry.url.includes("limit=0"))).toBe(true),
    );
    await user.selectOptions(screen.getByLabelText("Rows"), "100");
    await waitFor(() =>
      expect(requested.some((entry) => entry.url.includes("limit=100"))).toBe(true),
    );
  });
});

describe("AdminSystemPage", () => {
  it("leads with human statuses and keeps raw codes behind technical details", async () => {
    renderPage(<AdminSystemPage />);
    expect(await screen.findByText("External HR absence system")).toBeInTheDocument();
    expect(screen.getByText("Application available")).toBeInTheDocument();
    expect(screen.getByText("Database connected")).toBeInTheDocument();
    expect(screen.getAllByText("no live connection").length).toBe(2);
    // Raw engine codes remain available inside the technical-details region.
    expect(screen.getAllByText("BLOCKED_INTEGRATION", { exact: false }).length).toBe(2);
    expect(screen.getByText("SEED_SEAM", { exact: false })).toBeInTheDocument();
  });

  it("surfaces an inconsistent recipient registry and an unpublished calendar", async () => {
    renderPage(<AdminSystemPage />);
    expect(await screen.findByText("Submission recipients")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Calendar" })).toBeInTheDocument();
    expect(screen.getByText("inconsistent — submissions refused")).toBeInTheDocument();
    expect(screen.getByText("not published")).toBeInTheDocument();
    expect(screen.getByText(/a configured subject holds no reading role/i)).toBeInTheDocument();
    expect(
      screen.getByText(/publish one before recording future-dated facts/i),
    ).toBeInTheDocument();
    // The calendar has its own card now, so its exact code lives behind THAT card's
    // technical details and the integration list no longer repeats it.
    expect(screen.queryByText("Calendar edition")).not.toBeInTheDocument();
    expect(screen.getByText("recipient_registry: INCONSISTENT")).toBeInTheDocument();
    expect(screen.getByText("NOT_LOADED")).toBeInTheDocument();
  });

  it("lists every published edition with its effective range and its source", async () => {
    // A base edition and a bounded corrective overlay: overlapping published data,
    // which is legitimate and must not be presented as a conflict.
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/v1/admin/system") {
        return new Response(
          JSON.stringify({
            application: { name: "chronobeam-backend", version: "0.3.0", build_sha: "" },
            database: {
              reachable: true,
              schema_revision: "0010",
              expected_revision: "0010",
              schema_current: true,
            },
            auth: { mode: "DEVELOPMENT_HEADER", oidc_configured: false },
            integrations: { calendar: "LOADED" },
            calendar: {
              editions: [
                {
                  version: "2026-pilot-v1",
                  valid_from: "2026-01-01",
                  valid_to: "2026-12-31",
                  source: "HR calendar export",
                },
                {
                  version: "corrective-2026-10-03",
                  valid_from: "2026-10-03",
                  valid_to: "2026-10-03",
                  source: "operator correction",
                },
                {
                  version: "open-ended-v2",
                  valid_from: "2027-01-01",
                  valid_to: null,
                  source: "HR calendar export",
                },
              ],
              first_uncovered_month: null,
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(JSON.stringify([]), { status: 200 });
    });
    renderPage(<AdminSystemPage />);
    expect(await screen.findByText("published")).toBeInTheDocument();

    const rows = within(screen.getByRole("table")).getAllByRole("row");
    // Row 0 is the header; the caption names the table for a screen reader.
    expect(screen.getByText("Published editions")).toBeInTheDocument();
    expect(rows[1]).toHaveTextContent("2026-pilot-v1");
    expect(rows[1]).toHaveTextContent("01 Jan 2026 → 31 Dec 2026");
    expect(rows[1]).toHaveTextContent("HR calendar export");
    // The overlay is listed like any other edition: no error, no "conflict" label.
    expect(rows[2]).toHaveTextContent("corrective-2026-10-03");
    expect(rows[2]).toHaveTextContent("03 Oct 2026 → 03 Oct 2026");
    expect(rows[2]).toHaveTextContent("operator correction");
    // An open-ended edition says so in words rather than showing an empty cell.
    expect(rows[3]).toHaveTextContent("01 Jan 2027 → open-ended");
    expect(screen.getByText("No coverage gap from this month onward.")).toBeInTheDocument();
  });

  it("names the first uncovered month instead of only saying the calendar is loaded", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/v1/admin/system") {
        return new Response(
          JSON.stringify({
            application: { name: "chronobeam-backend", version: "0.3.0", build_sha: "" },
            database: {
              reachable: true,
              schema_revision: "0010",
              expected_revision: "0010",
              schema_current: true,
            },
            auth: { mode: "DEVELOPMENT_HEADER", oidc_configured: false },
            integrations: { calendar: "LOADED" },
            calendar: {
              editions: [
                {
                  version: "2026-pilot-v1",
                  valid_from: "2026-01-01",
                  valid_to: "2026-12-31",
                  source: "HR calendar export",
                },
              ],
              first_uncovered_month: "2027-01",
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response(JSON.stringify([]), { status: 200 });
    });
    renderPage(<AdminSystemPage />);
    // The loaded edition stays ordinary — a warning, never error-red for published data.
    const status = await screen.findByText("published");
    expect(status).toHaveClass("status-ok");
    const gap = screen.getByText(
      "Coverage gap: January 2027 is the first month with no covering edition.",
    );
    expect(gap).toHaveClass("status-warning");
    expect(gap).not.toHaveClass("status-error");
    expect(screen.getByText(/stay blocked until one is published/)).toBeInTheDocument();
    // The derived month is available verbatim behind technical details.
    expect(screen.getByText("2027-01")).toBeInTheDocument();
  });
});

describe("Users list readiness", () => {
  /** One row per provisioning state the operator has to be able to tell apart.
   *  The names are deliberately non-overlapping: a row's accessible name is the
   *  concatenation of its cells, so "Active Bare" must not be a substring of the
   *  inactive row's name. */
  const ROWS = [
    { ...USER_ONE, id: "u-ready", legal_name: "Ready Rita (synthetic)" },
    { ...USER_ONE, id: "u-off", legal_name: "Switched Off (synthetic)", active: false },
    { ...USER_ONE, id: "u-no-sso", legal_name: "No Sso (synthetic)", has_idp_subject: false },
    {
      ...USER_ONE,
      id: "u-active-bare",
      legal_name: "Bare Active (synthetic)",
      has_effective_contract: false,
      has_effective_schedule: false,
    },
    {
      ...USER_ONE,
      id: "u-inactive-bare",
      legal_name: "Bare Inactive (synthetic)",
      active: false,
      has_idp_subject: false,
      has_effective_contract: false,
      has_effective_schedule: false,
    },
  ];

  function mockRows() {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).startsWith("/api/v1/admin/users?")) {
        return new Response(JSON.stringify(ROWS), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(JSON.stringify([]), { status: 200 });
    });
  }

  /** The Readiness cell of the named row (cells: name, id, status,
   *  readiness, email, role, provisioned, actions). */
  function readinessCell(name: string): HTMLElement {
    const row = screen.getByRole("row", { name: new RegExp(name) });
    return within(row).getAllByRole("cell")[3] as HTMLElement;
  }

  it("names every missing prerequisite, so no account has to be opened to find out", async () => {
    mockRows();
    renderPage(<AdminUsersPage />);
    await screen.findByRole("table");

    expect(readinessCell("Ready Rita")).toHaveTextContent("Ready");
    // A single missing prerequisite is named, and nothing else is implied.
    expect(readinessCell("No Sso")).toHaveTextContent("Missing SSO");
    expect(readinessCell("No Sso")).not.toHaveTextContent("Missing contract");
    expect(readinessCell("No Sso")).not.toHaveTextContent("Missing schedule");
    // Several are listed compactly rather than as three columns.
    expect(readinessCell("Bare Active")).toHaveTextContent("Missing contract, Missing schedule");
    expect(readinessCell("Bare Inactive")).toHaveTextContent(
      "Missing SSO, Missing contract, Missing schedule",
    );
  });

  it("does not treat an inactive incomplete account as identical to an active one", async () => {
    mockRows();
    renderPage(<AdminUsersPage />);
    await screen.findByRole("table");

    // An ACTIVE account that cannot work is the support call.
    expect(readinessCell("Bare Active")).toHaveClass("status-warning");
    // The same gaps on an INACTIVE account are ordinary provisioning in progress.
    expect(readinessCell("Bare Inactive")).toHaveClass("status-muted");
    expect(readinessCell("Bare Inactive")).not.toHaveClass("status-warning");
    // Readiness is about provisioning completeness, not account state: an inactive
    // account with complete master data is Ready, and the Status column already
    // says it is switched off.
    expect(readinessCell("Switched Off")).toHaveTextContent("Ready");
    const status = within(screen.getByRole("row", { name: /Switched Off/ })).getAllByRole(
      "cell",
    )[2] as HTMLElement;
    expect(status).toHaveTextContent("Inactive");
  });

  it("does not claim a sort the server cannot perform", async () => {
    mockRows();
    renderPage(<AdminUsersPage />);
    await screen.findByRole("table");

    const readiness = screen.getByRole("columnheader", { name: "Readiness" });
    // No aria-sort at all: a column that cannot be ordered is not a column that is
    // not ordered, and an empty sort control would be a promise with nothing behind it.
    expect(readiness).not.toHaveAttribute("aria-sort");
    expect(within(readiness).queryByRole("button")).not.toBeInTheDocument();
    // The server-ordered columns keep their state.
    expect(screen.getByRole("columnheader", { name: "Status" })).toHaveAttribute(
      "aria-sort",
      "none",
    );
  });
});
