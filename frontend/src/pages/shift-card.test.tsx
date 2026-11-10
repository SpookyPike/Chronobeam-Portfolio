import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ShiftPlanActualCard } from "./shift-card";
import { decisionMakerRoleFieldLabel, decisionMakerRoleOther } from "../lib/labels";
import type { Shift } from "../lib/api";

const plannedShift = {
  id: "shift-1",
  shift_kind: "NIGHT",
  status: "PLANNED" as const,
  // What the employee's own write path always produces (the server forces it,
  // ACCESS-017). Read through `planSourceLabel`, so it reads "Planned shift" and
  // never "Approved plan".
  plan_source: "EMPLOYEE_RECORDED",
  approved_revision: {
    id: "revision-1",
    revision_no: 1,
    planned_start_at: "2026-07-31T17:00:00Z",
    planned_end_at: "2026-08-01T05:00:00Z",
    communicated_at: null,
  },
  actual_start_at: null,
  actual_end_at: null,
  deviation: null,
  findings: [
    { rule_id: "SHIFT-003", status: "WARNING" as const, message: "Paid break facts required." },
  ],
  paid_break_status: "EXPLICIT_FACT_REQUIRED" as const,
  paid_breaks: [],
};

/** One recorded paid break on the shift above. */
const paidBreak = {
  id: "break-1",
  kind: "PAID_SHIFT_BREAK" as const,
  start_at: "2026-08-01T00:30:00+02:00",
  end_at: "2026-08-01T01:00:00+02:00",
  start_local: "2026-08-01T00:30:00",
  end_local: "2026-08-01T01:00:00",
  elapsed_seconds: 1800,
  linked_shift_assignment_id: "shift-1",
  version: 1,
  supersedes_id: null,
  client_request_id: null,
};

/** A shift whose paid break fact is already linked to the assignment. */
const recordedBreakShift = {
  ...plannedShift,
  paid_break_status: "RECORDED" as const,
  paid_breaks: [paidBreak],
};

/** Deviated shift: the only state in which the report preview is reachable. */
const deviatedShift = {
  ...recordedBreakShift,
  actual_start_at: "2026-07-31T17:05:00Z",
  actual_end_at: "2026-08-01T05:00:00Z",
  deviation: {
    id: "deviation-1",
    deviation_type: "SHORTENED",
    actual_start_at: "2026-07-31T17:05:00Z",
    actual_end_at: "2026-08-01T05:00:00Z",
    noticed_or_decided_at: null,
    decision_maker_type: null,
    reason: null,
    version: 1,
  },
};

/** Preview carries its own requirement state, independent of the card prop. */
const previewPayload = {
  report_type: "SHIFT_REPORT",
  policy_version: "2026.1",
  policy_status: "PROVISIONAL",
  employee_identifier: "employee-1",
  shift_assignment_id: "shift-1",
  planned_start_at: "2026-07-31T17:00:00Z",
  planned_end_at: "2026-08-01T05:00:00Z",
  actual_start_at: null,
  actual_end_at: null,
  deviation_type: "SHORTENED",
  findings: [],
  paid_break_status: "EXPLICIT_FACT_REQUIRED",
  plan_source: "EMPLOYEE_RECORDED",
  compensation_review_status: "NOT_APPLICABLE",
  preview_fingerprint: "fingerprint-1",
};

function renderCard(shift: Shift = plannedShift, client: QueryClient = new QueryClient()) {
  return render(
    <QueryClientProvider client={client}>
      <ShiftPlanActualCard shift={shift} />
    </QueryClientProvider>,
  );
}

/** Open the card's `More shift actions` menu.
 *
 * The correction, cancellation, paid-break and mistaken-entry controls moved
 * behind ONE menu (owner directive, 2026-09-26), so a test that clicks one of them
 * must open the menu first. Going through this helper keeps the tests exercising
 * the real user path instead of reaching into the DOM. */
async function openShiftActions(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "More shift actions" }));
}

/** Is this secondary/destructive control OFFERED?
 *
 * The four controls live behind the actions menu, so the question has to be asked
 * through it: open the menu when it exists and look inside. A plan that offers
 * none of them has no menu at all — and that is a genuine absence, not a hidden
 * control, because the card only renders the toggle when at least one item is
 * available. Returns null when the control cannot be reached. */
async function queryShiftAction(
  user: ReturnType<typeof userEvent.setup>,
  name: string,
): Promise<HTMLElement | null> {
  const toggle = screen.queryByRole("button", { name: "More shift actions" });
  if (!toggle) return null;
  await user.click(toggle);
  return screen.queryByRole("button", { name });
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(plannedShift), { status: 200 })),
  );
});

afterEach(cleanup);

it("confirms an exact approved shift in one action without exposing a report", async () => {
  const user = userEvent.setup();
  renderCard();
  await user.click(screen.getByRole("button", { name: "Confirm worked as planned" }));
  expect(vi.mocked(fetch)).toHaveBeenCalledWith(
    "/api/v1/me/shifts/shift-1/confirm",
    expect.objectContaining({ method: "POST" }),
  );
  expect(screen.queryByText(/Shift Report preview/)).not.toBeInTheDocument();
});

it("shows the structured deviation form only when requested", async () => {
  const user = userEvent.setup();
  renderCard();
  expect(screen.queryByRole("heading", { name: "Actual difference" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Record a deviation" }));
  expect(screen.getByRole("heading", { name: "Actual difference" })).toBeInTheDocument();
  expect(screen.getByLabelText("Decision maker / reported role")).toBeInTheDocument();
});

it("records a paid shift break linked to this assignment", async () => {
  const user = userEvent.setup();
  renderCard();
  expect(screen.getByText("Not recorded yet")).toBeInTheDocument();
  await openShiftActions(user);
  await user.click(screen.getByRole("button", { name: "Record a paid shift break" }));
  await user.type(screen.getByLabelText("Break start"), "01.08.2026 00:30");
  await user.type(screen.getByLabelText("Break end"), "01.08.2026 01:00");
  // A date the employee STATED is shown in full, never reduced to a bare time —
  // unless it IS the day the field's own rule derives, which for a post-midnight
  // break on this night shift is the next morning.
  expect(screen.getByLabelText("Break start")).toHaveValue("00:30");
  await user.click(screen.getByRole("button", { name: "Save paid break" }));
  const breakCall = await waitFor(() => {
    const call = vi.mocked(fetch).mock.calls.find(([url]) => url === "/api/v1/me/break-intervals");
    if (!call) throw new Error("no paid shift break was posted");
    return call;
  });
  expect(breakCall[1]).toMatchObject({ method: "POST" });
  expect(JSON.parse(String(breakCall[1]?.body))).toMatchObject({
    kind: "PAID_SHIFT_BREAK",
    linked_shift_assignment_id: "shift-1",
    start_local: "2026-08-01T00:30",
    end_local: "2026-08-01T01:00",
  });
  expect(
    await screen.findByText("Paid shift break recorded. The shift report will show it."),
  ).toBeInTheDocument();
});

/* Owner request, 2026-10-08: a shift is at most a day long, so re-typing the date
 * and the year for every break is friction. A bare time is enough for a moment on
 * the shift's own day, and the NIGHT fixture is the case that would otherwise
 * need a second control: a post-midnight break must land on the NEXT day, which
 * the field derives from the shift's own start time rather than asking. */
it("accepts a bare time for the shift's own day and rolls a post-midnight break", async () => {
  const user = userEvent.setup();
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify({}), { status: 200 })),
  );
  renderCard();
  await openShiftActions(user);
  await user.click(screen.getByRole("button", { name: "Record a paid shift break" }));
  await user.type(screen.getByLabelText("Break start"), "00:30");
  await user.type(screen.getByLabelText("Break end"), "01:00");
  await user.click(screen.getByRole("button", { name: "Save paid break" }));

  const breakCall = await waitFor(() => {
    const call = vi.mocked(fetch).mock.calls.find(([url]) => url === "/api/v1/me/break-intervals");
    if (!call) throw new Error("no paid shift break was posted");
    return call;
  });
  expect(JSON.parse(String(breakCall[1]?.body))).toMatchObject({
    kind: "PAID_SHIFT_BREAK",
    // The shift runs 31 Jul 19:00 -> 1 Aug 07:00, so 00:30 is the NEXT morning.
    start_local: "2026-08-01T00:30",
    end_local: "2026-08-01T01:00",
  });
});

/* A shift can hold more than one paid break, and the backend has always stored
 * them that way: `paid_break_status` is RECORDED when ANY is linked. The card used
 * to hide the action once one existed, so a real second break could not be
 * recorded from the surface that owns the shift. */
it("records a second paid break on a shift that already has one", async () => {
  const user = userEvent.setup();
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify({}), { status: 200 })),
  );
  renderCard(recordedBreakShift);
  // The recorded break is listed, so the employee can see what is already there.
  expect(screen.getByText("Recorded")).toBeInTheDocument();
  expect(screen.getByText("01 Aug 2026, 00:30 → 01 Aug 2026, 01:00")).toBeInTheDocument();

  await openShiftActions(user);
  await user.click(screen.getByRole("button", { name: "Record a paid shift break" }));
  await user.type(screen.getByLabelText("Break start"), "04:00");
  await user.type(screen.getByLabelText("Break end"), "04:30");
  await user.click(screen.getByRole("button", { name: "Save paid break" }));

  const breakCall = await waitFor(() => {
    const call = vi.mocked(fetch).mock.calls.find(([url]) => url === "/api/v1/me/break-intervals");
    if (!call) throw new Error("the second paid shift break was not posted");
    return call;
  });
  expect(JSON.parse(String(breakCall[1]?.body))).toMatchObject({
    kind: "PAID_SHIFT_BREAK",
    linked_shift_assignment_id: "shift-1",
    start_local: "2026-08-01T04:00",
    end_local: "2026-08-01T04:30",
  });
  // The fields are cleared, so the next one is a NEW break rather than an edit of
  // the one just saved.
  expect(screen.getByLabelText("Break start")).toHaveValue("");
});

it("takes the paid-break state from the server, not from its own mutation", async () => {
  // The card must never keep claiming "Recorded" on the strength of its own
  // successful POST: the employee can later correct that break to UNPAID or
  // delete it in the month timeline, and the requirement becomes outstanding
  // again. A mutation flag would stay "success" and hide the corrective action.
  const user = userEvent.setup();
  const client = new QueryClient();
  const card = (
    <QueryClientProvider client={client}>
      <ShiftPlanActualCard shift={plannedShift} />
    </QueryClientProvider>
  );
  const { rerender } = render(card);
  await openShiftActions(user);
  await user.click(screen.getByRole("button", { name: "Record a paid shift break" }));
  await user.type(screen.getByLabelText("Break start"), "01.08.2026 00:30");
  await user.type(screen.getByLabelText("Break end"), "01.08.2026 01:00");
  await user.click(screen.getByRole("button", { name: "Save paid break" }));
  await waitFor(() =>
    expect(vi.mocked(fetch)).toHaveBeenCalledWith(
      "/api/v1/me/break-intervals",
      expect.objectContaining({ method: "POST" }),
    ),
  );

  // The server's refreshed answer: the break is gone, so the fact is required.
  rerender(card);
  expect(screen.getByText("Not recorded yet")).toBeInTheDocument();
  // The corrective action is reachable again. Asked THROUGH the menu, because the
  // menu closed when the item was chosen and a bare query would report a false
  // absence.
  expect(await queryShiftAction(user, "Record a paid shift break")).toBeInTheDocument();
});

/**
 * A Shift Report preview is a snapshot of the projection over the RECORDED
 * facts. Writing a paid break moves those facts, so the snapshot describes a
 * shift that no longer exists — and invalidating the query is not enough,
 * because a failed or slow refetch would leave the stale projection on screen
 * presenting itself as current (review R4).
 */
it("drops the private report preview when a write moves the facts it projected", async () => {
  const user = userEvent.setup();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      String(url).includes("report-preview")
        ? new Response(JSON.stringify(previewPayload), { status: 200 })
        : new Response(JSON.stringify({}), { status: 200 }),
    ),
  );
  renderCard(deviatedShift);
  await user.click(screen.getByRole("button", { name: "Preview Shift Report" }));
  expect(await screen.findByText(/Shift Report preview/)).toBeInTheDocument();

  // Re-saving the deviation writes the actual work fact the projection was
  // computed over.
  await user.click(screen.getByRole("button", { name: "Save deviation" }));

  await waitFor(() => expect(screen.queryByText(/Shift Report preview/)).toBeNull());
});

/* The preview's own closing sentence claims it includes "plan, actual facts and
 * deviation context", and the server sends all of them in the same payload — so
 * the preview has to render them. Rendering only the plan left the employee
 * unable to read what they were about to hand over, and hid the deviation, which
 * is the reason a Shift Report is submitted at all (ACCESS-004 scope). */
it("shows the actual window, the deviation and the findings in the report preview", async () => {
  const user = userEvent.setup();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      String(url).includes("report-preview")
        ? new Response(
            JSON.stringify({
              ...previewPayload,
              actual_start_at: "2026-07-31T17:00:00Z",
              actual_end_at: "2026-07-31T22:00:00Z",
              reason: "Ended two hours early.",
              noticed_or_decided_at: "2026-08-01T09:30:00Z",
              decision_maker_type: "Group leader",
              findings: [
                {
                  rule_id: "SHIFT-003",
                  status: "WARNING",
                  message: "Paid shift breaks require explicit break facts.",
                },
              ],
            }),
            { status: 200 },
          )
        : new Response(JSON.stringify({}), { status: 200 }),
    ),
  );
  renderCard(deviatedShift);
  await user.click(screen.getByRole("button", { name: "Preview Shift Report" }));
  const preview = await screen.findByLabelText("Private Shift Report preview");
  // The actual WINDOW, not merely the "Actual:" label: a regression back to
  // "not confirmed" while the payload carries times must fail here.
  expect(preview).toHaveTextContent("Actual: 31 Jul 2026, 19:00 → 01 Aug 2026, 00:00");
  expect(preview).toHaveTextContent("Ended earlier than planned");
  expect(preview).toHaveTextContent("Ended two hours early.");
  // The two fields the recipient's own view renders.
  expect(preview).toHaveTextContent("Noticed or decided:");
  expect(preview).toHaveTextContent("Decided or reported by: Group leader");
  expect(preview).toHaveTextContent("Paid shift breaks require explicit break facts.");
});

it("renders both paid shift-break requirement states, privately and in the report preview", async () => {
  const user = userEvent.setup();
  renderCard();
  expect(screen.getByText("Not recorded yet")).toBeInTheDocument();
  cleanup();

  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(previewPayload), { status: 200 })),
  );
  renderCard(deviatedShift);
  expect(screen.getByText("Recorded")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Preview Shift Report" }));
  expect(await screen.findByText("Paid shift break: Not recorded yet")).toBeInTheDocument();
});

/**
 * Confirming a shift writes the actual work fact, so every view derived from
 * employee facts must refresh — not just this card's own shift list. The card
 * holds no month, and the fact it writes may cross a month or a year boundary,
 * so the assertion uses two months that the write never names: a month-scoped
 * invalidation would leave January stale.
 */
it("refreshes every fact-derived view after confirming a shift, across months", async () => {
  const user = userEvent.setup();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  for (const key of [
    ["month", "2026-12"],
    ["month", "2027-01"],
    ["findings", "2026-12"],
    ["day", "2026-12", "2026-12-31"],
    ["home-month-summary", "2026-12"],
    ["identity"],
  ]) {
    client.setQueryData(key, { seeded: true });
  }

  renderCard(plannedShift, client);
  await user.click(screen.getByRole("button", { name: "Confirm worked as planned" }));

  await waitFor(() => expect(client.getQueryState(["month", "2026-12"])?.isInvalidated).toBe(true));
  expect(client.getQueryState(["month", "2027-01"])?.isInvalidated).toBe(true);
  expect(client.getQueryState(["findings", "2026-12"])?.isInvalidated).toBe(true);
  expect(client.getQueryState(["day", "2026-12", "2026-12-31"])?.isInvalidated).toBe(true);
  expect(client.getQueryState(["home-month-summary", "2026-12"])?.isInvalidated).toBe(true);
  // Not a sledgehammer: an unrelated cache keeps its data.
  expect(client.getQueryState(["identity"])?.isInvalidated).toBe(false);
});

/**
 * The notice for a rejected field says "check the marked fields", so every field
 * the server can name must actually be able to mark itself. This was found live:
 * a 422 naming `decision_maker_type` produced the summary with no field message
 * beside it, making that sentence false.
 */
it("renders a server field rejection beside the field it names", async () => {
  const user = userEvent.setup();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/deviation"))
        return new Response(
          JSON.stringify({
            detail: {
              code: "VALIDATION_FAILED",
              message: "Request validation failed.",
              details: [
                {
                  loc: ["body", "decision_maker_type"],
                  msg: "String should have at most 120 chars",
                },
              ],
            },
          }),
          { status: 422 },
        );
      return new Response(JSON.stringify(plannedShift), { status: 200 });
    }),
  );
  renderCard(deviatedShift);
  await user.click(screen.getByRole("button", { name: "Save deviation" }));

  // The field's own message, in the product's words.
  expect(await screen.findByText("Say who decided or reported it.")).toBeInTheDocument();
  // The summary, and the field is not left unmarked. Both are alerts: the
  // summary is the one announcement for the attempt, and each field message is
  // its own so it is reachable without the summary being focused.
  const alerts = screen.getAllByRole("alert").map((node) => node.textContent);
  expect(alerts).toContain("Some details could not be accepted — check the marked fields.");
  expect(alerts).toContain("Say who decided or reported it.");
  // R5: the CONTROL points at its message and declares itself invalid, so a
  // screen-reader user can get from the field to its rejection.
  const maker = screen.getByLabelText(/Decision maker/);
  expect(maker).toHaveAttribute("aria-invalid", "true");
  expect(maker).toHaveAttribute("aria-describedby", "deviation-maker-issue");
  // A field the server did NOT name is left alone.
  expect(screen.getByLabelText(/Actual start/)).not.toHaveAttribute("aria-invalid");
  // Neither the raw loc path element nor Pydantic's sentence reaches the DOM.
  const body = document.body.textContent ?? "";
  expect(body).not.toContain("decision_maker_type");
  expect(body).not.toContain("String should have at most");
  expect(body).not.toContain("Request validation failed.");
});

/**
 * The message was rendered beside the select, but the CONTROL pointed at
 * neither it nor itself — so a screen-reader user could not get from the field
 * to its rejection (review R20, the same wiring R5/R9/R6 gave the other forms).
 */
it("connects a deviation_type rejection to the select it names", async () => {
  const user = userEvent.setup();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes("/deviation"))
        return new Response(
          JSON.stringify({
            detail: {
              code: "VALIDATION_FAILED",
              message: "Request validation failed.",
              details: [{ loc: ["body", "deviation_type"], msg: "Input should be a valid enum" }],
            },
          }),
          { status: 422 },
        );
      return new Response(JSON.stringify(deviatedShift), { status: 200 });
    }),
  );
  renderCard(deviatedShift);
  await user.click(screen.getByRole("button", { name: "Save deviation" }));

  const select = screen.getByLabelText("Deviation");
  const message = await screen.findByText("Choose the kind of difference.");
  expect(select).toHaveAttribute("aria-invalid", "true");
  expect(select).toHaveAttribute("aria-describedby", message.id);
});

/**
 * The card is NOT the only writer of the facts its private preview projects: the
 * day timeline can edit or delete the linked paid break, and the shift arrives
 * here as a refetched prop. No callback on this card can see that writer, so the
 * facts are fingerprinted and the snapshot dropped whenever they change
 * (review R6). This is the window R4's three direct clears could not cover.
 */
it("drops the report preview when a fact it projected changes from elsewhere", async () => {
  const user = userEvent.setup();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      String(url).includes("report-preview")
        ? new Response(JSON.stringify(previewPayload), { status: 200 })
        : new Response(JSON.stringify({}), { status: 200 }),
    ),
  );
  const { rerender } = renderCard(deviatedShift);
  await user.click(screen.getByRole("button", { name: "Preview Shift Report" }));
  expect(await screen.findByText(/Shift Report preview/)).toBeInTheDocument();

  // The day timeline deleted the linked paid break; the parent refetches.
  rerender(
    <QueryClientProvider client={new QueryClient()}>
      <ShiftPlanActualCard
        shift={{ ...deviatedShift, paid_break_status: "EXPLICIT_FACT_REQUIRED" as const }}
      />
    </QueryClientProvider>,
  );

  await waitFor(() => expect(screen.queryByText(/Shift Report preview/)).toBeNull());
});

/**
 * The projection is built from the server's own `ShiftReportInputs`. A shift-plan
 * update that moves the APPROVED revision changes what the report says while
 * status, actual span, deviation version/type and the break status all stay put —
 * so a fingerprint carrying only those left the preview claiming the former plan
 * (review R7). The same holds for the deviation's decision context.
 */
it("drops the report preview when the approved revision changes", async () => {
  const user = userEvent.setup();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      String(url).includes("report-preview")
        ? new Response(JSON.stringify(previewPayload), { status: 200 })
        : new Response(JSON.stringify({}), { status: 200 }),
    ),
  );
  const { rerender } = renderCard(deviatedShift);
  await user.click(screen.getByRole("button", { name: "Preview Shift Report" }));
  expect(await screen.findByText(/Shift Report preview/)).toBeInTheDocument();

  // A controlled shift-plan update: new revision, new planned span, nothing else.
  rerender(
    <QueryClientProvider client={new QueryClient()}>
      <ShiftPlanActualCard
        shift={{
          ...deviatedShift,
          approved_revision: {
            ...deviatedShift.approved_revision,
            id: "revision-2",
            revision_no: 2,
            planned_start_at: "2026-07-31T18:00:00Z",
            planned_end_at: "2026-08-01T06:00:00Z",
          },
        }}
      />
    </QueryClientProvider>,
  );

  await waitFor(() => expect(screen.queryByText(/Shift Report preview/)).toBeNull());
});

it("drops the report preview when the deviation's decision context changes", async () => {
  const user = userEvent.setup();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      String(url).includes("report-preview")
        ? new Response(JSON.stringify(previewPayload), { status: 200 })
        : new Response(JSON.stringify({}), { status: 200 }),
    ),
  );
  const { rerender } = renderCard(deviatedShift);
  await user.click(screen.getByRole("button", { name: "Preview Shift Report" }));
  expect(await screen.findByText(/Shift Report preview/)).toBeInTheDocument();

  // Who decided and why are projection inputs; the version is unchanged because
  // this arrives from another surface, not from this card's own write.
  rerender(
    <QueryClientProvider client={new QueryClient()}>
      <ShiftPlanActualCard
        shift={{
          ...deviatedShift,
          deviation: {
            ...deviatedShift.deviation!,
            decision_maker_type: "SUPERVISOR",
            reason: "Handover ran long",
            noticed_or_decided_at: "2026-07-31T16:00:00Z",
          },
        }}
      />
    </QueryClientProvider>,
  );

  await waitFor(() => expect(screen.queryByText(/Shift Report preview/)).toBeNull());
});

/**
 * `ShiftReportInputs.findings` is `list[ShiftFindingOut]` and the report copies
 * it unchanged, `message` included. Fingerprinting only `rule_id:status` left the
 * preview showing superseded wording when the server re-derived the same finding
 * in new words while every other ShiftOut field stayed equal (review R8).
 */
it("drops the report preview when a finding's wording changes", async () => {
  const user = userEvent.setup();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      String(url).includes("report-preview")
        ? new Response(JSON.stringify(previewPayload), { status: 200 })
        : new Response(JSON.stringify({}), { status: 200 }),
    ),
  );
  const { rerender } = renderCard(deviatedShift);
  await user.click(screen.getByRole("button", { name: "Preview Shift Report" }));
  expect(await screen.findByText(/Shift Report preview/)).toBeInTheDocument();

  // Same rule, same status, reworded — nothing else moves.
  rerender(
    <QueryClientProvider client={new QueryClient()}>
      <ShiftPlanActualCard
        shift={{
          ...deviatedShift,
          findings: deviatedShift.findings.map((finding) => ({
            ...finding,
            message: "Paid break facts are now required for this shift.",
          })),
        }}
      />
    </QueryClientProvider>,
  );

  await waitFor(() => expect(screen.queryByText(/Shift Report preview/)).toBeNull());
});

/**
 * §6.2 provenance. The `<dt>` was hard-coded "Approved plan", which is a claim
 * nobody made for a plan the employee entered themselves. The label is now the
 * fail-closed three-way `planSourceLabel`, keyed on the POSITIVE authoritative
 * predicate — so an unrecognised source (the fixture's `TEST_SEED`, a future
 * adapter's own code, a typo) takes the neutral wording rather than falling into
 * an `else` that reads as institutional approval.
 */
it("labels the plan by its provenance and never claims approval it does not have", () => {
  renderCard({ ...plannedShift, plan_source: "EMPLOYEE_RECORDED" });
  expect(screen.getByText("Planned shift")).toBeInTheDocument();
  expect(screen.queryByText("Approved plan")).toBeNull();
  expect(screen.getByText("Entered by employee")).toBeInTheDocument();
  expect(screen.getByText("Employee-entered plan.")).toBeInTheDocument();
  cleanup();

  renderCard({ ...plannedShift, plan_source: "INSTITUTIONAL_APPROVAL" });
  expect(screen.getByText("Approved plan")).toBeInTheDocument();
  expect(screen.queryByText("Planned shift")).toBeNull();
  // No employee badge and no employee copy line: this plan is not their record.
  expect(screen.queryByText("Entered by employee")).toBeNull();
  cleanup();

  for (const source of ["TEST_SEED", "SOME_FUTURE_ADAPTER", ""]) {
    renderCard({ ...plannedShift, plan_source: source });
    expect(screen.getByText("Planned shift")).toBeInTheDocument();
    expect(screen.queryByText("Approved plan")).toBeNull();
    cleanup();
  }
});

/**
 * §6.3 / D22: Act B posts through the EXISTING deviation endpoint with
 * `deviation_type: "CANCELLED"`. The field label reports what the employee was
 * TOLD — `decision_maker_type` is free text stored unvalidated (SHIFT-013), so
 * "Cancelled by" would assert the Company's own decision in the employee's
 * record. This pins the label as well as the payload.
 */
it("records a cancellation with the reported instant and decision role", async () => {
  const user = userEvent.setup();
  renderCard();
  await openShiftActions(user);
  await user.click(screen.getByRole("button", { name: "Record a shift cancellation" }));

  expect(screen.getByLabelText(decisionMakerRoleFieldLabel)).toBeInTheDocument();
  expect(screen.queryByText(/Cancelled by/)).toBeNull();
  // The plan is preserved, and the form says so before the employee commits.
  expect(
    screen.getByText(
      "The planned shift stays as the basis for allowances — record this rather than removing the plan.",
    ),
  ).toBeInTheDocument();

  await user.selectOptions(screen.getByLabelText(decisionMakerRoleFieldLabel), "Department Head");
  await user.type(screen.getByLabelText("When were you told?"), "30.07.2026 08:15");
  await user.click(screen.getByRole("button", { name: "Save cancellation" }));

  const call = await waitFor(() => {
    const found = vi
      .mocked(fetch)
      .mock.calls.find(([url]) => url === "/api/v1/me/shifts/shift-1/deviation");
    if (!found) throw new Error("no cancellation was posted");
    return found;
  });
  expect(call[1]).toMatchObject({ method: "POST" });
  expect(JSON.parse(String(call[1]?.body))).toEqual({
    deviation_type: "CANCELLED",
    noticed_or_decided_local: "2026-07-30T08:15",
    decision_maker_type: "Department Head",
  });
});

/**
 * The escape hatch is a UI-only sentinel (`lib/labels.ts`): choosing it reveals a
 * free-text input and the TEXT is what is sent. Storing "Other / not stated" in
 * `decision_maker_type` would put a dropdown label into a fact column, and an
 * empty text must send NOTHING rather than the sentinel.
 */
it("sends the free text from the escape hatch, never the sentinel itself", async () => {
  const user = userEvent.setup();
  renderCard();
  await openShiftActions(user);
  await user.click(screen.getByRole("button", { name: "Record a shift cancellation" }));

  const roleSelect = screen.getByLabelText(decisionMakerRoleFieldLabel);
  expect(roleSelect).toHaveValue(decisionMakerRoleOther);
  // The reveal is announced in words, not only by the control appearing.
  expect(screen.getByText("A decision-maker field is below.")).toBeInTheDocument();

  await user.type(screen.getByLabelText("Who decided"), "Plant manager");
  await user.type(screen.getByLabelText("When were you told?"), "30.07.2026 08:15");
  await user.click(screen.getByRole("button", { name: "Save cancellation" }));

  const call = await waitFor(() => {
    const found = vi
      .mocked(fetch)
      .mock.calls.find(([url]) => url === "/api/v1/me/shifts/shift-1/deviation");
    if (!found) throw new Error("no cancellation was posted");
    return found;
  });
  const body = JSON.parse(String(call[1]?.body));
  expect(body.decision_maker_type).toBe("Plant manager");
  expect(String(call[1]?.body)).not.toContain(decisionMakerRoleOther);
});

/**
 * INV-15 / §4.6: the reported instant is OPTIONAL. `shift/schemas.py:165` defaults
 * it to None and an absent instant leaves SHIFT-009 BLOCKED_POLICY — the honest
 * answer. Gating the submit on it would be a process tightening: the employee would
 * be unable to record a cancellation they were told about but whose timing they do
 * not know. The field must therefore be submittable while empty, and an empty field
 * must send NOTHING rather than an empty string the server would reject.
 */
it("records a cancellation with no reported instant, sending no instant field", async () => {
  const user = userEvent.setup();
  renderCard();
  await openShiftActions(user);
  await user.click(screen.getByRole("button", { name: "Record a shift cancellation" }));

  const submit = screen.getByRole("button", { name: "Save cancellation" });
  expect(submit).toBeEnabled();

  await user.click(submit);
  await new Promise((r) => setTimeout(r, 50));
  const call = await waitFor(() => {
    const found = vi
      .mocked(fetch)
      .mock.calls.find(([url]) => url === "/api/v1/me/shifts/shift-1/deviation");
    if (!found) throw new Error("no cancellation was posted");
    return found;
  });
  const body = JSON.parse(String(call[1]?.body));
  expect(body.deviation_type).toBe("CANCELLED");
  expect(body).not.toHaveProperty("noticed_or_decided_local");
  // The consequence of leaving it out is stated in words, and the format gate is
  // NOT shown for an empty field.
  expect(screen.getByText(/can be saved either way/i)).toBeInTheDocument();
  expect(screen.queryByText(/Use the format DD.MM.YYYY HH:MM/)).toBeNull();
});

it("refuses only an instant the employee typed but that does not parse", async () => {
  const user = userEvent.setup();
  renderCard();
  await openShiftActions(user);
  await user.click(screen.getByRole("button", { name: "Record a shift cancellation" }));

  await user.type(screen.getByLabelText("When were you told?"), "not a date");
  expect(screen.getByRole("button", { name: "Save cancellation" })).toBeDisabled();
  expect(screen.getByText(/Use the format DD.MM.YYYY HH:MM/)).toBeInTheDocument();
});

/**
 * §6.4 / D12: Act A is a DIFFERENT act from a cancellation — it sets `WITHDRAWN`,
 * records no deviation and carries no entitlement — so it is separately worded,
 * visually secondary, and steered away from in words. It is offered only while
 * the plan is mutable (D3); once anything depends on the plan the card states the
 * reason instead of offering a control that can only fail.
 */
it("offers the mistaken-entry action only while the plan is mutable", async () => {
  const user = userEvent.setup();
  const label = "Remove plan entered by mistake";
  renderCard(plannedShift);
  // Behind the actions menu, so the menu has to be opened to see it at all —
  // which is the point of the hierarchy: the destructive control is reachable but
  // not on display beside the normal workflow.
  await openShiftActions(user);
  expect(screen.getByRole("button", { name: label })).toBeInTheDocument();
  // The steering sentence travels WITH the item, inside the menu, so it is read at
  // the moment of choice.
  expect(screen.getByText(/A Company cancellation is a cancellation/)).toBeInTheDocument();
  cleanup();

  // A linked paid shift break freezes the plan (D3, no containment exception).
  renderCard(recordedBreakShift);
  expect(await queryShiftAction(user, label)).toBeNull();
  cleanup();

  // Recorded actual time freezes it too, and the reason is stated in words.
  renderCard({
    ...plannedShift,
    actual_start_at: "2026-07-31T17:05:00Z",
    actual_end_at: "2026-08-01T05:00:00Z",
  });
  expect(await queryShiftAction(user, label)).toBeNull();
  expect(
    screen.getByText("This shift already has recorded actual time. Record a deviation instead."),
  ).toBeInTheDocument();
  cleanup();

  // A recorded deviation freezes it as well.
  renderCard(deviatedShift);
  expect(await queryShiftAction(user, label)).toBeNull();
});

it("removes a mistaken plan only through the two-step confirm", async () => {
  const user = userEvent.setup();
  renderCard();
  await openShiftActions(user);
  await user.click(screen.getByRole("button", { name: "Remove plan entered by mistake" }));
  // The first click ARMS; nothing destructive has happened yet.
  expect(vi.mocked(fetch)).not.toHaveBeenCalledWith(
    "/api/v1/me/shifts/shift-1/withdraw",
    expect.anything(),
  );
  await user.click(screen.getByRole("button", { name: "Remove the planned shift" }));
  await waitFor(() =>
    expect(vi.mocked(fetch)).toHaveBeenCalledWith(
      "/api/v1/me/shifts/shift-1/withdraw",
      expect.objectContaining({ method: "POST" }),
    ),
  );
});

/**
 * §4.7: a WITHDRAWN assignment is retained lineage, so it stays visible to the
 * employee as their own history — but it is never an active plan. The card used
 * to gate its actions on `!== "SUPERSEDED"`, which left a withdrawn plan showing
 * the confirm, deviation and paid-break controls.
 */
it("renders a withdrawn plan read-only, with no action buttons", async () => {
  const user = userEvent.setup();
  renderCard({ ...plannedShift, status: "WITHDRAWN" });
  expect(screen.getByText("Withdrawn")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Confirm worked as planned" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Record a deviation" })).toBeNull();
  // There is no actions menu at all for a non-active plan, so opening one is not
  // possible — and that is the stronger assertion: the controls are not merely
  // hidden behind a menu, the menu itself is absent.
  expect(screen.queryByRole("button", { name: "More shift actions" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Record a paid shift break" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Record a shift cancellation" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Correct the planned shift" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Remove plan entered by mistake" })).toBeNull();
  // Still the employee's own record, so the plan itself stays readable.
  expect(screen.getByText("Planned shift")).toBeInTheDocument();
  // The absence is genuine, not an artefact of a closed menu: opening it is
  // impossible because its toggle does not exist.
  expect(user).toBeDefined();
});

/**
 * D13: `ABORTED_BY_COMPANY` is a distinct value, not a synonym for CANCELLED —
 * a CANCELLED deviation DELETES the linked actual intervals, which would erase
 * the partial work that actually happened and destroy the very fact the
 * entitlement is calculated from.
 */
it("offers the Company-abortion deviation type", async () => {
  const user = userEvent.setup();
  renderCard();
  await user.click(screen.getByRole("button", { name: "Record a deviation" }));
  expect(screen.getByRole("option", { name: "ABORTED BY COMPANY" })).toBeInTheDocument();
});

/**
 * `plan_source` is now a projection input: the report states who authored the
 * plan, so a refetched shift that changed provenance must drop a snapshot still
 * claiming the old one — the same class as the approved-revision and finding
 * fingerprints above.
 */
it("drops the report preview when the plan's provenance changes", async () => {
  const user = userEvent.setup();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      String(url).includes("report-preview")
        ? new Response(JSON.stringify(previewPayload), { status: 200 })
        : new Response(JSON.stringify({}), { status: 200 }),
    ),
  );
  const { rerender } = renderCard(deviatedShift);
  await user.click(screen.getByRole("button", { name: "Preview Shift Report" }));
  expect(await screen.findByText(/Shift Report preview/)).toBeInTheDocument();

  rerender(
    <QueryClientProvider client={new QueryClient()}>
      <ShiftPlanActualCard shift={{ ...deviatedShift, plan_source: "INSTITUTIONAL_APPROVAL" }} />
    </QueryClientProvider>,
  );

  await waitFor(() => expect(screen.queryByText(/Shift Report preview/)).toBeNull());
});

/**
 * D19 / §8.8: a cancelled or aborted shift's FACTUAL report is submittable, and
 * what the recipient gets instead of an entitlement is one report-level review
 * status. The employee must see the same line before sharing it — so the private
 * preview states it, in the shared `compensationReviewLabel` words.
 */
it("states the plan source and the compensation review line in the private preview", async () => {
  const user = userEvent.setup();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      String(url).includes("report-preview")
        ? new Response(
            JSON.stringify({
              ...previewPayload,
              deviation_type: "CANCELLED",
              compensation_review_status: "MANUAL_REVIEW_REQUIRED",
            }),
            { status: 200 },
          )
        : new Response(JSON.stringify({}), { status: 200 }),
    ),
  );
  renderCard(deviatedShift);
  await user.click(screen.getByRole("button", { name: "Preview Shift Report" }));

  expect(
    await screen.findByText("Compensation review: Compensation needs manual review"),
  ).toBeInTheDocument();
  // Scoped to the preview: the snapshot is self-contained and must state the
  // provenance itself, so the same words also appear on the card's own `<dt>`.
  const panel = screen.getByLabelText("Private Shift Report preview");
  expect(within(panel).getByText("Planned shift")).toBeInTheDocument();
});

/**
 * The last missing affordance of the plan-entry slice: the card offered create,
 * confirm, deviation, cancellation and withdrawal but NO way to correct a planned
 * WINDOW that was recorded wrongly — and docs/INVARIANTS.md requires the employee to be
 * able to correct their own unsubmitted records (§12.2 row 2: "edit it → the plan
 * is mutable; `revision_no` advanced").
 *
 * The act is a CORRECTION, not a cancellation (Act B, which leaves the plan
 * byte-identical) and not a withdrawal (Act A, for a shift that was never really
 * planned), so the form names the other two acts in words.
 */
it("corrects the planned window of a mutable plan on the row's own revision token", async () => {
  const user = userEvent.setup();
  renderCard();
  await openShiftActions(user);
  await user.click(screen.getByRole("button", { name: "Correct the planned shift" }));

  // A real fieldset/legend group with a real label on every control: the label
  // text IS the accessible name, with no `aria-label` standing in for one.
  const form = screen.getByRole("group", { name: "Correct the planned shift" });
  // Prefilled from the row, so an untouched field writes back what it already had.
  // Shown as a bare TIME because each value already sits on the day its own field
  // anchors to (the window's start day, and the window's END day for the end) —
  // the write-back below proves the underlying instant is the row's, unchanged.
  expect(within(form).getByLabelText("Planned start")).toHaveValue("19:00");
  expect(within(form).getByLabelText("Planned end")).toHaveValue("07:00");
  expect(within(form).getByLabelText("Shift kind")).toHaveValue("NIGHT");
  // The three acts must stay tellable apart: this one moves the window and
  // nothing else, and it names the other two.
  expect(
    within(form).getByText(
      "Changes the planned window only. Record a cancellation if the Company cancelled it; remove the plan if it was never really planned.",
    ),
  ).toBeInTheDocument();

  await user.click(within(form).getByRole("button", { name: "Save correction" }));

  const call = await waitFor(() => {
    const found = vi.mocked(fetch).mock.calls.find(([url]) => url === "/api/v1/me/shifts/shift-1");
    if (!found) throw new Error("no correction was sent");
    return found;
  });
  expect(call[1]).toMatchObject({ method: "PATCH" });
  const body = JSON.parse(String(call[1]?.body));
  expect(body).toEqual({
    // The token is the CURRENT approved revision id read from the row on screen —
    // the same one confirm/withdraw send. A guessed or stale token would either
    // corrupt a correctable record or be refused as CONFLICT_SHIFT_PLAN_CHANGED.
    approved_revision_id: "revision-1",
    planned_start_local: "2026-07-31T19:00",
    planned_end_local: "2026-08-01T07:00",
    // Unambiguous wall times resolve identically either way, so re-stating the
    // stored occurrence is harmless; the fall-back hour is covered below.
    start_fold: 0,
    end_fold: 0,
    shift_kind: "NIGHT",
  });
  // A correction is a version write, not a create: there is no command key.
  expect(body).not.toHaveProperty("client_request_id");
  expect(
    await screen.findByText("Planned shift corrected. The plan now shows the window you recorded."),
  ).toBeInTheDocument();
});

/**
 * D3, the same gate as the mistaken-entry action: once a linked actual interval, a
 * current deviation or a paid shift break depends on the plan, the server refuses
 * the write — so the card states the reason in words and offers no control that
 * could only fail (`overtime.tsx:92-95`).
 */
it("offers the correction only while the plan is mutable", async () => {
  const user = userEvent.setup();
  const label = "Correct the planned shift";
  renderCard(plannedShift);
  expect(await queryShiftAction(user, label)).toBeInTheDocument();
  cleanup();

  // A linked paid shift break freezes the plan (D3, no containment exception).
  renderCard(recordedBreakShift);
  expect(await queryShiftAction(user, label)).toBeNull();
  cleanup();

  // Recorded actual time freezes it, and the reason is stated instead.
  renderCard({
    ...plannedShift,
    actual_start_at: "2026-07-31T17:05:00Z",
    actual_end_at: "2026-08-01T05:00:00Z",
  });
  expect(await queryShiftAction(user, label)).toBeNull();
  expect(
    screen.getByText("This shift already has recorded actual time. Record a deviation instead."),
  ).toBeInTheDocument();
  cleanup();

  renderCard(deviatedShift);
  expect(await queryShiftAction(user, label)).toBeNull();
});

it("refreshes the fact-derived views after a correction", async () => {
  const user = userEvent.setup();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(["month", "2026-12"], { seeded: true });
  const onFactsChanged = vi.fn();
  render(
    <QueryClientProvider client={client}>
      <ShiftPlanActualCard shift={plannedShift} onFactsChanged={onFactsChanged} />
    </QueryClientProvider>,
  );

  await openShiftActions(user);
  await user.click(screen.getByRole("button", { name: "Correct the planned shift" }));
  await user.click(screen.getByRole("button", { name: "Save correction" }));

  await waitFor(() => expect(client.getQueryState(["month", "2026-12"])?.isInvalidated).toBe(true));
  expect(onFactsChanged).toHaveBeenCalled();
});

/**
 * The plan is frozen (or the token is stale) — the server's 409 names the cause and
 * the next step, and it is announced through the card's ONE notice channel rather
 * than a second, silently-divergent message surface (acceptance E4).
 */
it("states the server's refusal and keeps the correction open on the typed values", async () => {
  const user = userEvent.setup();
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) =>
      String(input) === "/api/v1/me/shifts/shift-1"
        ? new Response(
            JSON.stringify({
              detail: {
                code: "CONFLICT_SHIFT_PLAN_CHANGED",
                message: "The approved shift plan changed; refresh before correcting it.",
              },
            }),
            { status: 409 },
          )
        : new Response(JSON.stringify(plannedShift), { status: 200 }),
    ),
  );
  renderCard();
  await openShiftActions(user);
  await user.click(screen.getByRole("button", { name: "Correct the planned shift" }));
  await user.clear(screen.getByLabelText("Planned end"));
  // A day OTHER than the field's own: the stated date must stay visible, so the
  // short form can never quietly drop a date the employee typed.
  await user.type(screen.getByLabelText("Planned end"), "02.08.2026 06:00");
  await user.click(screen.getByRole("button", { name: "Save correction" }));

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("The approved shift plan changed; refresh before correcting it.");
  // Nothing was invented: the form stays open on the values the employee typed.
  expect(screen.getByRole("group", { name: "Correct the planned shift" })).toBeInTheDocument();
  expect(screen.getByLabelText("Planned end")).toHaveValue("02.08.2026 06:00");
});

/**
 * INV-15: nothing NEW is made mandatory — both ends are prefilled from the row, so
 * an untouched form always satisfies the requirement. It is only stated once a
 * field has been emptied, and then the disabled primary points at its own sentence
 * (WCAG 2.2 SC 3.3.2) instead of sitting there enabled and doing nothing.
 */
it("states what a disabled correction is waiting for", async () => {
  const user = userEvent.setup();
  renderCard();
  await openShiftActions(user);
  await user.click(screen.getByRole("button", { name: "Correct the planned shift" }));
  const save = screen.getByRole("button", { name: "Save correction" });
  expect(save).toBeEnabled();

  await user.clear(screen.getByLabelText("Planned start"));
  expect(save).toBeDisabled();
  const requirement = screen.getByText(/Both ends are needed to move the window/);
  expect(requirement).toHaveAttribute("role", "status");
  expect(save).toHaveAttribute("aria-describedby", requirement.id);
});

/**
 * An engine code is never primary copy, and a `<select>` cannot show a kind it does
 * not know — offering the label map's options over an unknown kind would silently
 * replace a fact the employee never chose. The field is withheld and the payload
 * omits the key, which the server reads as "unchanged".
 */
it("withholds the kind field for a kind the label map cannot present", async () => {
  const user = userEvent.setup();
  renderCard({ ...plannedShift, shift_kind: "SOME_FUTURE_KIND" });
  await openShiftActions(user);
  await user.click(screen.getByRole("button", { name: "Correct the planned shift" }));

  const form = screen.getByRole("group", { name: "Correct the planned shift" });
  expect(within(form).queryByLabelText("Shift kind")).toBeNull();
  await user.click(within(form).getByRole("button", { name: "Save correction" }));

  const call = await waitFor(() => {
    const found = vi.mocked(fetch).mock.calls.find(([url]) => url === "/api/v1/me/shifts/shift-1");
    if (!found) throw new Error("no correction was sent");
    return found;
  });
  expect(JSON.parse(String(call[1]?.body))).not.toHaveProperty("shift_kind");
});

/**
 * docs/ARCHITECTURE.md / `shift/service.py`: an ambiguous wall time is refused unless the
 * caller says WHICH occurrence — the server never guesses, so an untouched field
 * must carry the occurrence the ROW already has, or a plan sitting in the fall-back
 * hour could never be corrected at all. A NEW ambiguous value stays the employee's
 * to disambiguate: no fold travels and the server refuses it in words rather than
 * letting the client pick an occurrence on their behalf.
 *
 * 2026-10-25 is the fall-back day: 02:30 happens twice in Europe/Berlin.
 */
it("re-states the stored DST occurrence for an untouched window and never picks one for a new value", async () => {
  const user = userEvent.setup();
  const fallBack = (start: string) => ({
    ...plannedShift,
    approved_revision: {
      ...plannedShift.approved_revision,
      planned_start_at: start,
      // 04:00Z is 05:00 CET: outside the ambiguous hour, so the fold is ignored
      // either way and the end says nothing about the case under test.
      planned_end_at: "2026-10-25T04:00:00Z",
    },
  });
  const patchBody = async (): Promise<Record<string, unknown>> => {
    const call = await waitFor(() => {
      const found = vi
        .mocked(fetch)
        .mock.calls.find(([url]) => url === "/api/v1/me/shifts/shift-1");
      if (!found) throw new Error("no correction was sent");
      return found;
    });
    return JSON.parse(String(call[1]?.body));
  };

  // First occurrence: 00:30Z is 02:30 CEST. The fold is the one the row carries,
  // so the untouched field re-states the instant it already had (CEST ⇒ 0, CET ⇒
  // 1 — outside the ambiguous hour the server ignores the fold entirely).
  renderCard(fallBack("2026-10-25T00:30:00Z"));
  await openShiftActions(user);
  await user.click(screen.getByRole("button", { name: "Correct the planned shift" }));
  await user.click(screen.getByRole("button", { name: "Save correction" }));
  expect(await patchBody()).toMatchObject({ start_fold: 0, end_fold: 1 });
  cleanup();

  // Second occurrence: 01:30Z is 02:30 CET — the same wall time, one hour later.
  vi.mocked(fetch).mockClear();
  renderCard(fallBack("2026-10-25T01:30:00Z"));
  await openShiftActions(user);
  await user.click(screen.getByRole("button", { name: "Correct the planned shift" }));
  await user.click(screen.getByRole("button", { name: "Save correction" }));
  expect(await patchBody()).toMatchObject({ start_fold: 1, end_fold: 1 });
  cleanup();

  // A value the employee typed is not the client's to disambiguate.
  vi.mocked(fetch).mockClear();
  renderCard(fallBack("2026-10-25T01:30:00Z"));
  await openShiftActions(user);
  await user.click(screen.getByRole("button", { name: "Correct the planned shift" }));
  await user.clear(screen.getByLabelText("Planned end"));
  await user.type(screen.getByLabelText("Planned end"), "25.10.2026 02:45");
  await user.click(screen.getByRole("button", { name: "Save correction" }));
  const body = await patchBody();
  expect(body).not.toHaveProperty("end_fold");
  expect(body).toMatchObject({ start_fold: 1 });
});
