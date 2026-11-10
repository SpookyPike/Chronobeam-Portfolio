import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { OcdPage } from "./ocd";

vi.mock("@tanstack/react-router", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@tanstack/react-router")>()),
  Link: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

let ocdResponse: unknown;
let previewResponse: unknown;
const mutations: Array<{ url: string; body: unknown; method: string }> = [];
let fetchMock: ReturnType<typeof vi.fn>;
// The capability the identity read reports. Empty is the fail-closed default, so
// a test that forgets to grant a switch never accidentally gets a write control.
let identityCapabilities: string[] = [];

const assignment = {
  id: "assignment-1",
  start_at: "2026-07-19T19:00:00+02:00",
  end_at: "2026-07-20T08:00:00+02:00",
  status: "SCHEDULED",
  scheduled_at: "2026-06-01T10:00:00+02:00",
  source: "PILOT",
  calls: [],
  findings: [
    {
      rule_id: "OCD-002",
      status: "INFO",
      message: "Assignment agreed more than 4 weeks in advance.",
    },
  ],
};

/** An availability the EMPLOYEE recorded — the only kind with a write path.
 *
 *  `version` is the row's optimistic-concurrency token. `OcdOut` does not report
 *  it yet (see the task report), so the fixture carries it only in the tests that
 *  exercise the guarded commands: the wiring is verified here so that it works the
 *  moment the read payload carries the token, and `versionToken` withholds the
 *  controls rather than guessing when it does not. */
const employeeAssignment = {
  ...assignment,
  source: "EMPLOYEE_RECORDED",
  plan_source: "EMPLOYEE_RECORDED",
  scheduled_at: null,
  reported_notice_at: null,
  version: 1,
};

const recordedCall = {
  id: "call-1",
  mode: "ON_SITE",
  call_start_at: "2026-07-19T23:00:00+02:00",
  call_end_at: "2026-07-20T02:00:00+02:00",
  travel_out_minutes: null,
  travel_back_minutes: null,
  comment: null,
  status: "RECORDED",
  version: 1,
  linked_interval_id: "interval-1",
  span_start_at: "2026-07-19T23:00:00+02:00",
  span_end_at: "2026-07-20T02:00:00+02:00",
  elapsed_seconds: 3 * 3600,
};

const previewFixture = {
  report_type: "OCD_COMPENSATION",
  policy_version: "2026-ocd-v1",
  policy_status: "CONFIRMED",
  employee_identifier: "emp-0001",
  ocd_assignment_id: "assignment-1",
  assignment_start_at: "2026-07-19T19:00:00+02:00",
  assignment_end_at: "2026-07-20T08:00:00+02:00",
  calls: [],
  classified_components: [
    {
      rule_id: "OCD-007",
      component: "OCD_CALL_BASE",
      minutes: 135,
      time_equivalent_minutes: 135,
    },
  ],
  duty: {
    duty_minutes: 780,
    duty_kind: "DUTY_GE_12H",
    units: 4,
    basis_percent: null,
    short_notice: null as boolean | null,
  },
  election: { id: null, mode: null, version: null },
  findings: [],
};

function renderPage() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <OcdPage month="2026-07" />
    </QueryClientProvider>,
  );
}

/** The shared API stub for every case in this file: the page reads the
 *  assignment list, the session's capabilities and, for a call write, the
 *  election. Declared once at file scope so a describe added later cannot
 *  silently run without a stub (or against the previous case's state). */
beforeEach(() => {
  ocdResponse = [assignment];
  previewResponse = null;
  identityCapabilities = [];
  mutations.length = 0;
  fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.includes("/api/v1/me/ocd?from=")) {
      return new Response(JSON.stringify(ocdResponse), { status: 200 });
    }
    // The identity read shares the shell's `["identity"]` cache entry. Exact
    // match, because the OCD routes below are different URLs.
    if (url === "/api/v1/me")
      return new Response(JSON.stringify({ capabilities: identityCapabilities }), {
        status: 200,
      });
    if (url.includes("/compensation-elections?context_type=OCD")) {
      return new Response(JSON.stringify(null), { status: 200 });
    }
    if (url.includes("/compensation-elections")) {
      const body = init?.body ? JSON.parse(String(init.body)) : {};
      mutations.push({ url, body, method: String(init?.method) });
      return new Response(
        JSON.stringify({
          id: "election-1",
          context_type: "OCD",
          context_id: body.context_id,
          mode: body.mode,
          version: 1,
          selected_at: "2026-07-20T09:00:00+02:00",
        }),
        { status: 200 },
      );
    }
    if (url.includes("/ocd-calls")) {
      const body = init?.body ? JSON.parse(String(init.body)) : {};
      mutations.push({ url, body, method: String(init?.method) });
      return new Response(
        JSON.stringify({
          id: "call-1",
          mode: body.mode,
          call_start_at: "2026-07-20T01:10:00+02:00",
          call_end_at: "2026-07-20T03:25:00+02:00",
          travel_out_minutes: body.mode === "ON_SITE" ? (body.travel_out_minutes ?? null) : null,
          travel_back_minutes: body.mode === "ON_SITE" ? (body.travel_back_minutes ?? null) : null,
          comment: body.comment ?? null,
          status: "RECORDED",
          version: 1,
          interval_id: "interval-ocd-1",
          span_start_at: "2026-07-20T00:45:00+02:00",
          span_end_at: "2026-07-20T03:50:00+02:00",
          elapsed_seconds: 11100,
        }),
        { status: 201 },
      );
    }
    if (url.includes("/compensation-preview")) {
      return new Response(JSON.stringify(previewResponse), { status: 200 });
    }
    // The availability WRITE routes, after the more specific reads above: the
    // compensation preview shares the `/ocd-assignments/` prefix.
    if (url.includes("/ocd-assignments") || url.includes("/cancel")) {
      const body = init?.body ? JSON.parse(String(init.body)) : {};
      mutations.push({ url, body, method: String(init?.method) });
      return new Response(JSON.stringify({ ...employeeAssignment, ...body }), {
        status: String(init?.method) === "POST" && url.endsWith("/ocd-assignments") ? 201 : 200,
      });
    }
    return new Response(JSON.stringify({}), { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
});

describe("employee OCD view", () => {
  it("shows the assignment, its private findings, and the availability/call separation", async () => {
    renderPage();
    expect((await screen.findAllByText(/On-call duty —/)).length).toBeGreaterThan(0);
    expect(screen.getByText(/19 Jul/)).toBeInTheDocument();
    expect(screen.getByText(/Assignment agreed more than 4 weeks in advance/)).toBeInTheDocument();
    expect(screen.getByText(/No calls recorded/)).toBeInTheDocument();
    expect(screen.getByText("Availability")).toBeInTheDocument();
    // Separation invariant: no work fact exists before a call is recorded.
    expect(screen.queryByText(/00:45/)).not.toBeInTheDocument();
  });

  it("records a call with travel and shows the compensation election flow", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Add call" }));
    const start = screen.getByLabelText("Call start");
    const end = screen.getByLabelText("Call end");
    await user.type(start, "2026-07-20T01:10");
    await user.type(end, "2026-07-20T03:25");
    const travelOut = screen.getByLabelText(/Travel to site/);
    const travelBack = screen.getByLabelText(/Travel back/);
    await user.type(travelOut, "25");
    await user.type(travelBack, "25");
    await user.click(screen.getByRole("button", { name: "Record call + work time" }));
    await waitFor(() =>
      expect(
        mutations.some((entry) => entry.url.includes("/ocd-calls") && entry.method === "POST"),
      ).toBe(true),
    );
    const created = mutations.find(
      (entry) => entry.url.includes("/ocd-calls") && entry.method === "POST",
    );
    expect(created?.body).toMatchObject({
      ocd_assignment_id: "assignment-1",
      mode: "ON_SITE",
      travel_out_minutes: 25,
      travel_back_minutes: 25,
    });
    expect(
      await screen.findByText(/Call recorded together with its work time/),
    ).toBeInTheDocument();
  });

  it("rejects remote calls that carry travel by keeping the server error visible", async () => {
    const user = userEvent.setup();
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/v1/me/ocd?from="))
        return new Response(JSON.stringify(ocdResponse), { status: 200 });
      if (url.includes("/compensation-elections"))
        return new Response(JSON.stringify(null), { status: 200 });
      if (url.includes("/ocd-calls")) {
        return new Response(
          JSON.stringify({
            detail: {
              code: "VALIDATION_FAILED",
              message: "Remote calls cannot include travel time.",
            },
          }),
          { status: 422 },
        );
      }
      return new Response(JSON.stringify({}), { status: 404 });
    });
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Add call" }));
    const mode = screen.getByLabelText("Mode");
    await user.selectOptions(mode, "REMOTE");
    await user.type(screen.getByLabelText("Call start"), "2026-07-20T01:10");
    await user.type(screen.getByLabelText("Call end"), "2026-07-20T03:25");
    await user.click(screen.getByRole("button", { name: "Record call + work time" }));
    // A failure is announced as an alert, not as a status: an alarm for good news
    // trains people to ignore alarms (the one-channel contract in lib/notice.tsx).
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Remote calls cannot include travel time/,
    );
  });

  /**
   * A compensation preview is a snapshot of the computation over the RECORDED
   * calls. Voiding one moves those facts, so the snapshot describes an
   * assignment that no longer exists — and waiting for the invalidated query to
   * refetch is not equivalent, because a failed refetch would leave the stale
   * projection on screen presenting itself as current (review R3). The refetch
   * is made to FAIL here, which is the case the effect alone cannot cover.
   */
  it("drops the private compensation preview when a call write succeeds, even if the refetch fails", async () => {
    const user = userEvent.setup();
    previewResponse = previewFixture;
    ocdResponse = [
      {
        ...assignment,
        calls: [
          {
            id: "call-1",
            version: 1,
            status: "RECORDED",
            mode: "ON_SITE",
            call_start_at: "2026-07-19T23:00:00+02:00",
            call_end_at: "2026-07-20T02:00:00+02:00",
            elapsed_seconds: 10800,
            travel_out_minutes: null,
            travel_back_minutes: null,
            comment: null,
            work_interval_id: "interval-1",
          },
        ],
      },
    ];
    let callsServed = 0;
    const base = vi.mocked(fetch).getMockImplementation()!;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes("/void")) return new Response(null, { status: 204 });
        if (url.includes("/api/v1/me/ocd?from=")) {
          // The first read is the loaded assignment; every later read fails.
          if (callsServed++ > 0) return new Response(null, { status: 500 });
        }
        return base(input, init);
      }),
    );
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Preview OCD compensation" }));
    expect(await screen.findByText(/OCD compensation preview — not shared/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Void call" }));

    await waitFor(() =>
      expect(screen.queryByText(/OCD compensation preview — not shared/)).toBeNull(),
    );
  });

  /**
   * A message rendered beside a field is not enough: the CONTROL must declare
   * itself invalid and point at the message, or a screen-reader user cannot get
   * from the field to its rejection (review R6 — the same wiring R5/R9 gave the
   * deviation and correction forms).
   */
  it("connects an add-call field rejection to the control it names", async () => {
    const user = userEvent.setup();
    // Only the call write fails; every other request keeps the shared mock.
    const base = vi.mocked(fetch).getMockImplementation()!;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
        String(input).includes("/ocd-calls")
          ? new Response(
              JSON.stringify({
                detail: {
                  code: "VALIDATION_FAILED",
                  message: "Request validation failed.",
                  details: [{ loc: ["body", "call_start_local"], msg: "Invalid datetime" }],
                },
              }),
              { status: 422 },
            )
          : base(input, init),
      ),
    );
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Add call" }));
    await user.type(screen.getByLabelText("Call start"), "2026-07-20T01:10");
    await user.type(screen.getByLabelText("Call end"), "2026-07-20T03:25");
    await user.click(screen.getByRole("button", { name: "Record call + work time" }));

    // The product's own sentence, never the raw loc path or Pydantic's English.
    const message = await screen.findByText("Enter the call start as DD.MM.YYYY HH:MM.");
    const start = screen.getByLabelText("Call start");
    expect(start).toHaveAttribute("aria-invalid", "true");
    expect(start).toHaveAttribute("aria-describedby", message.id);
    expect(document.body.textContent ?? "").not.toContain("call_start_local");
    expect(document.body.textContent ?? "").not.toContain("Request validation failed.");
  });

  /**
   * The preview is a snapshot of the computation and the ELECTION is one of its
   * inputs — the panel renders `preview.election`. Saving an election invalidated
   * only the election read, so the visible preview went on stating the previous
   * choice (review R18).
   */
  it("drops the preview when the compensation election changes", async () => {
    const user = userEvent.setup();
    previewResponse = previewFixture;
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Preview OCD compensation" }));
    expect(await screen.findByText(/OCD compensation preview — not shared/)).toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: "Time compensation" }));

    await waitFor(() =>
      expect(screen.queryByText(/OCD compensation preview — not shared/)).toBeNull(),
    );
  });

  it("renders the private OCD compensation preview with duty classification", async () => {
    const user = userEvent.setup();
    previewResponse = previewFixture;
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Preview OCD compensation" }));
    expect(await screen.findByText(/OCD compensation preview — not shared/)).toBeInTheDocument();
    expect(screen.getByText(/4 hourly-wage unit/)).toBeInTheDocument();
    expect(screen.getByText(/Call work/)).toBeInTheDocument();
    expect(screen.getByText(/Duty 13:00/)).toBeInTheDocument();
    // `null` means the server could not classify it — a different fact from a
    // classification of "no", so it gets its own words.
    expect(screen.getByText(/short-notice compensation: not classified/)).toBeInTheDocument();
    expect(screen.getByText(/No monetary amount is calculated/)).toBeInTheDocument();
  });

  it("names the four-week notice expectation apart from the short-notice classification", async () => {
    // G4/H1: the assignment's own finding (OCD-002, four weeks, sourced) and the
    // preview's compensation classification (OCD-007) are DIFFERENT rules. Both
    // are individually correct, so neither threshold changes — the screen just
    // stops presenting them with the same words.
    const user = userEvent.setup();
    ocdResponse = [
      {
        ...assignment,
        findings: [
          {
            rule_id: "OCD-002",
            status: "WARNING",
            message: "OCD assignment was scheduled with less than four weeks notice.",
          },
        ],
      },
    ];
    previewResponse = {
      ...previewFixture,
      duty: { ...previewFixture.duty, short_notice: false },
    };
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Preview OCD compensation" }));
    // The finding keeps its sourced four-week wording…
    expect(
      await screen.findByText(/scheduled with less than four weeks notice/),
    ).toBeInTheDocument();
    // …while the classification names its own basis (the live <72 h rule)…
    expect(
      screen.getByText(/short-notice compensation: no \(communicated 72 h or more/),
    ).toBeInTheDocument();
    // …and the preview says outright that the two are different rules.
    expect(screen.getByText(/the four-week figure is the notice expectation/i)).toBeInTheDocument();
    expect(screen.getByText(/still being confirmed/i)).toBeInTheDocument();
    // An engine code is never employee-facing copy (labels.ts header).
    expect(screen.queryByText(/OCD-00/)).not.toBeInTheDocument();
  });

  it("saves an explicit compensation election for the assignment", async () => {
    const user = userEvent.setup();
    renderPage();
    // A mutually exclusive single choice resolves as a radio in a named group.
    const group = await screen.findByRole("group", { name: "Compensation election" });
    expect(within(group).getAllByRole("radio")).toHaveLength(2);
    await user.click(within(group).getByRole("radio", { name: "Time compensation" }));
    await waitFor(() =>
      expect(mutations.some((entry) => entry.url.includes("/compensation-elections"))).toBe(true),
    );
    const saved = mutations.find((entry) => entry.url.includes("/compensation-elections"));
    expect(saved?.body).toEqual({ context_type: "OCD", context_id: "assignment-1", mode: "TIME" });
    expect(await screen.findByText("Compensation choice saved.")).toBeInTheDocument();
  });

  it("does not repeat a remote call's mode as a travel note", async () => {
    // A remote call has no travel component, and its mode is already the chip
    // beside the interval — so no note may repeat the word "Remote" under it.
    ocdResponse = [
      {
        ...assignment,
        calls: [
          {
            id: "call-remote",
            mode: "REMOTE",
            call_start_at: "2026-07-20T01:10:00+02:00",
            call_end_at: "2026-07-20T03:25:00+02:00",
            travel_out_minutes: null,
            travel_back_minutes: null,
            comment: null,
            status: "RECORDED",
            version: 1,
            interval_id: "interval-ocd-1",
            span_start_at: "2026-07-20T00:45:00+02:00",
            span_end_at: "2026-07-20T03:50:00+02:00",
            elapsed_seconds: 8100,
          },
        ],
      },
    ];
    renderPage();
    const row = (await screen.findByText("Remote")).closest("li") as HTMLElement;
    expect(row.querySelector(".work-type-chip")?.textContent).toBe("Remote");
    expect(row.querySelectorAll("p.private-note")).toHaveLength(0);
  });
});

describe("OCD call correction", () => {
  it("connects a rejected field to its own message and states the requirement", async () => {
    // R9: the correction form's fields rendered their message but carried no
    // aria-invalid/aria-describedby, so a screen-reader user could not get from
    // the field to its rejection.
    const user = userEvent.setup();
    ocdResponse = [
      {
        ...assignment,
        calls: [
          {
            id: "call-1",
            mode: "ON_SITE",
            call_start_at: "2026-07-19T23:00:00+02:00",
            call_end_at: "2026-07-20T02:00:00+02:00",
            travel_out_minutes: null,
            travel_back_minutes: null,
            comment: null,
            status: "RECORDED",
            version: 1,
            linked_interval_id: "interval-1",
            span_start_at: "2026-07-19T23:00:00+02:00",
            span_end_at: "2026-07-20T02:00:00+02:00",
            elapsed_seconds: 3 * 3600,
          },
        ],
      },
    ];
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Correct call" }));

    // A pattern-invalid value states the requirement rather than leaving an
    // enabled button that native validation silently blocks.
    const start = screen.getByLabelText("Call start");
    fireEvent.change(start, { target: { value: "23:00" } });
    expect(
      await screen.findByText(/Enter the call start and end as DD.MM.YYYY HH:MM/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save correction" })).toBeDisabled();

    // And a server rejection is connected to the control it names.
    fireEvent.change(start, { target: { value: "19.07.2026 23:00" } });
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/v1/me/ocd?from="))
        return new Response(JSON.stringify(ocdResponse), { status: 200 });
      if (url.includes("/compensation-elections"))
        return new Response(JSON.stringify(null), { status: 200 });
      return new Response(
        JSON.stringify({
          detail: {
            code: "VALIDATION_FAILED",
            message: "Request validation failed.",
            details: [{ loc: ["body", "call_start_local"], msg: "String should match pattern" }],
          },
        }),
        { status: 422 },
      );
    });
    await user.click(screen.getByRole("button", { name: "Save correction" }));

    expect(
      await screen.findByText("Enter the call start as DD.MM.YYYY HH:MM."),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Call start")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("Call start")).toHaveAttribute(
      "aria-describedby",
      "ocd-correction-start-issue",
    );
    // The field the server did NOT name is left alone.
    expect(screen.getByLabelText("Call end")).not.toHaveAttribute("aria-invalid");
  });
});

/**
 * The employee's own AVAILABILITY plan entry (pilot slice, plan §6.1/§10.5).
 *
 * Two independent switches gate the two domains, so every case here also proves
 * the shift switch alone grants nothing on this page.
 */
describe("employee on-call availability plan entry", () => {
  async function openAddForm(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole("button", { name: "+ Add on-call duty" }));
    return user;
  }

  function postedAssignment(method: string) {
    return mutations.find(
      (entry) => entry.url.includes("/ocd-assignments") && entry.method === method,
    );
  }

  it("records an availability without ever asserting when the Company gave notice", async () => {
    identityCapabilities = ["OCD_PLAN_ENTRY_WRITE"];
    const user = userEvent.setup();
    renderPage();
    await openAddForm(user);
    await user.type(screen.getByLabelText("Starts"), "19.07.2026 19:00");
    await user.type(screen.getByLabelText("Ends"), "20.07.2026 08:00");
    await user.type(screen.getByLabelText(/When were you told/), "01.06.2026 10:00");
    await user.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => expect(postedAssignment("POST")).toBeDefined());
    const body = postedAssignment("POST")?.body as Record<string, unknown>;
    // The institutional notice instant is not the employee's to assert: the server
    // forces it NULL, and the request must not even offer one.
    expect(Object.keys(body)).not.toContain("scheduled_at");
    expect(body).toMatchObject({
      start_local: "2026-07-19T19:00",
      end_local: "2026-07-20T08:00",
      reported_notice_local: "2026-06-01T10:00",
    });
    expect(await screen.findByText("On-call availability recorded.")).toBeInTheDocument();
  });

  it("sends no notice key at all when the employee states none", async () => {
    // INV-15: the backend defaults the reported instant to none, so it stays
    // optional here and an empty field is "not stated" rather than a value.
    identityCapabilities = ["OCD_PLAN_ENTRY_WRITE"];
    const user = userEvent.setup();
    renderPage();
    await openAddForm(user);
    await user.type(screen.getByLabelText("Starts"), "19.07.2026 19:00");
    await user.type(screen.getByLabelText("Ends"), "20.07.2026 08:00");
    await user.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() => expect(postedAssignment("POST")).toBeDefined());
    const body = postedAssignment("POST")?.body as Record<string, unknown>;
    expect(Object.keys(body)).not.toContain("reported_notice_local");
  });

  it("states the window rule instead of leaving a button that does nothing", async () => {
    identityCapabilities = ["OCD_PLAN_ENTRY_WRITE"];
    const user = userEvent.setup();
    renderPage();
    await openAddForm(user);
    // An availability may cross midnight, so the order is the only rule — and it
    // is stated before the browser can block the submit silently.
    await user.type(screen.getByLabelText("Starts"), "20.07.2026 08:00");
    await user.type(screen.getByLabelText("Ends"), "19.07.2026 19:00");
    expect(
      await screen.findByText("The availability must end after it starts."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
  });

  it("does not offer the add control without OCD_PLAN_ENTRY_WRITE", async () => {
    identityCapabilities = [];
    renderPage();
    await screen.findByText(/No calls recorded/);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith("/api/v1/me", expect.anything()));
    expect(screen.queryByRole("button", { name: "+ Add on-call duty" })).not.toBeInTheDocument();
  });

  it("labels the employee's own availability, and its notice as reported", async () => {
    ocdResponse = [{ ...employeeAssignment, reported_notice_at: "2026-06-01T10:00:00+02:00" }];
    renderPage();
    expect(await screen.findByText("Recorded by you")).toBeInTheDocument();
    // Fail-closed provenance: nothing approved this availability, so the label
    // must not read "Approved plan". The NOUN is asserted too — the default is
    // "shift", and calling an on-call availability a shift is a false statement
    // about the employee's record on the one page whose subject is that
    // distinction (INV-10).
    expect(screen.getByText("Planned on-call duty")).toBeInTheDocument();
    expect(screen.queryByText("Planned shift")).not.toBeInTheDocument();
    expect(screen.queryByText("Approved plan")).not.toBeInTheDocument();
    expect(screen.getByText(/Employee-entered plan/)).toBeInTheDocument();
    // The reported notice is rendered, and labelled as REPORTED: no classifier
    // reads it, so it decides no allowance.
    expect(screen.getByText(/Notice you reported: 01 Jun 2026, 10:00/)).toBeInTheDocument();
    expect(screen.getByText(/decides no allowance/)).toBeInTheDocument();
  });

  it("hides Correct and Cancel when the availability already has a call, and says why", async () => {
    identityCapabilities = ["OCD_PLAN_ENTRY_WRITE"];
    ocdResponse = [{ ...employeeAssignment, calls: [recordedCall] }];
    renderPage();
    expect(await screen.findByText(/already has recorded calls/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Correct" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
  });

  it("hides Correct and Cancel for an availability the employee did not record", async () => {
    identityCapabilities = ["OCD_PLAN_ENTRY_WRITE"];
    ocdResponse = [{ ...assignment, version: 1 }];
    renderPage();
    await screen.findByText(/No calls recorded/);
    expect(screen.queryByRole("button", { name: "Correct" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
  });

  it("hides both when only the OTHER domain's switch is held", async () => {
    identityCapabilities = ["SHIFT_PLAN_ENTRY_WRITE"];
    ocdResponse = [{ ...employeeAssignment }];
    renderPage();
    await screen.findByText("Recorded by you");
    expect(screen.queryByRole("button", { name: "Correct" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "+ Add on-call duty" })).not.toBeInTheDocument();
  });

  it("offers no Correct or Cancel when the row reports no version token", async () => {
    // The token must be the row's own: a remembered one is stale by construction
    // and a guessed one would corrupt a correctable record, so the controls are
    // withheld rather than wired to a value the payload never carried.
    identityCapabilities = ["OCD_PLAN_ENTRY_WRITE"];
    ocdResponse = [{ ...employeeAssignment, version: undefined }];
    renderPage();
    await screen.findByText("Recorded by you");
    expect(screen.queryByRole("button", { name: "Correct" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
  });

  it("corrects an availability with the version token from the row", async () => {
    identityCapabilities = ["OCD_PLAN_ENTRY_WRITE"];
    ocdResponse = [{ ...employeeAssignment, reported_notice_at: "2026-06-01T10:00:00+02:00" }];
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Correct" }));
    // The window arrives prefilled from the row, so an untouched field writes back
    // the value it already had instead of being silently dropped.
    expect(screen.getByLabelText("Starts")).toHaveValue("19.07.2026 19:00");
    expect(screen.getByLabelText("Ends")).toHaveValue("20.07.2026 08:00");
    fireEvent.change(screen.getByLabelText("Starts"), {
      target: { value: "19.07.2026 18:30" },
    });
    await user.click(screen.getByRole("button", { name: "Save correction" }));

    await waitFor(() => expect(postedAssignment("PATCH")).toBeDefined());
    expect(postedAssignment("PATCH")?.body).toEqual({
      version: 1,
      start_local: "2026-07-19T18:30",
      end_local: "2026-07-20T08:00",
      reported_notice_local: "2026-06-01T10:00",
    });
    expect(await screen.findByText("Availability corrected.")).toBeInTheDocument();
  });

  it("clears a reported notice by emptying the field", async () => {
    // §4.3.1 presence merge: an OMITTED key preserves the instant and an explicit
    // null clears it, so an emptied field must send the null — otherwise a notice
    // entered by mistake could never be withdrawn.
    identityCapabilities = ["OCD_PLAN_ENTRY_WRITE"];
    ocdResponse = [{ ...employeeAssignment, reported_notice_at: "2026-06-01T10:00:00+02:00" }];
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Correct" }));
    await user.clear(screen.getByLabelText(/When were you told/));
    await user.click(screen.getByRole("button", { name: "Save correction" }));

    await waitFor(() => expect(postedAssignment("PATCH")).toBeDefined());
    expect(postedAssignment("PATCH")?.body).toMatchObject({ reported_notice_local: null });
  });

  it("cancels with the version token and nothing else", async () => {
    identityCapabilities = ["OCD_PLAN_ENTRY_WRITE"];
    ocdResponse = [{ ...employeeAssignment }];
    const user = userEvent.setup();
    renderPage();
    // A lifecycle write is two-step: the resting control only arms it.
    await user.click(await screen.findByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "Cancel this availability" }));

    await waitFor(() =>
      expect(mutations.some((entry) => entry.url.includes("/cancel"))).toBe(true),
    );
    const cancelled = mutations.find((entry) => entry.url.includes("/cancel"));
    expect(cancelled?.method).toBe("POST");
    expect(cancelled?.body).toEqual({ version: 1 });
    expect(await screen.findByText("Availability cancelled.")).toBeInTheDocument();
  });
});
