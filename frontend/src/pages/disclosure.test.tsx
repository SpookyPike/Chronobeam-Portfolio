import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AccessRequestsPage } from "./access-requests";
import { RecipientQueuePage } from "./recipient-queue";
import { HumanPreview, SubmissionsPage } from "./submissions";

vi.mock("@tanstack/react-router", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@tanstack/react-router")>()),
  Link: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

function renderPage(page: ReactNode) {
  return render(<QueryClientProvider client={new QueryClient()}>{page}</QueryClientProvider>);
}

let fetchMock: ReturnType<typeof vi.fn>;
let identity: { subject: string; display_name: string; capabilities: string[] };
let recipientsResponse: unknown;
/** The OCD assignments the picker offers — mutable so a test can add a second
 *  one and change the scope while a preview request is in flight. */
let ocdAssignments: Array<{ id: string; start_at: string }> = [
  { id: "ocd-1", start_at: "2026-07-12T19:00:00+02:00" },
];
/** When set, the compensation-preview request answers with this promise instead
 *  of immediately — the in-flight window a scope change must survive. */
let deferredPreview: Promise<Response> | null = null;

beforeEach(() => {
  recipientsResponse = [{ subject: "demo-recipient", readable: true }];
  identity = {
    subject: "employee",
    display_name: "Employee",
    capabilities: [
      "OWN_LEDGER_READ",
      "OWN_LEDGER_WRITE",
      "OWN_SUBMISSION_CREATE",
      "OWN_DISCLOSURE_GRANT",
    ],
  };
  fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/v1/me/submissions") return new Response(JSON.stringify([]), { status: 200 });
    if (url === "/api/v1/me/submission-recipients")
      return new Response(JSON.stringify(recipientsResponse), { status: 200 });
    if (url.includes("/api/v1/me/shifts?"))
      return new Response(
        JSON.stringify([
          { id: "shift-1", approved_revision: { planned_start_at: "2026-07-10T19:00:00+02:00" } },
        ]),
        { status: 200 },
      );
    if (url.includes("/api/v1/me/ocd?"))
      return new Response(JSON.stringify(ocdAssignments), { status: 200 });
    if (url.includes("report-preview"))
      return new Response(
        JSON.stringify({
          report_type: "SHIFT_REPORT",
          employee_identifier: "emp-1",
          shift_assignment_id: "shift-1",
          planned_start_at: "2026-07-10T19:00:00+02:00",
          findings: [{ rule_id: "PRIVATE", status: "WARNING" }],
          preview_fingerprint: "a".repeat(64),
        }),
        { status: 200 },
      );
    if (url.includes("compensation-preview")) {
      if (deferredPreview) return deferredPreview;
      return new Response(
        JSON.stringify({
          report_type: "OCD_COMPENSATION",
          employee_identifier: "emp-1",
          ocd_assignment_id: "ocd-1",
          calls: [],
          findings: [{ rule_id: "PRIVATE", status: "WARNING" }],
          preview_fingerprint: "a".repeat(64),
        }),
        { status: 200 },
      );
    }
    if (url.includes("/api/v1/me/submissions/SHIFT_REPORT"))
      return new Response(
        JSON.stringify({
          detail: {
            code: "SUBMISSION_CHANGED",
            message: "Facts changed since preview; re-preview before submitting.",
          },
        }),
        { status: 409 },
      );
    if (url.includes("/api/v1/me/submissions/OCD_COMPENSATION"))
      return new Response(
        JSON.stringify({
          detail: {
            code: "SUBMISSION_CHANGED",
            message: "Facts changed since preview; re-preview before submitting.",
          },
        }),
        { status: 409 },
      );
    if (url === "/api/v1/me") return new Response(JSON.stringify(identity), { status: 200 });
    if (url === "/api/v1/me/access-requests")
      return new Response(
        JSON.stringify([
          {
            id: "request-1",
            requester_principal: "hr-requester",
            purpose: "RANDOM_CHECK",
            period_start: "2026-07-01",
            period_end: "2026-07-31",
            view_type: "work-time-record",
            message: "Please review",
            status: "PENDING",
            created_at: "2026-07-01T08:00:00Z",
            closed_at: null,
          },
        ]),
        { status: 200 },
      );
    if (url.includes("/grant"))
      return new Response(JSON.stringify({ id: "grant-1" }), { status: 200 });
    if (url === "/api/v1/recipient/submissions")
      return new Response(
        JSON.stringify([
          {
            id: "package-1",
            purpose: "OCD_COMPENSATION",
            period_start: "2026-07-01",
            period_end: "2026-07-31",
            version: 1,
            status: "SUBMITTED",
            employee_identifier: "emp-1",
            created_at: "2026-07-31T08:00:00Z",
            read_at: null,
            supersedes_id: null,
          },
        ]),
        { status: 200 },
      );
    if (url === "/api/v1/recipient/submissions/package-1")
      return new Response(
        JSON.stringify({
          id: "package-1",
          purpose: "OCD_COMPENSATION",
          period_start: "2026-07-01",
          period_end: "2026-07-31",
          version: 1,
          status: "SUBMITTED",
          employee_identifier: "emp-1",
          created_at: "2026-07-31T08:00:00Z",
          read_at: "2026-07-31T09:00:00Z",
          supersedes_id: null,
          payload: {
            report_type: "OCD_COMPENSATION",
            employee_identifier: "emp-1",
            assignment_start_at: "2026-07-19T19:00:00+02:00",
            assignment_end_at: "2026-07-20T07:00:00+02:00",
            calls: [
              {
                call_id: "call-1",
                mode: "ON_SITE",
                call_start_at: "2026-07-19T23:42:00+02:00",
                call_end_at: "2026-07-20T00:18:00+02:00",
                travel_out_minutes: 30,
                travel_back_minutes: 30,
                span_start_at: "2026-07-19T23:12:00+02:00",
                span_end_at: "2026-07-20T00:48:00+02:00",
                // The credited span (call window + travel both ways), which is
                // what the server computes — not the call window's own length.
                elapsed_seconds: 5760,
              },
            ],
            classified_components: [
              {
                rule_id: "OCD-002",
                component: "OCD_CALL_BASE",
                minutes: 36,
                time_equivalent_minutes: null,
              },
            ],
            duty: {
              duty_minutes: 780,
              duty_kind: "DUTY_GE_12H",
              units: 4,
              basis_points: null,
              short_notice: false,
            },
            election: { id: null, mode: "TIME", version: 1 },
          },
        }),
        { status: 200 },
      );
    return new Response(JSON.stringify([]), { status: 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
});

describe("Disclosure UI", () => {
  it("submits OCD with the preview fingerprint and prompts re-preview on 409", async () => {
    const user = userEvent.setup();
    renderPage(<SubmissionsPage month="2026-07" />);
    const assignmentChoices = await screen.findAllByLabelText("Assignment");
    await user.selectOptions(assignmentChoices[1], "ocd-1");
    await user.click(
      screen.getByRole("button", { name: "Preview exact package — OCD compensation" }),
    );
    expect(await screen.findByText("What the recipient will see")).toBeInTheDocument();
    expect(screen.queryByText("PRIVATE")).not.toBeInTheDocument();
    // Nothing is pre-selected: sending a package needs an explicit choice.
    await user.click(screen.getAllByRole("checkbox", { name: "demo-recipient" })[1]);
    await user.click(screen.getByRole("button", { name: "Submit this OCD compensation package" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Facts changed — re-preview");
    const post = fetchMock.mock.calls.find(([input]) =>
      String(input).includes("/api/v1/me/submissions/OCD_COMPENSATION"),
    );
    expect(post).toBeTruthy();
    expect(JSON.parse((post![1] as RequestInit).body as string)).toMatchObject({
      recipients: ["demo-recipient"],
    });
  });

  /**
   * A preview answers for ONE scope. If the employee changes the scope while the
   * request is in flight, the response describes the OLD scope: it would show
   * assignment A's recipient projection while B is selected and carry A's
   * fingerprint into a submit aimed at B (review R19). Submission fails safely on
   * the fingerprint mismatch, but the DISPLAY is what the employee reads.
   */
  it("discards a preview response that answers for a scope no longer selected", async () => {
    const user = userEvent.setup();
    ocdAssignments = [
      { id: "ocd-1", start_at: "2026-07-12T19:00:00+02:00" },
      { id: "ocd-2", start_at: "2026-07-19T19:00:00+02:00" },
    ];
    let release!: (value: Response) => void;
    deferredPreview = new Promise<Response>((resolve) => {
      release = resolve;
    });
    renderPage(<SubmissionsPage month="2026-07" />);
    const assignmentChoices = await screen.findAllByLabelText("Assignment");
    await user.selectOptions(assignmentChoices[1], "ocd-1");
    await user.click(
      screen.getByRole("button", { name: "Preview exact package — OCD compensation" }),
    );

    // The employee moves to another assignment before the answer arrives.
    await user.selectOptions(assignmentChoices[1], "ocd-2");
    release(
      new Response(
        JSON.stringify({
          report_type: "OCD_COMPENSATION",
          employee_identifier: "emp-1",
          ocd_assignment_id: "ocd-1",
          calls: [],
          findings: [],
          preview_fingerprint: "a".repeat(64),
        }),
        { status: 200 },
      ),
    );

    // The response must be PROCESSED before its absence is asserted: `waitFor`
    // returns immediately when the assertion already holds, which would pass
    // before the stale payload had landed and prove nothing. The preview button
    // re-enables only once the mutation has settled, so that is the signal.
    const previewButton = screen.getByRole("button", {
      name: "Preview exact package — OCD compensation",
    });
    await waitFor(() => expect(previewButton).not.toBeDisabled());

    // A's projection must never be presented as B's.
    expect(screen.queryByText("What the recipient will see")).not.toBeInTheDocument();
  });

  it("loads the selectable recipients from the server, not a client constant", async () => {
    ocdAssignments = [{ id: "ocd-1", start_at: "2026-07-12T19:00:00+02:00" }];
    deferredPreview = null;
    recipientsResponse = [{ subject: "hr-requester", readable: true }];
    renderPage(<SubmissionsPage month="2026-07" />);
    expect(await screen.findByRole("checkbox", { name: "hr-requester" })).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /demo-recipient/ })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/me/submission-recipients", expect.anything());
  });

  it("preselects no recipient and blocks the submit until one is chosen", async () => {
    const user = userEvent.setup();
    renderPage(<SubmissionsPage month="2026-07" />);
    await waitFor(() =>
      expect(screen.getAllByRole("checkbox", { name: "demo-recipient" })).toHaveLength(4),
    );
    const boxes = screen.getAllByRole("checkbox", { name: "demo-recipient" }); // one picker per card
    boxes.forEach((box) => expect(box).not.toBeChecked());
    const assignmentChoices = await screen.findAllByLabelText("Assignment");
    await user.selectOptions(assignmentChoices[1], "ocd-1");
    await user.click(
      screen.getByRole("button", { name: "Preview exact package — OCD compensation" }),
    );
    expect(await screen.findByText("What the recipient will see")).toBeInTheDocument();
    const submit = screen.getByRole("button", { name: "Submit this OCD compensation package" });
    expect(submit).toBeDisabled();
    await user.click(boxes[1]);
    expect(
      screen.getByRole("button", { name: "Submit this OCD compensation package" }),
    ).toBeEnabled();
  });

  it("renders an unreadable recipient disabled with its warning", async () => {
    recipientsResponse = [{ subject: "demo-recipient", readable: false }];
    renderPage(<SubmissionsPage month="2026-07" />);
    const box = await screen.findByRole("checkbox", { name: /demo-recipient/ });
    expect(box).toBeDisabled();
    await waitFor(() => expect(screen.getAllByRole("note")).toHaveLength(4));
    // every card explains the same registry gap
    expect(screen.getAllByRole("note")[0]).toHaveTextContent(/cannot read submissions/);
  });

  it("renders an assigned recipient queue and only opens the frozen package", async () => {
    const user = userEvent.setup();
    identity = {
      subject: "recipient",
      display_name: "Recipient",
      capabilities: ["SUBMISSION_READ_ASSIGNED"],
    };
    renderPage(<RecipientQueuePage />);
    expect(await screen.findByRole("button", { name: "Open package" })).toBeInTheDocument();
    expect(screen.queryByText("employee ledger")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open package" }));
    expect(await screen.findByText("What you will receive")).toBeInTheDocument();
    // The recipient gets the same human rendering the employee previewed.
    expect(
      screen
        .getByRole("heading", { name: "What you will receive" })
        .closest("section")
        ?.querySelector(".human-preview"),
    ).toHaveTextContent("Employee reference: emp-1");
    // 7A: EVERY substantive field of the recipient projection is rendered in
    // words — the individual calls with their travel, the duty calculation and
    // the classified component. These are exactly the facts the removed raw-JSON
    // disclosure used to carry, and this assertion is what stops the human
    // renderer from quietly dropping them again.
    const preview = screen
      .getByRole("heading", { name: "What you will receive" })
      .closest("section")!
      .querySelector(".human-preview")!;
    expect(preview).toHaveTextContent("Assignment: 19 Jul 2026, 19:00 → 20 Jul 2026, 07:00");
    expect(preview).toHaveTextContent("19 Jul 2026, 23:42 → 20 Jul 2026, 00:18 · On-site · 0:36");
    // The effective-work span is a required field of the disclosed projection —
    // the call window widened by travel, and what the package credits.
    expect(preview).toHaveTextContent(
      "Effective work 19 Jul 2026, 23:12 → 20 Jul 2026, 00:48 · 1:36",
    );
    expect(preview).toHaveTextContent("travel out 0:30, back 0:30");
    expect(preview).toHaveTextContent("Duty 13:00 · 4 hourly-wage unit(s)");
    // The three duty states must not collapse: `false` is a fact, not silence.
    // And the classification names its OWN basis — the same two words "short
    // notice" also carry the four-week notice expectation (acceptance G4/H1).
    expect(preview).toHaveTextContent(
      "short-notice compensation: no (communicated 72 h or more before the duty)",
    );
    expect(preview).toHaveTextContent("Call work: 0:36");
    expect(preview).toHaveTextContent("Election: Time compensation");
    expect(
      screen.getByText("Employee-private notes are never part of a package."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/OCD_COMPENSATION/)).not.toBeInTheDocument();
    expect(document.querySelector(".recipient-detail details")).toBeNull();
  });

  it("takes an open package off screen when the filter excludes it", async () => {
    const user = userEvent.setup();
    identity = {
      subject: "recipient",
      display_name: "Recipient",
      capabilities: ["SUBMISSION_READ_ASSIGNED"],
    };
    renderPage(<RecipientQueuePage />);
    await user.click(await screen.findByRole("button", { name: "Open package" }));
    expect(await screen.findByText("What you will receive")).toBeInTheDocument();
    // The seeded package is an OCD one, so the overtime filter excludes it: the
    // detail must not stay on screen beside the "Overtime — pay" tab.
    await user.click(screen.getByRole("button", { name: "Overtime — pay" }));
    expect(screen.queryByText("What you will receive")).not.toBeInTheDocument();
    expect(screen.getByText(/No packages assigned to you for this filter/)).toBeInTheDocument();
  });

  it("shows the employee grant action with its exact recipient and review path", async () => {
    renderPage(<AccessRequestsPage />);
    expect(
      await screen.findByRole("button", { name: "Open exactly this scope to hr-requester" }),
    ).toBeInTheDocument();
  });

  /* The grant/decline buttons render only while a request is PENDING, so a
   * resolved request used to show NOTHING about its outcome: the row looked
   * unanswered and the "Review … first" link read as an instruction to act on a
   * decision the employee had already made. The employee must be able to read
   * back the privacy decision they took on the page that exists to take it. */
  it("states the outcome of an already-resolved access request to the employee", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/me")
        return new Response(
          JSON.stringify({
            subject: "employee",
            display_name: "Employee",
            capabilities: ["OWN_LEDGER_READ", "OWN_DISCLOSURE_GRANT"],
          }),
          { status: 200 },
        );
      if (url === "/api/v1/me/access-requests")
        return new Response(
          JSON.stringify([
            {
              id: "request-granted",
              requester_principal: "hr-requester",
              purpose: "RANDOM_CHECK",
              period_start: "2026-07-01",
              period_end: "2026-07-31",
              view_type: "work-time-record",
              message: null,
              status: "GRANTED",
              created_at: "2026-07-01T08:00:00Z",
              closed_at: "2026-07-02T08:00:00Z",
              grant_id: "grant-1",
            },
          ]),
          { status: 200 },
        );
      return new Response(JSON.stringify({}), { status: 200 });
    });
    renderPage(<AccessRequestsPage />);
    expect(await screen.findByText("Opened by the employee")).toBeInTheDocument();
    // A resolved request is not an instruction: no grant/decline, no review prompt.
    expect(
      screen.queryByRole("button", { name: "Open exactly this scope to hr-requester" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/Review July 2026 first/)).not.toBeInTheDocument();
  });

  it("renders HR request metadata without ledger data before a grant", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/v1/me")
        return new Response(
          JSON.stringify({
            subject: "hr-requester",
            display_name: "HR",
            capabilities: ["ACCESS_REQUEST_CREATE", "DISCLOSURE_READ_GRANTED"],
          }),
          { status: 200 },
        );
      if (url === "/api/v1/hr/access-requests")
        return new Response(
          JSON.stringify([
            {
              id: "request-1",
              requester_principal: "hr-requester",
              purpose: "RANDOM_CHECK",
              period_start: "2026-07-01",
              period_end: "2026-07-31",
              view_type: "work-time-record",
              message: "Check",
              status: "PENDING",
              created_at: "2026-07-01T08:00:00Z",
              closed_at: null,
            },
          ]),
          { status: 200 },
        );
      return new Response(JSON.stringify({}), { status: 200 });
    });
    renderPage(<AccessRequestsPage />);
    expect(await screen.findByRole("heading", { name: "Your requests" })).toBeInTheDocument();
    expect(await screen.findByText(/Random check/)).toBeInTheDocument();
    expect(screen.queryByText(/Intervals:/)).not.toBeInTheDocument();
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith("/api/v1/hr/access-requests", expect.anything()),
    );
  });

  it("states the HR request requirement at the primary and issues the request once it is met", async () => {
    // An empty employee ID and an incomplete month used to be blocked by native
    // validation only: no request, no message, an enabled button that did
    // nothing (acceptance E3).
    const user = userEvent.setup();
    identity = {
      subject: "hr-requester",
      display_name: "HR",
      capabilities: ["ACCESS_REQUEST_CREATE"],
    };
    renderPage(<AccessRequestsPage />);
    await screen.findByRole("heading", { name: "HR request creation" });
    const submit = screen.getByRole("button", { name: "Request random check" });
    expect(submit).toBeDisabled();
    const requirement = screen.getByText(/Enter the employee's external ID/);
    expect(submit).toHaveAttribute("aria-describedby", requirement.id);
    expect(screen.getByLabelText("Employee ID")).toHaveAttribute("aria-invalid", "true");

    await user.type(screen.getByLabelText("Employee ID"), "emp-0001");
    // The month is still an incomplete fragment, so the primary states THAT.
    expect(screen.getByText(/Enter the month as MM\.YYYY/)).toBeInTheDocument();
    expect(submit).toBeDisabled();
    expect(screen.getByLabelText("Month")).toHaveAttribute("aria-invalid", "true");

    await user.type(screen.getByLabelText("Month"), "10.2026");
    expect(submit).toBeEnabled();
    await user.click(submit);
    await waitFor(() => {
      const post = fetchMock.mock.calls.find(
        ([input, init]) =>
          String(input) === "/api/v1/hr/access-requests" &&
          (init as RequestInit | undefined)?.method === "POST",
      );
      expect(post).toBeTruthy();
      expect(JSON.parse(String((post![1] as RequestInit).body))).toMatchObject({
        employee_external_id: "emp-0001",
        month: "2026-10",
      });
    });
  });

  it("states the recipient requirement at the submit instead of leaving it inert", async () => {
    const user = userEvent.setup();
    renderPage(<SubmissionsPage month="2026-07" />);
    const assignmentChoices = await screen.findAllByLabelText("Assignment");
    await user.selectOptions(assignmentChoices[1], "ocd-1");
    await user.click(
      screen.getByRole("button", { name: "Preview exact package — OCD compensation" }),
    );
    const submit = await screen.findByRole("button", {
      name: "Submit this OCD compensation package",
    });
    expect(submit).toBeDisabled();
    const requirement = screen.getByText(/Select at least one recipient/);
    expect(submit).toHaveAttribute("aria-describedby", requirement.id);

    await user.click(screen.getAllByRole("checkbox", { name: "demo-recipient" })[1]);
    expect(screen.queryByText(/Select at least one recipient/)).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Submit this OCD compensation package" }),
    ).toBeEnabled();
  });

  it("does not render employee request controls for an unassigned principal", async () => {
    identity = { subject: "supervisor", display_name: "Supervisor", capabilities: [] };
    renderPage(<AccessRequestsPage />);
    expect(
      await screen.findByText("This account cannot create or respond to access requests."),
    ).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalledWith("/api/v1/me/access-requests", expect.anything());
  });
});

describe("HumanPreview", () => {
  // A shift report has no compensation election, so naming one there is a term
  // the reader has nothing to attach to.
  it("does not name a compensation election on a shift report", () => {
    renderPage(
      <HumanPreview
        purpose="SHIFT_REPORT"
        audience="recipient"
        preview={{ report_type: "SHIFT_REPORT", employee_identifier: "emp-1" }}
      />,
    );
    expect(screen.queryByText(/Election/)).not.toBeInTheDocument();
    expect(screen.queryByText(/not chosen/)).not.toBeInTheDocument();
  });

  // An all-missing pair rendered as "— → —" told the reader nothing; the line
  // still always appears (7B) but says so in words.
  it("words the missing actual time instead of two dashes", () => {
    renderPage(
      <HumanPreview
        purpose="SHIFT_REPORT"
        audience="recipient"
        preview={{ report_type: "SHIFT_REPORT", employee_identifier: "emp-1" }}
      />,
    );
    expect(screen.getByText(/Actual: not recorded yet/)).toBeInTheDocument();
    expect(screen.queryByText(/— → —/)).not.toBeInTheDocument();
  });

  it("names the election on an overtime package", () => {
    renderPage(
      <HumanPreview
        purpose="OVERTIME_PAID"
        audience="recipient"
        preview={{
          report_type: "OVERTIME",
          employee_identifier: "emp-1",
          election: { mode: "PAID" },
        }}
      />,
    );
    expect(screen.getByText(/Election:/)).toBeInTheDocument();
  });
});
