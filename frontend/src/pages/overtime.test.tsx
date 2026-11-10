import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { OvertimePage } from "./overtime";

vi.mock("@tanstack/react-router", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@tanstack/react-router")>()),
  Link: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

const authorization = {
  id: "auth-1",
  valid_from: "2026-07-13T08:00:00+02:00",
  valid_to: null,
  purpose: "Beamtime support",
  status: "APPROVED",
  authorized_by: "Dr. Supervisor (institutional)",
  source_ref: "ref-2026-07-13",
  source: "INSTITUTIONAL_APPROVAL",
  version: 1,
};

let authResponse: unknown;
let previewResponse: unknown;
const mutations: Array<{ url: string; body: unknown; method: string }> = [];
let fetchMock: ReturnType<typeof vi.fn>;

function renderPage() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <OvertimePage month="2026-07" />
    </QueryClientProvider>,
  );
}

describe("employee overtime view", () => {
  beforeEach(() => {
    authResponse = [authorization];
    previewResponse = {
      report_type: "OVERTIME",
      policy_version: "2026-ot-v1",
      policy_status: "CONFIRMED",
      employee_identifier: "emp-0001",
      period_start: "2026-07-01",
      period_end: "2026-08-01",
      authorizations: [
        {
          id: "auth-1",
          valid_from: authorization.valid_from,
          valid_to: null,
          purpose: "Beamtime support",
          status: "APPROVED",
          source_ref: "ref-2026-07-13",
        },
      ],
      overtime_intervals: [
        {
          interval_id: "i-1",
          start_at: "2026-07-13T18:00:00+02:00",
          end_at: "2026-07-13T20:30:00+02:00",
          work_type: "OVERTIME_PAID",
          elapsed_seconds: 9000,
          linked_authorization_id: "auth-1",
        },
        {
          interval_id: "i-2",
          start_at: "2026-07-14T19:00:00+02:00",
          end_at: "2026-07-14T20:00:00+02:00",
          work_type: "OVERTIME_TIME_COMP",
          elapsed_seconds: 3600,
          linked_authorization_id: null,
        },
      ],
      classified_components: [
        {
          rule_id: "ALLOW-002",
          component: "OVERTIME_SUPPLEMENT",
          minutes: 150,
          time_equivalent_minutes: null,
        },
        { rule_id: "ALLOW-003", component: "NIGHT_20", minutes: 90, time_equivalent_minutes: 18 },
      ],
      election: { id: "election-1", mode: "PAY", version: 1 },
      findings: [
        {
          rule_id: "ACCESS-006",
          status: "BLOCKED_POLICY",
          message: "Overtime without an authorization reference cannot be submitted.",
        },
      ],
    };
    mutations.length = 0;
    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/overtime/preview"))
        return new Response(JSON.stringify(previewResponse), { status: 200 });
      if (url.includes("/overtime-authorizations")) {
        if (init?.method === "POST") {
          const body = init.body ? JSON.parse(String(init.body)) : {};
          mutations.push({ url, body, method: "POST" });
          return new Response(
            JSON.stringify({ id: "auth-2", ...body, source: "EMPLOYEE_RECORDED", version: 1 }),
            { status: 201 },
          );
        }
        if (init?.method === "PATCH") {
          const body = init.body ? JSON.parse(String(init.body)) : {};
          mutations.push({ url, body, method: "PATCH" });
          return new Response(JSON.stringify({ ...authorization, ...body, version: 2 }), {
            status: 200,
          });
        }
        return new Response(JSON.stringify(authResponse), { status: 200 });
      }
      if (url.includes("/compensation-elections?context_type=OVERTIME&context_id=auth-1")) {
        return new Response(
          JSON.stringify({
            id: "election-1",
            context_type: "OVERTIME",
            context_id: "auth-1",
            mode: "PAY",
            version: 1,
            selected_at: "2026-07-13T09:00:00+02:00",
          }),
          { status: 200 },
        );
      }
      if (url.includes("/compensation-elections")) {
        const body = init?.body ? JSON.parse(String(init.body)) : {};
        mutations.push({ url, body, method: String(init?.method) });
        return new Response(
          JSON.stringify({
            id: "election-2",
            context_type: "OVERTIME",
            context_id: body.context_id,
            mode: body.mode,
            version: 2,
            selected_at: "2026-07-20T09:00:00+02:00",
          }),
          { status: 200 },
        );
      }
      return new Response(JSON.stringify({}), { status: 404 });
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  it("shows the decision, the actor and the provenance of an institutionally decided row read-only", async () => {
    renderPage();
    expect(await screen.findByText(/Overtime authorizations/)).toBeInTheDocument();
    expect(screen.getByText(/Beamtime support/)).toBeInTheDocument();
    expect(
      screen.getByText(/Institutional decision by Dr\. Supervisor \(institutional\)/),
    ).toBeInTheDocument();
    expect(screen.queryByText("INSTITUTIONAL_APPROVAL")).not.toBeInTheDocument();
    // The employee cannot rewrite an institutional decision.
    expect(screen.getByRole("button", { name: "Correct" })).toBeDisabled();
    expect(screen.queryByText(/does not verify external approvals/)).not.toBeInTheDocument();
  });

  it("creates an authorization reference (employee-recorded, not verified)", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Add authorization reference" }));
    // A request has no decision and no approver to capture.
    expect(screen.queryByLabelText(/Authorized by/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Status")).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("Valid from"), "2026-07-20T08:00");
    await user.type(screen.getByLabelText("Purpose"), "Shift cover");
    await user.type(screen.getByLabelText("Source / reference"), "ref-2");
    await user.click(screen.getByRole("button", { name: "Save reference" }));
    await waitFor(() =>
      expect(
        mutations.some(
          (entry) => entry.method === "POST" && entry.url.includes("/overtime-authorizations"),
        ),
      ).toBe(true),
    );
    const created = mutations.find(
      (entry) => entry.method === "POST" && entry.url.includes("/overtime-authorizations"),
    );
    expect(created?.body).toMatchObject({
      purpose: "Shift cover",
      source_ref: "ref-2",
      valid_from: "2026-07-20T08:00",
    });
    // The server refuses these outright (extra="forbid"), so they are never sent.
    expect(created?.body).not.toHaveProperty("status");
    expect(created?.body).not.toHaveProperty("authorized_by");
    expect(created?.body).not.toHaveProperty("source");
    expect(await screen.findByText(/employee-entered, not verified/)).toBeInTheDocument();
  });

  it("shows an unrecognised provenance read-only, exactly as the server treats it", async () => {
    authResponse = [
      { ...authorization, source: "FUTURE_ADAPTER_V2", status: "REQUESTED", authorized_by: null },
    ];
    renderPage();
    expect(await screen.findByText(/Read-only/)).toBeInTheDocument();
    // Deny by default: the server refuses every /me write on this head, so the
    // control must not offer one that always fails.
    expect(screen.getByRole("button", { name: "Correct" })).toBeDisabled();
  });

  it("offers only Requested or Cancelled when correcting your own request", async () => {
    authResponse = [
      { ...authorization, status: "REQUESTED", authorized_by: null, source: "EMPLOYEE_RECORDED" },
    ];
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Correct" }));
    const status = screen.getByLabelText("Status");
    expect(
      Array.from(status.querySelectorAll("option")).map((option) => option.textContent),
    ).toEqual(["Requested", "Cancelled"]);
    expect(screen.queryByLabelText(/Authorized by/)).not.toBeInTheDocument();
    await user.selectOptions(status, "CANCELLED");
    await user.click(screen.getByRole("button", { name: "Save correction" }));
    await waitFor(() => expect(mutations.some((entry) => entry.method === "PATCH")).toBe(true));
    const patch = mutations.find((entry) => entry.method === "PATCH");
    expect(patch?.body).toMatchObject({ version: 1, status: "CANCELLED" });
    expect(patch?.body).not.toHaveProperty("authorized_by");
  });

  it("never rewrites a legacy self-declared status on a metadata-only correction", async () => {
    // A pre-F6 row can be APPROVED with the employee's own provenance, and the
    // select then starts on REQUESTED. Comparing the selection to the row's
    // status would send `status: "REQUESTED"` on a metadata-only save and
    // silently version the head back into a request.
    authResponse = [
      { ...authorization, status: "APPROVED", authorized_by: "self", source: "EMPLOYEE_RECORDED" },
    ];
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Correct" }));
    const purpose = screen.getByLabelText("Purpose");
    await user.clear(purpose);
    await user.type(purpose, "Beamtime support (corrected)");
    await user.click(screen.getByRole("button", { name: "Save correction" }));
    await waitFor(() => expect(mutations.some((entry) => entry.method === "PATCH")).toBe(true));
    const untouched = mutations.find((entry) => entry.method === "PATCH");
    expect(untouched?.body).toMatchObject({
      version: 1,
      purpose: "Beamtime support (corrected)",
    });
    expect(untouched?.body).not.toHaveProperty("status");

    // ... but a status the employee DID pick is still sent.
    await user.click(await screen.findByRole("button", { name: "Correct" }));
    await user.selectOptions(screen.getByLabelText("Status"), "CANCELLED");
    await user.click(screen.getByRole("button", { name: "Save correction" }));
    await waitFor(() => expect(mutations.filter((e) => e.method === "PATCH")).toHaveLength(2));
    expect(mutations.filter((entry) => entry.method === "PATCH")[1]?.body).toMatchObject({
      status: "CANCELLED",
    });
  });

  it("discards a cancelled draft instead of saving it with a later edit", async () => {
    // Cancel means abandon. A draft that survived it would be sent by the next
    // unrelated edit — here the CANCELLED the employee had just walked away from.
    authResponse = [
      { ...authorization, status: "APPROVED", authorized_by: "self", source: "EMPLOYEE_RECORDED" },
    ];
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Correct" }));
    await user.selectOptions(screen.getByLabelText("Status"), "CANCELLED");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    await user.click(await screen.findByRole("button", { name: "Correct" }));
    expect(screen.getByLabelText("Status")).toHaveValue("REQUESTED");
    const purpose = screen.getByLabelText("Purpose");
    await user.clear(purpose);
    await user.type(purpose, "Beamtime support (renamed)");
    await user.click(screen.getByRole("button", { name: "Save correction" }));
    await waitFor(() => expect(mutations.some((entry) => entry.method === "PATCH")).toBe(true));
    const patch = mutations.find((entry) => entry.method === "PATCH");
    expect(patch?.body).toMatchObject({ purpose: "Beamtime support (renamed)" });
    expect(patch?.body).not.toHaveProperty("status");
  });

  it("renders the classification preview with blocked unlinked overtime", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: /Show \/ refresh preview/ }));
    expect(await screen.findByText(/Overtime preview — not shared/)).toBeInTheDocument();
    expect(screen.getByText(/Overtime supplement: 2:30/)).toBeInTheDocument();
    expect(screen.getByText(/Night allowance: 1:30/)).toBeInTheDocument();
    expect(screen.getAllByText(/no authorization reference/)).toHaveLength(1);
    expect(
      screen.getByText("Overtime not covered by an institutional approval"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/Overtime without an authorization reference cannot be submitted/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Election: Paid out/)).toBeInTheDocument();
  });

  it("renders each distinct ACCESS-006 refusal as its own finding", async () => {
    const user = userEvent.setup();
    previewResponse = {
      ...(previewResponse as Record<string, unknown>),
      findings: [
        {
          rule_id: "ACCESS-006",
          status: "BLOCKED_POLICY",
          message: "Overtime without an authorization reference cannot be submitted.",
        },
        {
          rule_id: "ACCESS-006",
          status: "BLOCKED_POLICY",
          message:
            "The linked overtime authorization is not an institutional approval, so this overtime cannot be submitted.",
        },
        {
          rule_id: "ACCESS-006",
          status: "BLOCKED_POLICY",
          message:
            "The overtime interval is not covered by the linked authorization's validity window, so it cannot be submitted.",
        },
      ],
    };
    renderPage();
    await user.click(await screen.findByRole("button", { name: /Show \/ refresh preview/ }));
    expect(
      await screen.findByText(/is not an institutional approval, so this overtime/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/is not covered by the linked authorization's validity window/),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Overtime without an authorization reference cannot be submitted."),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "The linked overtime authorization is not an institutional approval, so this overtime cannot be submitted.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "The overtime interval is not covered by the linked authorization's validity window, so it cannot be submitted.",
      ),
    ).toBeInTheDocument();
  });

  /**
   * The preview panel renders `preview.election`, so an election is an INPUT to
   * a derived view. Saving one invalidated only the election read, leaving the
   * visible preview stating the previous choice (review R18). The preview key is
   * now registered as fact-derived, so the write moves it like any other.
   */
  it("refreshes the derived preview when the compensation election changes", async () => {
    const user = userEvent.setup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidated: unknown[] = [];
    const spy = vi.spyOn(client, "invalidateQueries").mockImplementation(async (filters) => {
      invalidated.push((filters as { queryKey?: unknown })?.queryKey);
    });
    render(
      <QueryClientProvider client={client}>
        <OvertimePage month="2026-07" />
      </QueryClientProvider>,
    );
    const group = await screen.findByRole("group", { name: "Compensation election" });
    await user.click(within(group).getByRole("radio", { name: "Time compensation" }));

    await waitFor(() =>
      expect(invalidated.some((key) => JSON.stringify(key).includes("overtime-preview"))).toBe(
        true,
      ),
    );
    spy.mockRestore();
  });

  /**
   * An authorization reference is one of the facts the OPEN preview is computed
   * over — it decides which overtime is covered. Refreshing only the list left
   * the visible preview stating that the reference does not exist (review R6).
   */
  it("refreshes the open preview when an authorization reference is added", async () => {
    const user = userEvent.setup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidated: unknown[] = [];
    vi.spyOn(client, "invalidateQueries").mockImplementation(async (filters) => {
      invalidated.push((filters as { queryKey?: unknown })?.queryKey);
    });
    render(
      <QueryClientProvider client={client}>
        <OvertimePage month="2026-07" />
      </QueryClientProvider>,
    );
    await user.click(await screen.findByRole("button", { name: /Add authorization reference/i }));
    await user.type(screen.getByLabelText("Authorization valid from"), "01.07.2026 08:00");
    await user.click(screen.getByRole("button", { name: "Save reference" }));

    await waitFor(() =>
      expect(invalidated.some((key) => JSON.stringify(key).includes("overtime-preview"))).toBe(
        true,
      ),
    );
  });

  it("changes the compensation election for an authorization", async () => {
    const user = userEvent.setup();
    renderPage();
    const group = await screen.findByRole("group", { name: "Compensation election" });
    const timeRadio = within(group).getByRole("radio", { name: "Time compensation" });
    await user.click(timeRadio);
    await waitFor(() =>
      expect(mutations.some((entry) => entry.url.includes("/compensation-elections"))).toBe(true),
    );
    const saved = mutations.find((entry) => entry.url.includes("/compensation-elections"));
    expect(saved?.body).toEqual({ context_type: "OVERTIME", context_id: "auth-1", mode: "TIME" });
    expect(await screen.findByText("Compensation choice saved.")).toBeInTheDocument();
  });
});
