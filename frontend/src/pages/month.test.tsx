import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { shouldRetryQuery } from "../lib/api";
import { berlinToday } from "../lib/format";
import { DayTimeline, MonthWorkspace, defaultDayFor } from "./month";

vi.mock("@tanstack/react-router", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@tanstack/react-router")>()),
  Link: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

/** Interval editors and the add form are the only fieldsets; details
 *  elements also carry an implicit "group" role, so fieldset is the precise
 *  "an editor is open" signal. */
function fieldsets(): NodeListOf<HTMLFieldSetElement> {
  return document.querySelectorAll("fieldset");
}

const fourIntervals = [
  ["08:00", "10:00", "ON_SITE"],
  ["10:15", "12:00", "ON_SITE"],
  ["12:30", "14:00", "REMOTE"],
  ["14:15", "17:00", "REMOTE"],
].map(([start, end, work_type], index) => {
  const [sh, sm] = start.split(":").map(Number);
  const [eh, em] = end.split(":").map(Number);
  return {
    id: `interval-${index}`,
    start_at: `2026-07-06T${start}:00+02:00`,
    end_at: `2026-07-06T${end}:00+02:00`,
    start_local: `2026-07-06T${start}:00`,
    end_local: `2026-07-06T${end}:00`,
    work_type,
    elapsed_seconds: (eh * 60 + em - (sh * 60 + sm)) * 60,
    comment: null,
    version: 1,
  };
});

let shiftsResponse: unknown;
/** `GET /api/v1/me` — the capability set the add-plan control is gated on
 *  (§6.1). Empty by default, which is what the rest of the suite expects: the
 *  control is OPTIONAL, so its absence must never change what the month shows. */
let identityCapabilities: string[] = [];
let dayStatusResponse: unknown;
let julyDailyResult: Record<string, unknown>;
let daySixIntervals: typeof fourIntervals;
let daySixBreaks: unknown[];
let octoberIntervals: Array<(typeof fourIntervals)[number]>;
let octoberTwentyFourthIntervals: Array<(typeof fourIntervals)[number]>;
let findingsResponse: Array<{
  rule_id: string;
  severity: string;
  date: string | null;
  message: string;
}>;
let fetchMock: ReturnType<typeof vi.fn>;
/** When set, the month request answers this failure instead of a payload — used
 *  to render the coverage-boundary states (POLICY_BLOCKED / CALENDAR_BLOCKED). */
let monthError: { status: number; code: string } | null;
/** Days a test adds to the month payload AFTER the workspace has rendered — the
 *  way a fact write changes which days are still eligible for the normal week. */
let extraMonthDays: Array<{ date: string; interval_count: number }> = [];

/** What the month page reads for the employee's Chronobeam start. NOT completed by
 *  default, so the ordinary balance path is what the rest of the suite exercises. */
let onboardingForTest: Record<string, unknown> = {
  state: "READY",
  month: "2026-07",
  default_start_month: "2026-07",
  opening: null,
  blocked_reason: null,
  blocked_month: null,
  has_facts_in_month: false,
  correctable: false,
  in_use_reason: null,
  schedule: null,
};

/** A COMPLETED start state whose cutover month is `cutoverMonth`. */
function startedIn(cutoverMonth: string) {
  return {
    ...onboardingForTest,
    state: "COMPLETED",
    opening: {
      cutover_month: cutoverMonth,
      minutes: 735,
      seed_minutes: 735,
      occurred_on: "2026-09-30",
      previous_period: "2026-09",
      confirmed_by: "employee",
      revision: 1,
      corrected_at: null,
    },
  };
}

function renderWorkspace(client: QueryClient = newQueryClient()) {
  return render(
    <QueryClientProvider client={client}>
      <MonthWorkspace month="2026-07" />
    </QueryClientProvider>,
  );
}

/** The app's own client shape (`main.tsx`): one deliberate retry policy, so a
 *  deterministic refusal surfaces immediately instead of being retried three
 *  times with backoff. A bare `new QueryClient()` would take TanStack's default
 *  and leave a 409 pending for seconds, which is not what the app does. */
function newQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: shouldRetryQuery } } });
}

/** Month payload requests are the observable effect of invalidating
 *  ["month", month]. */
function monthFetches(): number {
  return fetchMock.mock.calls.filter(([input]) => String(input).includes("/months/2026-07")).length;
}

const vacationFull = {
  id: "status-1",
  day: "2026-07-06",
  category: "VACATION",
  portion: "FULL",
  day_part: "ALL",
  version: 1,
  supersedes_id: null,
  created_at: "2026-07-01T08:00:00+02:00",
};

/** The EXCEPTION case: a half day, which is the one extent a calendar bar still
 *  states, because "am"/"pm" is the only thing that separates it from the default
 *  full day on a cell that has ~77px of text space. */
const vacationHalf = {
  ...vacationFull,
  id: "status-half",
  portion: "HALF",
  day_part: "MORNING",
};

/** The extended day-status response: the year's recorded compensation facts,
 *  read-only and explicitly not enforced. */
const compensationMetrics = {
  year: 2026,
  flex_compensation_full_days: 6,
  flex_compensation_half_days: 1,
  flex_compensation_zero_requirement_days: 2,
  flex_compensation_longest_run_days: 6,
  shift_compensation_days: 3,
  limits: {
    max_days_per_year: 20,
    max_consecutive_days: 5,
    source: "POL-05",
    enforced: false,
  },
  counting_note:
    "This is guidance, not a limit we apply. How a half day counts towards the 20-day limit is an open configuration question.",
};

describe("the day a month opens on", () => {
  it("is the 1st for a month that is not the current one", () => {
    // Dates no clock can be inside, so these two assertions never depend on when
    // the suite runs.
    expect(defaultDayFor("1999-01")).toBe("1999-01-01");
    expect(defaultDayFor("2099-12")).toBe("2099-12-01");
  });

  it("is TODAY for the current month, not the 1st", () => {
    const today = berlinToday();
    expect(defaultDayFor(today.slice(0, 7))).toBe(today);
    // On the 1st of a month the two branches agree, so this test cannot tell them
    // apart that day; every other day it discriminates.
  });
});

describe("employee month workspace", () => {
  it("opens the day a finding link names, and refuses a day outside the month", async () => {
    // UI-046: the review list links to the day a finding belongs to, so the
    // workspace must open on that day instead of always on the 1st.
    const view = render(
      <QueryClientProvider client={new QueryClient()}>
        <MonthWorkspace month="2026-07" initialDay="2026-07-15" />
      </QueryClientProvider>,
    );
    expect(await screen.findByRole("heading", { name: "Wed, 15 July" })).toBeInTheDocument();
    view.unmount();
    // A hand-edited URL must not move the day panel — and with it the day
    // editor's new-interval default — onto a date this month does not own.
    render(
      <QueryClientProvider client={new QueryClient()}>
        <MonthWorkspace month="2026-07" initialDay="2026-08-01" />
      </QueryClientProvider>,
    );
    expect(await screen.findByRole("heading", { name: "Wed, 1 July" })).toBeInTheDocument();
  });
  beforeEach(() => {
    monthError = null;
    extraMonthDays = [];
    shiftsResponse = [];
    identityCapabilities = [];
    dayStatusResponse = {
      period: "2026-07",
      entries: [],
      compensation_metrics: compensationMetrics,
    };
    julyDailyResult = {
      date: "2026-07-06",
      worked_minutes: 480,
      required_minutes: 348,
      delta_minutes: 132,
      calendar_class: "WEEKDAY",
      account_credit_minutes: 480,
    };
    daySixIntervals = fourIntervals;
    daySixBreaks = [];
    octoberIntervals = [];
    octoberTwentyFourthIntervals = [];
    findingsResponse = [
      {
        rule_id: "WT-CORE-003",
        severity: "WARNING",
        date: "2026-07-06",
        message: "Take a qualifying break.",
      },
    ];
    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/day-status"))
        return new Response(JSON.stringify(dayStatusResponse), { status: 200 });
      if (url.includes("/findings"))
        return new Response(
          JSON.stringify({
            closing_balance_minutes: 18,
            findings: findingsResponse,
          }),
          { status: 200 },
        );
      if (url.includes("/profile"))
        return new Response(
          JSON.stringify({
            employee: {
              legal_name: "Synthetic Employee",
              external_employee_id: "emp-0001",
              email: null,
            },
            contract: null,
            schedule: null,
            preferences: {
              usual_work_type: "ON_SITE",
              preferred_calendar_view: "month",
              normal_day_template: [{ start: "08:00:00", end: "16:18:00", work_type: "ON_SITE" }],
            },
          }),
          { status: 200 },
        );
      if (url.includes("/shifts"))
        return new Response(JSON.stringify(shiftsResponse), { status: 200 });
      if (url.includes("/break-intervals/") && init?.method === "PATCH") {
        // A correction mints a NEW head row (new id + version); a later read
        // returns that head, never the superseded one.
        const head = {
          id: "break-head-2",
          kind: "OTHER",
          start_at: "2026-07-06T12:00:00+02:00",
          end_at: "2026-07-06T12:45:00+02:00",
          start_local: "2026-07-06T12:00:00",
          end_local: "2026-07-06T12:45:00",
          elapsed_seconds: 2700,
          linked_shift_assignment_id: null,
          version: 3,
          supersedes_id: "break-head",
          client_request_id: null,
        };
        daySixBreaks = [head];
        return new Response(JSON.stringify(head), { status: 200 });
      }
      if (url.includes("/days/2026-07-06"))
        return new Response(
          JSON.stringify({ date: "2026-07-06", intervals: daySixIntervals, breaks: daySixBreaks }),
          {
            status: 200,
          },
        );
      if (url.includes("/days/2026-10-24"))
        return new Response(
          JSON.stringify({ date: "2026-10-24", intervals: octoberTwentyFourthIntervals }),
          { status: 200 },
        );
      if (url.includes("/days/2026-10-25"))
        return new Response(JSON.stringify({ date: "2026-10-25", intervals: octoberIntervals }), {
          status: 200,
        });
      if (url.includes("/days/"))
        return new Response(JSON.stringify({ date: "2026-07-01", intervals: [] }), { status: 200 });
      if (url.includes("/apply-template")) return new Response(JSON.stringify([]), { status: 200 });
      if (url.includes("/onboarding")) {
        return new Response(JSON.stringify(onboardingForTest), { status: 200 });
      }
      if (url.includes("/months/2026-07")) {
        if (monthError)
          return new Response(
            JSON.stringify({
              detail: { code: monthError.code, message: "The server refused this month." },
            }),
            { status: monthError.status },
          );
        return new Response(
          JSON.stringify({
            period_month: "2026-07-01",
            status: "OPEN",
            interval_count: 4,
            closing_balance_minutes: 18,
            days: [
              { date: "2026-07-05", interval_count: 2 },
              { date: "2026-07-06", interval_count: 4 },
              ...extraMonthDays,
            ],
            daily_results: [julyDailyResult],
          }),
          { status: 200 },
        );
      }
      // The identity read shares the shell's `["identity"]` cache entry. Exact
      // match, because `/api/v1/me/shifts` and friends are different routes.
      if (url === "/api/v1/me")
        return new Response(JSON.stringify({ capabilities: identityCapabilities }), {
          status: 200,
        });
      return new Response(JSON.stringify({ method: init?.method }), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  async function openJulySixth(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole("button", { name: /^Monday, 6 July 2026/ }));
    await screen.findAllByText("08:00–10:00");
  }

  // ---------------------------------------------------------------------------
  // Months before the employee's own Chronobeam start
  // ---------------------------------------------------------------------------

  /** The balance line splits across text nodes, so assert on the element's own
   *  text rather than on a node-by-node match. */
  async function balanceLine(): Promise<string> {
    await waitFor(() => expect(document.querySelector(".balance")).not.toBeNull());
    return document.querySelector(".balance")?.textContent ?? "";
  }

  it("reports the archive instead of a closing balance for a month before the start", async () => {
    onboardingForTest = startedIn("2026-10");
    renderWorkspace();
    // July 2026 is before an October start: the service did not run then, so there
    // is no balance to report — and reporting one reads as debt never owed.
    expect(await balanceLine()).toMatch(/Before your Chronobeam start \(October 2026\)/);
    expect(await balanceLine()).not.toMatch(/Balance at end/);
  });

  it("still reports a balance for the start month itself and later", async () => {
    onboardingForTest = startedIn("2026-07");
    renderWorkspace();
    // The start month IS part of the service, so the balance is the ordinary one.
    expect(await balanceLine()).toMatch(/Balance at end of July 2026/);
  });

  it("reports the balance for an employee who has not started yet", async () => {
    // No start state at all: nothing is pre-cutover, so the balance stands.
    onboardingForTest = { ...onboardingForTest, state: "READY", opening: null };
    renderWorkspace();
    expect(await balanceLine()).toMatch(/Balance at end of July 2026/);
  });

  it("renders a compact four-interval mixed-location day with totals on top, breaks and a private finding", async () => {
    const user = userEvent.setup();
    renderWorkspace();
    await openJulySixth(user);
    // Normal state: compact rows, no permanently expanded CRUD forms.
    expect(fieldsets()).toHaveLength(0);
    expect(screen.getAllByText(/08:00–10:00|10:15–12:00|12:30–14:00|14:15–17:00/)).toHaveLength(4);
    expect(screen.getAllByText("On-site")).toHaveLength(2);
    expect(screen.getAllByText("Remote")).toHaveLength(2);
    expect(screen.getByText("2:00")).toBeInTheDocument();
    expect(screen.getByText("1:45")).toBeInTheDocument();
    expect(screen.getByText("2:45")).toBeInTheDocument();
    // Server-derived totals sit above the interval list (spec §5).
    // The panel is a region named by its VISIBLE heading, so the name a screen
    // reader announces and the heading on screen cannot drift apart. It used to be
    // a written-out "Timeline for <ISO date>", which stopped describing the panel
    // once the panel grew its four sections.
    const panel = screen.getByRole("region", { name: /6 July/ });
    const totals = screen.getByLabelText("Server-calculated day totals");
    expect(totals.textContent).toContain("Worked");
    expect(totals.textContent).toContain("8:00");
    expect(totals.textContent).toContain("5:48");
    expect(totals.textContent).toContain("+2:12");
    expect(panel).toContainElement(totals);
    // Compared by DOCUMENT POSITION, not by direct-child index: the timeline now
    // lives inside the day panel's "Actual work" section, so it is no longer a
    // sibling of the totals. The property the spec asks for — the server's totals
    // are read before the interval list — is unchanged.
    expect(
      totals.compareDocumentPosition(panel.querySelector(".timeline") as Element) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // Derived break labels for the positive gaps (15/30/15 minutes).
    expect(screen.getAllByText("15 min break")).toHaveLength(2);
    expect(screen.getByText("30 min break")).toBeInTheDocument();
    // Finding pin near the totals: compact summary only — the day panel no
    // longer duplicates the list below it (F14).
    expect(screen.getByText("⚠ Take a qualifying break.")).toBeInTheDocument();
    expect(screen.queryByText(/nothing is sent to HR/)).not.toBeInTheDocument();
    expect(screen.getAllByText(/Take a qualifying break/)).toHaveLength(2); // pin summary + pin detail, no duplicate list
  });

  it("opens a focused editor on demand, saves edits, and closes again", async () => {
    const user = userEvent.setup();
    renderWorkspace();
    await openJulySixth(user);
    await user.click(screen.getByRole("button", { name: "Edit interval 08:00–10:00" }));
    await waitFor(() => expect(fieldsets()).toHaveLength(1));
    const editor = document.querySelector("fieldset") as HTMLFieldSetElement;
    expect(within(editor).getByLabelText("Work type")).toHaveValue("ON_SITE");
    fireEvent.change(within(editor).getByLabelText("Start time"), { target: { value: "08:30" } });
    await user.click(within(editor).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(fieldsets()).toHaveLength(0));
    const patch = fetchMock.mock.calls.find(
      ([input, init]) =>
        String(input).includes("/work-intervals/interval-0") && init?.method === "PATCH",
    );
    expect(patch).toBeTruthy();
    expect(JSON.parse((patch![1] as RequestInit).body as string)).toMatchObject({
      start_local: "2026-07-06T08:30:00",
    });
  });

  it("cancelling discards unsaved edits without calling the API", async () => {
    const user = userEvent.setup();
    renderWorkspace();
    await openJulySixth(user);
    await user.click(screen.getByRole("button", { name: "Edit interval 08:00–10:00" }));
    await waitFor(() => expect(fieldsets()).toHaveLength(1));
    const editor = document.querySelector("fieldset") as HTMLFieldSetElement;
    fireEvent.change(within(editor).getByLabelText("Start time"), { target: { value: "08:45" } });
    await user.click(within(editor).getByRole("button", { name: "Cancel" }));
    expect(fieldsets()).toHaveLength(0);
    expect(
      fetchMock.mock.calls.some(([input]) => String(input).includes("/work-intervals/interval-0")),
    ).toBe(false);
  });

  it("adds an interval with intelligent defaults behind the primary action", async () => {
    const user = userEvent.setup();
    renderWorkspace();
    await openJulySixth(user);
    expect(screen.queryByLabelText("New start time")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "+ Add interval" }));
    await waitFor(() => expect(fieldsets()).toHaveLength(1));
    // Deterministic defaults: start = end of latest interval, type follows it.
    expect(screen.getByLabelText("New start time")).toHaveValue("17:00");
    expect(screen.getByLabelText("New work type")).toHaveValue("REMOTE");
    // No end time is invented; Add stays disabled until the user enters one.
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("New end time"), { target: { value: "18:00" } });
    await user.click(screen.getByRole("button", { name: "Add" }));
    const post = fetchMock.mock.calls.find(
      ([input, init]) => String(input).includes("/work-intervals") && init?.method === "POST",
    );
    expect(post).toBeTruthy();
    expect(JSON.parse((post![1] as RequestInit).body as string)).toMatchObject({
      start_local: "2026-07-06T17:00:00",
      end_local: "2026-07-06T18:00:00",
      work_type: "REMOTE",
    });
    await waitFor(() => expect(fieldsets()).toHaveLength(0));
  });

  it("refuses an interval that ends before it starts, and states why", async () => {
    // UI-012: `14:30 → 08:00` with "Ends next day" unticked is a negative span
    // the server rejects (VALIDATION_INTERVAL_ORDER). The form used to leave
    // `Add` enabled and let a round-trip explain it somewhere else on the page.
    const user = userEvent.setup();
    renderWorkspace();
    await openJulySixth(user);
    await user.click(screen.getByRole("button", { name: "+ Add interval" }));
    await waitFor(() => expect(fieldsets()).toHaveLength(1));
    // An untouched form is not an error: the empty End is why `Add` is
    // disabled, and that needs no message.
    expect(screen.queryByText("Enter a time as HH:MM.")).not.toBeInTheDocument();
    const end = screen.getByLabelText("New end time");
    fireEvent.change(screen.getByLabelText("New start time"), { target: { value: "14:30" } });
    fireEvent.change(end, { target: { value: "08:00" } });
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
    expect(end).toHaveAttribute("aria-invalid", "true");
    const reason = screen.getByText(/must be after the start time/);
    expect(end).toHaveAttribute("aria-describedby", reason.id);
    // The checkbox is what decides: the same two times are a valid overnight
    // interval once the end belongs to the next day.
    await user.click(screen.getByLabelText("Ends next day"));
    expect(screen.getByRole("button", { name: "Add" })).toBeEnabled();
    expect(screen.queryByText(/must be after the start time/)).not.toBeInTheDocument();
    expect(end).not.toHaveAttribute("aria-invalid");
    // A time that is not HH:MM is refused with a reason too, not silently.
    fireEvent.change(end, { target: { value: "25:99" } });
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
    expect(screen.getByText("Enter a time as HH:MM.")).toBeInTheDocument();
  });

  it("preserves the following-day date in a cross-midnight latest-end default", async () => {
    const user = userEvent.setup();
    daySixIntervals = [
      {
        id: "overnight",
        start_at: "2026-07-06T19:00:00+02:00",
        end_at: "2026-07-07T02:30:00+02:00",
        start_local: "2026-07-06T19:00:00",
        end_local: "2026-07-07T02:30:00",
        work_type: "SHIFT",
        elapsed_seconds: 7.5 * 60 * 60,
        comment: null,
        version: 1,
      },
    ];
    renderWorkspace();
    await user.click(await screen.findByRole("button", { name: /^Monday, 6 July 2026/ }));
    await screen.findByText("19:00–02:30");
    await user.click(screen.getByRole("button", { name: "+ Add interval" }));
    expect(screen.getByLabelText("New start time")).toHaveValue("02:30");
    expect(
      screen.getByText("The latest interval ends on Tue 7 Jul; this entry starts then."),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("New end time"), { target: { value: "04:00" } });
    await user.click(screen.getByRole("button", { name: "Add" }));
    const post = fetchMock.mock.calls.find(
      ([input, init]) => String(input).includes("/work-intervals") && init?.method === "POST",
    );
    expect(JSON.parse((post![1] as RequestInit).body as string)).toMatchObject({
      start_local: "2026-07-07T02:30:00",
      end_local: "2026-07-07T04:00:00",
    });
  });

  it("requires an explicit shared occurrence choice when an added start is in the fall-back hour", async () => {
    const user = userEvent.setup();
    octoberIntervals = [
      {
        id: "fall-back",
        start_at: "2026-10-25T02:30:00+02:00",
        end_at: "2026-10-25T03:30:00+01:00",
        start_local: "2026-10-25T02:30:00",
        end_local: "2026-10-25T03:30:00",
        work_type: "SHIFT",
        elapsed_seconds: 2 * 60 * 60,
        comment: null,
        version: 1,
      },
    ];
    render(
      <QueryClientProvider client={new QueryClient()}>
        <DayTimeline
          month="2026-10"
          day="2026-10-25"
          usualWorkType="ON_SITE"
          onFactsChanged={() => {}}
        />
      </QueryClientProvider>,
    );
    await screen.findByText("02:30–03:30");
    await user.click(screen.getByRole("button", { name: "+ Add interval" }));
    fireEvent.change(screen.getByLabelText("New start time"), { target: { value: "02:30" } });
    fireEvent.change(screen.getByLabelText("New end time"), { target: { value: "04:00" } });
    const occurrence = screen.getByLabelText("Occurrence for start time");
    const add = screen.getByRole("button", { name: "Add" });
    expect(add).toBeDisabled();
    // A disabled primary must state the requirement and have it associated with
    // the control, not leave the reason inside an unrelated <select>'s
    // placeholder (acceptance C1). docs/ARCHITECTURE.md: an ambiguous wall time is
    // detected and SURFACED, never silently guessed.
    const requirement = document.getElementById("new-interval-requirement");
    expect(requirement).toHaveTextContent(/falls in the hour that happens twice/);
    expect(add).toHaveAccessibleDescription(/falls in the hour that happens twice/);
    await user.selectOptions(occurrence, "0");
    expect(screen.getByRole("button", { name: "Add" })).toBeEnabled();
    expect(document.getElementById("new-interval-requirement")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Add" }));
    const post = fetchMock.mock.calls.find(
      ([input, init]) => String(input).includes("/work-intervals") && init?.method === "POST",
    );
    expect(JSON.parse((post![1] as RequestInit).body as string)).toMatchObject({
      start_local: "2026-10-25T02:30:00",
      end_local: "2026-10-25T04:00:00",
      fold: 0,
    });
  });

  it("gives each day of a cross-midnight interval its own elapsed portion, across the fall-back night", async () => {
    // 22:00 CEST on the 24th to 06:00 CET on the 25th = 9 elapsed hours, of
    // which 2 fall on the 24th and 7 on the 25th (the clock gains an hour at
    // 03:00). No wall-clock subtraction can produce both numbers.
    const overnight = {
      id: "overnight",
      start_at: "2026-10-24T22:00:00+02:00",
      end_at: "2026-10-25T06:00:00+01:00",
      start_local: "2026-10-24T22:00:00",
      end_local: "2026-10-25T06:00:00",
      work_type: "SHIFT",
      elapsed_seconds: 9 * 60 * 60,
      comment: null,
      version: 1,
    };
    octoberTwentyFourthIntervals = [overnight];
    octoberIntervals = [overnight];
    const firstDay = render(
      <QueryClientProvider client={new QueryClient()}>
        <DayTimeline
          month="2026-10"
          day="2026-10-24"
          usualWorkType="ON_SITE"
          onFactsChanged={() => {}}
        />
      </QueryClientProvider>,
    );
    expect(await screen.findByText("2:00 on this day · 9:00 total")).toBeInTheDocument();
    firstDay.unmount();
    render(
      <QueryClientProvider client={new QueryClient()}>
        <DayTimeline
          month="2026-10"
          day="2026-10-25"
          usualWorkType="ON_SITE"
          onFactsChanged={() => {}}
        />
      </QueryClientProvider>,
    );
    expect(await screen.findByText("7:00 on this day · 9:00 total")).toBeInTheDocument();
  });

  it("keeps Duplicate/Split/Delete behind the overflow menu", async () => {
    const user = userEvent.setup();
    renderWorkspace();
    await openJulySixth(user);
    expect(screen.queryByRole("button", { name: "Duplicate" })).not.toBeInTheDocument();
    // Duplicate after the first interval.
    await user.click(screen.getByRole("button", { name: "Interval actions for 08:00–10:00" }));
    await user.click(await screen.findByRole("button", { name: "Duplicate" }));
    const duplicate = fetchMock.mock.calls.find(
      ([input, init]) => String(input).includes("/work-intervals") && init?.method === "POST",
    );
    expect(duplicate).toBeTruthy();
    expect(JSON.parse((duplicate![1] as RequestInit).body as string)).toMatchObject({
      start_local: "2026-07-06T10:00",
      end_local: "2026-07-06T12:00",
      work_type: "ON_SITE",
    });
    // Split opens the focused editor with the split controls.
    await user.click(screen.getByRole("button", { name: "Interval actions for 08:00–10:00" }));
    await user.click(screen.getByRole("button", { name: "Split" }));
    await waitFor(() => expect(fieldsets()).toHaveLength(1));
    fireEvent.change(screen.getByLabelText("Split interval at"), { target: { value: "09:00" } });
    await user.click(screen.getByRole("button", { name: "Split here" }));
    await waitFor(() => expect(fieldsets()).toHaveLength(0));
    const split = fetchMock.mock.calls.find(([input]) =>
      String(input).includes("/work-intervals/interval-0/split"),
    );
    expect(split).toBeTruthy();
    expect(JSON.parse((split![1] as RequestInit).body as string)).toMatchObject({
      version: 1,
      split_local: "2026-07-06T09:00:00",
    });
    // Delete via the menu — two steps: arming must not send anything (UI-006).
    await user.click(screen.getByRole("button", { name: "Interval actions for 12:30–14:00" }));
    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          String(input).includes("/work-intervals/interval-2") && init?.method === "DELETE",
      ),
    ).toBe(false);
    await user.click(screen.getByRole("button", { name: "Delete interval" }));
    const remove = fetchMock.mock.calls.find(
      ([input, init]) =>
        String(input).includes("/work-intervals/interval-2") && init?.method === "DELETE",
    );
    expect(remove).toBeTruthy();
  });

  it("renders the day's explicit breaks and records a new one with the right body", async () => {
    const user = userEvent.setup();
    daySixBreaks = [
      {
        id: "break-1",
        kind: "UNPAID",
        start_at: "2026-07-06T12:00:00+02:00",
        end_at: "2026-07-06T12:30:00+02:00",
        start_local: "2026-07-06T12:00:00",
        end_local: "2026-07-06T12:30:00",
        elapsed_seconds: 1800,
        linked_shift_assignment_id: null,
        version: 1,
        supersedes_id: null,
        client_request_id: null,
      },
    ];
    renderWorkspace();
    await openJulySixth(user);
    // The recorded break renders with its kind and local times, distinct from
    // the work rows.
    expect(screen.getByText("Unpaid break")).toBeInTheDocument();
    expect(screen.getByText("12:00–12:30")).toBeInTheDocument();
    // Add a paid shift break: compensated presence, not a gap (SHIFT-003).
    await user.click(screen.getByRole("button", { name: "+ Add break" }));
    fireEvent.change(screen.getByLabelText("New break start time"), { target: { value: "09:30" } });
    fireEvent.change(screen.getByLabelText("New break end time"), { target: { value: "09:45" } });
    await user.selectOptions(screen.getByLabelText("New break kind"), "PAID_SHIFT_BREAK");
    const shiftsCalls = () =>
      fetchMock.mock.calls.filter(([input]) => String(input).includes("/shifts")).length;
    const shiftsBefore = shiftsCalls();
    await user.click(screen.getByRole("button", { name: "Add" }));
    const post = fetchMock.mock.calls.find(
      ([input, init]) => String(input).includes("/break-intervals") && init?.method === "POST",
    );
    expect(post).toBeTruthy();
    expect(JSON.parse((post![1] as RequestInit).body as string)).toMatchObject({
      start_local: "2026-07-06T09:30:00",
      end_local: "2026-07-06T09:45:00",
      kind: "PAID_SHIFT_BREAK",
    });
    // A break is a calculation input for any shift it is linked to (SHIFT-003's
    // paid-break requirement), so the shift surface must refresh too — otherwise
    // it keeps showing a requirement state the employee has just changed.
    await waitFor(() => expect(shiftsCalls()).toBeGreaterThan(shiftsBefore));
  });

  it("deletes a break targeting the current head id and version", async () => {
    const user = userEvent.setup();
    daySixBreaks = [
      {
        id: "break-head",
        kind: "OTHER",
        start_at: "2026-07-06T12:00:00+02:00",
        end_at: "2026-07-06T12:30:00+02:00",
        start_local: "2026-07-06T12:00:00",
        end_local: "2026-07-06T12:30:00",
        elapsed_seconds: 1800,
        linked_shift_assignment_id: null,
        version: 2,
        supersedes_id: "break-old",
        client_request_id: null,
      },
    ];
    renderWorkspace();
    await openJulySixth(user);
    await user.click(screen.getByRole("button", { name: "Break actions for 12:00–12:30" }));
    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          String(input).includes("/break-intervals/break-head?version=2") &&
          init?.method === "DELETE",
      ),
    ).toBe(false);
    await user.click(screen.getByRole("button", { name: "Delete break" }));
    const remove = fetchMock.mock.calls.find(
      ([input, init]) =>
        String(input).includes("/break-intervals/break-head?version=2") &&
        init?.method === "DELETE",
    );
    expect(remove).toBeTruthy();
  });

  it("corrects a break via PATCH and shows the returned head, never the superseded id", async () => {
    const user = userEvent.setup();
    daySixBreaks = [
      {
        id: "break-head",
        kind: "OTHER",
        start_at: "2026-07-06T12:00:00+02:00",
        end_at: "2026-07-06T12:30:00+02:00",
        start_local: "2026-07-06T12:00:00",
        end_local: "2026-07-06T12:30:00",
        elapsed_seconds: 1800,
        linked_shift_assignment_id: null,
        version: 2,
        supersedes_id: "break-old",
        client_request_id: null,
      },
    ];
    renderWorkspace();
    await openJulySixth(user);
    await user.click(screen.getByRole("button", { name: "Edit break 12:00–12:30" }));
    await waitFor(() => expect(fieldsets()).toHaveLength(1));
    const editor = document.querySelector("fieldset") as HTMLFieldSetElement;
    fireEvent.change(within(editor).getByLabelText("Break end time"), {
      target: { value: "12:45" },
    });
    await user.click(within(editor).getByRole("button", { name: "Save" }));
    const patch = fetchMock.mock.calls.find(
      ([input, init]) =>
        String(input).includes("/break-intervals/break-head") && init?.method === "PATCH",
    );
    expect(patch).toBeTruthy();
    // Presence of the current head version + the changed field; a stale version
    // would be rejected by the server as a conflict.
    expect(JSON.parse((patch![1] as RequestInit).body as string)).toMatchObject({
      version: 2,
      end_local: "2026-07-06T12:45:00",
    });
    // The row must read the returned head (new id + version), not the old row.
    await waitFor(() => expect(fieldsets()).toHaveLength(0));
    expect(await screen.findByText("12:00–12:45")).toBeInTheDocument();
    expect(screen.queryByText("12:00–12:30")).not.toBeInTheDocument();
  });

  it("records a cross-midnight break with the next day's date in end_local", async () => {
    const user = userEvent.setup();
    renderWorkspace();
    await openJulySixth(user);
    await user.click(screen.getByRole("button", { name: "+ Add break" }));
    fireEvent.change(screen.getByLabelText("New break start time"), { target: { value: "23:45" } });
    fireEvent.change(screen.getByLabelText("New break end time"), { target: { value: "00:15" } });
    await user.click(screen.getByRole("checkbox", { name: "Ends next day" }));
    await user.click(screen.getByRole("button", { name: "Add" }));
    const post = fetchMock.mock.calls.find(
      ([input, init]) => String(input).includes("/break-intervals") && init?.method === "POST",
    );
    expect(post).toBeTruthy();
    expect(JSON.parse((post![1] as RequestInit).body as string)).toMatchObject({
      start_local: "2026-07-06T23:45:00",
      end_local: "2026-07-07T00:15:00",
      kind: "UNPAID",
    });
  });

  it("supports keyboard operation: Enter opens the editor, Escape closes it", async () => {
    const user = userEvent.setup();
    renderWorkspace();
    await openJulySixth(user);
    screen.getByRole("button", { name: "Edit interval 08:00–10:00" }).focus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(fieldsets()).toHaveLength(1));
    await user.keyboard("{Escape}");
    await waitFor(() => expect(fieldsets()).toHaveLength(0));
  });

  // 9.3: the slot is fixed, so the control is present on every day and disabled
  // rather than absent when the previous day has nothing to copy. The 5th stands
  // in for the 1st here: in both cases the previous day is absent from the
  // month's day list, which is exactly the guard being tested.
  it("keeps Copy yesterday in the action row but disabled when the previous day is empty", async () => {
    const user = userEvent.setup();
    renderWorkspace();
    await user.click(await screen.findByRole("button", { name: /^Sunday, 5 July 2026/ }));
    const copy = screen.getByRole("button", { name: "Copy yesterday" });
    expect(copy).toBeDisabled();
    expect(screen.getByRole("button", { name: "Apply normal day" })).toBeInTheDocument();
  });

  it("offers Copy yesterday and Apply normal day as compact quick actions", async () => {
    const user = userEvent.setup();
    renderWorkspace();
    await openJulySixth(user);
    await user.click(screen.getByRole("button", { name: "Copy yesterday" }));
    const copy = fetchMock.mock.calls.find(([input]) => String(input).includes("/copy-previous"));
    expect(copy).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Apply normal day" }));
    const apply = fetchMock.mock.calls.find(([input]) => String(input).includes("/apply-template"));
    expect(apply).toBeTruthy();
    expect(JSON.parse((apply![1] as RequestInit).body as string)).toMatchObject({
      dates: ["2026-07-06"],
    });
  });

  /**
   * "Copy yesterday" writes THIS day's facts from a different component than the
   * one holding the preview, so wiring only the timeline's own writers left the
   * preview claiming a day the copy had just filled (review R6).
   */
  it("drops the normal-week preview when Copy yesterday writes the day", async () => {
    const user = userEvent.setup();
    renderWorkspace();
    await openJulySixth(user);
    await user.click(screen.getByText("More…"));
    await user.click(screen.getByRole("button", { name: "Preview normal work week" }));
    const preview = () => screen.getByLabelText("Normal week preview").textContent ?? "";
    // 6 July already carries intervals; the week Mon 6 – Fri 10 July is 07–10.
    expect(preview()).toContain("07");

    await user.click(screen.getByRole("button", { name: "Copy yesterday" }));

    await waitFor(() =>
      expect(screen.queryByLabelText("Normal week preview")).not.toBeInTheDocument(),
    );
  });

  /**
   * The normal-week preview claims which days "will receive your saved
   * normal-day pattern", and eligibility is `!intervalsByDate.get(date)` — a
   * fact. Held as a snapshot it went on claiming a day the employee had since
   * recorded time on, and applying it asked the server for something it refuses
   * (review R17). The claim is now derived from the same month data the calendar
   * is drawn from, so it cannot promise more than the month already shows.
   */
  it("drops a previewed day the moment it gains an interval", async () => {
    const user = userEvent.setup();
    const client = newQueryClient();
    renderWorkspace(client);
    await openJulySixth(user);
    await user.click(screen.getByText("More…"));
    await user.click(screen.getByRole("button", { name: "Preview normal work week" }));

    const preview = () => screen.getByLabelText("Normal week preview").textContent ?? "";
    // 6 July is the selected day and already carries 4 intervals, so the week
    // Mon 6 – Fri 10 July is offered as 07–10.
    expect(preview()).toContain("07");
    expect(preview()).toContain("10");

    // A write lands on the 7th; the month payload now reports it.
    extraMonthDays = [{ date: "2026-07-07", interval_count: 1 }];
    await client.invalidateQueries({ queryKey: ["month"] });

    await waitFor(() => expect(preview()).not.toContain("07"));
    expect(preview()).toContain("08");
    expect(preview()).toContain("10");
  });

  it("shows a confirmation preview before applying a normal work week", async () => {
    const user = userEvent.setup();
    renderWorkspace();
    await openJulySixth(user);
    await user.click(screen.getByText("More…"));
    await user.click(screen.getByRole("button", { name: "Preview normal work week" }));
    expect(screen.getByText("Preview before creating actual work")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Apply to these days" }));
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/me/months/2026-07/apply-template",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("keeps the month workspace available when a stale shifts fixture is not a list", async () => {
    shiftsResponse = {};
    renderWorkspace();
    expect((await screen.findAllByText("Private — visible only to you")).length).toBeGreaterThan(0);
  });

  it("starts calendar weeks on Monday (Europe/Berlin convention, §17)", async () => {
    const { container } = renderWorkspace();
    await screen.findByRole("heading", { name: "Month" }); // calendar renders only after month data loads
    expect(container.querySelector(".calendar-weekdays")?.textContent?.trim()).toBe(
      "MonTueWedThuFriSatSun",
    );
    // 2026-07-01 is a Wednesday -> Monday-first grid has exactly 2 leading blanks.
    expect(container.querySelectorAll(".calendar-grid > span[aria-hidden='true']")).toHaveLength(2);
    expect(container.querySelectorAll(".calendar-grid .day-cell")).toHaveLength(31);
  });

  it("marks a day with its recorded status in the cell and in its accessible name", async () => {
    dayStatusResponse = { period: "2026-07", entries: [vacationFull] };
    renderWorkspace();
    // The cell's accessible name carries the event's SPOKEN phrase — the bars are
    // aria-hidden, so this name is the only place a screen reader can reach the
    // status at all.
    const cell = await screen.findByRole("button", { name: /Vacation \(full day\)/ });
    // The category word and the extent are separate elements so the phone density
    // can drop the extent; at desktop density both must still read as one label.
    const bar = cell.querySelector(".cal-bar") as HTMLElement;
    expect(bar).toHaveTextContent("Vacation");
    // A FULL day states NOTHING: it is the default, and measured against the live
    // grid a bar has ~77px of text space while "Shift comp" plus "full" needs 80,
    // so the extra word ellipsized the category it was meant to qualify. The full
    // phrase ("Vacation (full day)") still reaches assistive technology through the
    // cell's accessible name, asserted above, and the day panel spells it out.
    expect(bar.querySelector(".cal-bar-detail")).toBeNull();
    // The category is carried by a WORD as well as by the hue, so the meaning does
    // not depend on colour.
    expect(bar.querySelector(".cal-bar-label")?.textContent).toBe("Vacation");
    expect(bar.getAttribute("data-cal-kind")).toBe("VACATION");
    // The two-letter glyph is present for the phone density but is not the visible
    // label at desktop density.
    expect(bar.querySelector(".cal-bar-glyph")?.textContent).toBe("Va");
  });

  it("states the exception on a half-day status bar, where a full day says nothing", async () => {
    dayStatusResponse = { period: "2026-07", entries: [vacationHalf] };
    renderWorkspace();
    const cell = await screen.findByRole("button", { name: /Vacation \(morning half\)/ });
    const bar = cell.querySelector(".cal-bar") as HTMLElement;
    // "am", not "morning half": twelve characters ellipsized in every cell that had
    // one. The full words stay in the accessible name and in the day panel.
    expect(bar.querySelector(".cal-bar-detail")?.textContent).toBe("am");
    expect(bar.querySelector(".cal-bar-label")?.textContent).toBe("Vacation");
  });

  it("records a full-day vacation, sends the create request and refreshes the month", async () => {
    const user = userEvent.setup();
    renderWorkspace();
    await openJulySixth(user);
    const monthFetchesBefore = monthFetches();
    await user.click(screen.getByRole("button", { name: "+ Add day status" }));
    await user.selectOptions(screen.getByLabelText("Day status category"), "VACATION");
    await user.click(screen.getByRole("button", { name: "Add" }));
    const post = fetchMock.mock.calls.find(
      ([input, init]) => String(input).includes("/day-status") && init?.method === "POST",
    );
    expect(post).toBeTruthy();
    expect(JSON.parse((post![1] as RequestInit).body as string)).toMatchObject({
      day: "2026-07-06",
      category: "VACATION",
      portion: "FULL",
      day_part: "ALL",
    });
    // The derived requirement and findings come from the server, so the month
    // (and its findings) must be refetched after the fact changes.
    await waitFor(() => expect(monthFetches()).toBeGreaterThan(monthFetchesBefore));
  });

  /**
   * The fact-derived contract is owned by one helper (`lib/query.ts`). This pins
   * that the workspace actually calls it, for the caches no writer used to see:
   * Home's own balance cache, and the neighbouring month a cross-boundary fact
   * touches. Only non-mounted caches are seeded, so the workspace still loads
   * normally.
   */
  it("refreshes fact-derived views the workspace never names, after a day-status write", async () => {
    const user = userEvent.setup();
    const client = newQueryClient();
    for (const key of [
      ["month", "2026-08"],
      ["home-month-summary", "2026-07"],
      ["travel-trips"],
      ["identity"],
    ]) {
      client.setQueryData(key, { seeded: true });
    }
    render(
      <QueryClientProvider client={client}>
        <MonthWorkspace month="2026-07" />
      </QueryClientProvider>,
    );
    await openJulySixth(user);
    await user.click(screen.getByRole("button", { name: "+ Add day status" }));
    await user.selectOptions(screen.getByLabelText("Day status category"), "VACATION");
    await user.click(screen.getByRole("button", { name: "Add" }));

    await waitFor(() =>
      expect(client.getQueryState(["home-month-summary", "2026-07"])?.isInvalidated).toBe(true),
    );
    expect(client.getQueryState(["month", "2026-08"])?.isInvalidated).toBe(true);
    expect(client.getQueryState(["travel-trips"])?.isInvalidated).toBe(true);
    // Not a sledgehammer: an unrelated cache keeps its data.
    expect(client.getQueryState(["identity"])?.isInvalidated).toBe(false);
  });

  it("opens the day-status form on no category and refuses to add one", async () => {
    // UI-031: the form used to open with `Vacation` selected and `Add` enabled,
    // so one click recorded a sensitive absence fact nobody had chosen.
    const user = userEvent.setup();
    renderWorkspace();
    await openJulySixth(user);
    await user.click(screen.getByRole("button", { name: "+ Add day status" }));
    expect(screen.getByLabelText("Day status category")).toHaveValue("");
    // The extent keeps its deterministic default; only the fact is unchosen.
    expect(screen.getByLabelText("Day status extent")).toHaveValue("FULL");
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
    await user.selectOptions(screen.getByLabelText("Day status category"), "VACATION");
    expect(screen.getByRole("button", { name: "Add" })).toBeEnabled();
  });

  it("reveals the half-day choice and refuses half-day sickness before the request", async () => {
    const user = userEvent.setup();
    renderWorkspace();
    await openJulySixth(user);
    await user.click(screen.getByRole("button", { name: "+ Add day status" }));
    expect(screen.queryByLabelText("Half of the day")).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Day status extent"), "HALF");
    const halfOfDay = screen.getByLabelText("Half of the day");
    expect(within(halfOfDay).getByRole("option", { name: "Morning" })).toBeInTheDocument();
    expect(within(halfOfDay).getByRole("option", { name: "Afternoon" })).toBeInTheDocument();
    // Half-day sickness cannot be recorded yet: offered disabled with the
    // reason, so the refusal is visible before the request. The reason is in
    // employee words — no internal register id reaches the user.
    await user.selectOptions(screen.getByLabelText("Day status category"), "SICK");
    expect(screen.getByRole("option", { name: "Half day" })).toBeDisabled();
    expect(
      screen.getByText(/How a half sick day counts is still being confirmed/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/ABSENCE-|MISSING-/)).not.toBeInTheDocument();
    // …and the form stays on a combination the server can record.
    expect(screen.getByLabelText("Day status extent")).toHaveValue("FULL");
    expect(screen.queryByLabelText("Half of the day")).not.toBeInTheDocument();
  });

  it("corrects a recorded status to a half day and removes it", async () => {
    const user = userEvent.setup();
    dayStatusResponse = { period: "2026-07", entries: [vacationFull] };
    renderWorkspace();
    await openJulySixth(user);
    await user.click(screen.getByRole("button", { name: "Correct Vacation (full day)" }));
    await user.selectOptions(screen.getByLabelText("Day status extent"), "HALF");
    await user.click(screen.getByRole("button", { name: "Save" }));
    const patch = fetchMock.mock.calls.find(
      ([input, init]) => String(input).includes("/day-status/status-1") && init?.method === "PATCH",
    );
    expect(patch).toBeTruthy();
    // Presence-based: only what changed is sent, never a full-row replace.
    expect(JSON.parse((patch![1] as RequestInit).body as string)).toEqual({
      version: 1,
      portion: "HALF",
      day_part: "MORNING",
    });
    // Two steps (UI-006): the accessible name stays on the resting control, and
    // arming alone must not delete the fact.
    await user.click(screen.getByRole("button", { name: "Remove Vacation (full day)" }));
    expect(
      fetchMock.mock.calls.some(
        ([input, init]) =>
          String(input).includes("/day-status/status-1?version=1") && init?.method === "DELETE",
      ),
    ).toBe(false);
    await user.click(screen.getByRole("button", { name: "Remove day status" }));
    const remove = fetchMock.mock.calls.find(
      ([input, init]) =>
        String(input).includes("/day-status/status-1?version=1") && init?.method === "DELETE",
    );
    expect(remove).toBeTruthy();
  });

  it("shows an ordinary day's calendar class without a consequence note", async () => {
    const user = userEvent.setup();
    renderWorkspace();
    await openJulySixth(user);
    const totals = screen.getByLabelText("Server-calculated day totals");
    expect(totals.textContent).toContain("Calendar");
    expect(totals.textContent).toContain("Ordinary day");
    // Regression: the server's `WEEKDAY` class means "no calendar exception"
    // (the edition enumerates exceptions), so it is emitted for Saturdays and
    // Sundays too. Calling it a *working* day asserted something the calendar
    // does not say — the requirement comes from the schedule, shown separately.
    expect(totals.textContent).not.toContain("working day");
    expect(screen.queryByText(/carries no required time/)).not.toBeInTheDocument();
  });

  it("explains a non-working calendar class and the account credit in the day panel", async () => {
    const user = userEvent.setup();
    julyDailyResult = {
      ...julyDailyResult,
      calendar_class: "PUBLIC_HOLIDAY",
      required_minutes: 0,
      delta_minutes: 480,
    };
    renderWorkspace();
    await openJulySixth(user);
    const totals = screen.getByLabelText("Server-calculated day totals");
    expect(totals.textContent).toContain("Public holiday");
    expect(totals.textContent).toContain("Credited to your balance");
    expect(totals.textContent).toContain("8:00");
    expect(
      screen.getByText(
        "A public holiday carries no required time; your recorded work still counts as work.",
      ),
    ).toBeInTheDocument();
  });

  it("names a non-working calendar class in the day cell, not by colour alone", async () => {
    julyDailyResult = { ...julyDailyResult, calendar_class: "PUBLIC_HOLIDAY", required_minutes: 0 };
    renderWorkspace();
    // The classification is SPOKEN in full ("public holiday"), because the cell's
    // only visible non-colour cue for it is the compact `H` marker, which is
    // aria-hidden. A screen reader must hear which kind of non-working day it is.
    const cell = await screen.findByRole("button", { name: /public holiday/ });
    expect(cell.getAttribute("data-nonworking")).toBe("PUBLIC_HOLIDAY");
    expect(within(cell).getByText("H")).toBeInTheDocument();
    // The marker is decorative: the words above are the real statement.
    expect(within(cell).getByText("H")).toHaveAttribute("aria-hidden", "true");
    // An employer-free day is a DIFFERENT fact and says so, even though it shares
    // the red family in the grid (INV/domain distinction preserved).
    cleanup();
    julyDailyResult = { ...julyDailyResult, calendar_class: "EMPLOYER_FREE" };
    renderWorkspace();
    const free = await screen.findByRole("button", { name: /employer-free day/ });
    expect(free.getAttribute("data-nonworking")).toBe("EMPLOYER_FREE");
    expect(within(free).getByText("X")).toBeInTheDocument();
  });

  it("marks weekends by their own weekday and never calls one an ordinary day", async () => {
    renderWorkspace();
    // 2026-07-05 is a Sunday and the server reports `WEEKDAY` for it — the
    // classification enumerates EXCEPTIONS, so the weekend has to come from the
    // date. The cell must not read as an ordinary day.
    const sunday = await screen.findByRole("button", { name: /^Sunday, 5 July 2026/ });
    expect(sunday.getAttribute("data-nonworking")).toBe("WEEKEND");
    expect(sunday.getAttribute("aria-label")).toContain("weekend");
    expect(sunday.getAttribute("aria-label")).not.toContain("ordinary day");
    // No tint for a weekend: the date number carries it, and a wash would put
    // weekends and holidays on the same footing.
    expect(sunday.querySelector(".cell-nonworking-mark")).toBeNull();
    // A midweek day is ordinary.
    const monday = await screen.findByRole("button", { name: /^Monday, 6 July 2026/ });
    expect(monday.getAttribute("data-nonworking")).toBeNull();
    expect(monday.getAttribute("aria-label")).toContain("ordinary day");
  });

  it("makes no credit-rule claim on a paid-overtime day while the rule is inactive", async () => {
    const user = userEvent.setup();
    daySixIntervals = [
      ...fourIntervals,
      {
        id: "paid-overtime",
        start_at: "2026-07-06T18:00:00+02:00",
        end_at: "2026-07-06T19:00:00+02:00",
        start_local: "2026-07-06T18:00:00",
        end_local: "2026-07-06T19:00:00",
        work_type: "OVERTIME_PAID",
        elapsed_seconds: 3600,
        comment: null,
        version: 1,
      },
    ];
    renderWorkspace();
    await openJulySixth(user);
    // The SHIPPED edition credits every work type, so a claim that paid overtime
    // does not credit the account would contradict the number rendered next to
    // it. The row shows the server's figure and asserts no rule.
    expect(await screen.findByText("Credited to your balance")).toBeInTheDocument();
    expect(
      screen.queryByText(/does not credit your time account|compensated by salary/),
    ).not.toBeInTheDocument();
  });

  /**
   * Owner directive 2026-09-26: the year-scoped compensation statistics block was
   * removed from the Month page. It carried substantial visual weight at the bottom
   * of the primary workspace for figures the employee does not act on there, and it
   * was NOT replaced by another summary widget.
   *
   * The figures themselves are untouched: the server still computes
   * `compensation_metrics` and still sends it on the day-status payload, so a more
   * appropriate surface (Review / Compensation / Overtime) can present them later
   * with no backend change. This pins the FRONTEND removal only, so a future
   * reintroduction has to be a deliberate decision rather than an accident.
   */
  it("no longer renders the year's compensation-day statistics on the Month page", async () => {
    renderWorkspace();
    await openJulySixth(userEvent.setup());
    expect(screen.queryByLabelText("Compensation days recorded")).toBeNull();
    expect(screen.queryByText(/Compensation days recorded in/)).toBeNull();
    // None of the individual figures survives in any other form on this surface —
    // "do not replace it with another summary widget".
    expect(screen.queryByText("Flex-time, full days")).toBeNull();
    expect(screen.queryByText("Flex-time, half days")).toBeNull();
    expect(screen.queryByText("Longest full-day run")).toBeNull();
    expect(screen.queryByText("Shift-compensation days")).toBeNull();
    expect(screen.queryByText(/Guidance: 20 days per year/)).toBeNull();
    // The payload still carries them, which is what keeps the figures recoverable
    // on whichever surface later presents them.
    expect(
      (dayStatusResponse as { compensation_metrics?: unknown }).compensation_metrics,
    ).toBeTruthy();
  });

  it("keeps a month-level warning calm and reserves the alarm for a critical one", async () => {
    // Overtime and corridor deviations are ordinary here, so a permanent
    // month-level WARNING badge would just train people to ignore the header.
    findingsResponse = [
      ...findingsResponse,
      {
        rule_id: "WT-FLEX-003",
        severity: "WARNING",
        date: null,
        message: "Balance exceeds 50% of contractual weekly hours.",
      },
    ];
    renderWorkspace();
    const header = (await screen.findByRole("heading", { name: /^Month/ })).closest("h2");
    // "1 thing to check" read as the MONTH's total while it counts only the
    // findings that belong to the month rather than to a day — and a day beside it
    // could legitimately show two. The badge now names its scope.
    expect(
      await within(header as HTMLElement).findByText("1 month-level finding"),
    ).toBeInTheDocument();
    expect(within(header as HTMLElement).queryByText(/WARNING/)).not.toBeInTheDocument();
  });

  it("alarms on a month-level critical finding", async () => {
    findingsResponse = [
      ...findingsResponse,
      {
        rule_id: "WT-FLEX-004",
        severity: "CRITICAL",
        date: null,
        message: "Balance exceeds 100% of contractual weekly hours.",
      },
    ];
    renderWorkspace();
    const header = (await screen.findByRole("heading", { name: /^Month/ })).closest("h2");
    expect(await within(header as HTMLElement).findByText("⚠ CRITICAL")).toBeInTheDocument();
    expect(within(header as HTMLElement).queryByText(/finding/)).not.toBeInTheDocument();
  });

  it("gives a day's findings their worst severity, in the pin and in the cell", async () => {
    const user = userEvent.setup();
    // The WARNING is LAST in the server's list, and the pin used to take the
    // last item's severity while the cell took the first: this ordering is the
    // discriminator (acceptance C2/L4).
    findingsResponse = [
      ...findingsResponse,
      {
        rule_id: "WT-CORE-004",
        severity: "CRITICAL",
        date: "2026-07-06",
        message: "Rest period too short.",
      },
      {
        rule_id: "WT-CORE-005",
        severity: "CRITICAL",
        date: "2026-07-06",
        message: "Daily maximum exceeded.",
      },
      {
        rule_id: "WT-CORE-006",
        severity: "CRITICAL",
        date: "2026-07-06",
        message: "Weekly maximum exceeded.",
      },
      {
        rule_id: "WT-CORE-007",
        severity: "WARNING",
        date: "2026-07-06",
        message: "Corridor deviation.",
      },
    ];
    renderWorkspace();
    const cell = await screen.findByRole("button", { name: /^Monday, 6 July 2026/ });
    // The cell states the loudest severity, and its accessible name carries it.
    expect(within(cell).getByText("CRITICAL")).toBeInTheDocument();
    expect(within(cell).queryByText("WARNING")).not.toBeInTheDocument();
    expect(cell).toHaveAccessibleName(/CRITICAL finding/);

    await openJulySixth(user);
    const pin = document.querySelector(".finding-pin summary");
    expect(pin).toHaveTextContent("5 things to check");
    expect(pin).toHaveClass("severity-critical");
  });

  it("names a month with no installed working-time rules instead of a load failure", async () => {
    // A policy-edition boundary is a PERMANENT fact about the month, not a
    // transient fault — "could not load / try again" was a lie. The employee
    // must be told which boundary they hit and where the month actually lives.
    monthError = { status: 409, code: "POLICY_BLOCKED" };
    renderWorkspace();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/No working-time rules are installed for/i);
    expect(alert).toHaveTextContent(/July 2026/);
    expect(alert).toHaveTextContent(/stay in the Excel archive/i);
    expect(alert).not.toHaveTextContent(/Could not load/i);
  });

  it("names a missing holiday calendar instead of a load failure", async () => {
    monthError = { status: 409, code: "CALENDAR_BLOCKED" };
    renderWorkspace();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/No public-holiday calendar edition covers July 2026/i);
    expect(alert).not.toHaveTextContent(/Could not load/i);
  });

  it("keeps the operational-error sentence for a real load failure", async () => {
    // The boundary copy must not swallow genuine faults: a 500 still says the
    // workspace could not be loaded and that saved entries are unchanged.
    // Retries are off here because this asserts the RENDERING branch, not the
    // retry policy (a 5xx is deliberately retried; that is covered in
    // api.test.ts).
    monthError = { status: 500, code: "INTERNAL_ERROR" };
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <MonthWorkspace month="2026-07" />
      </QueryClientProvider>,
    );
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Could not load your private workspace/i);
    expect(alert).toHaveTextContent(/saved entries are unchanged/i);
  });
  it("shows an OCD call's own span and travel on the interval it created", async () => {
    // An OCD call widens its work interval by the required travel (OCD-006), so
    // the row alone cannot explain its extra minutes: the call's own span and
    // legs must be visible where the interval is (acceptance G3). The link is
    // already persisted and already exposed by the API.
    const user = userEvent.setup();
    daySixIntervals = [
      {
        id: "ocd-interval",
        start_at: "2026-07-06T01:30:00+02:00",
        end_at: "2026-07-06T05:30:00+02:00",
        start_local: "2026-07-06T01:30:00",
        end_local: "2026-07-06T05:30:00",
        work_type: "OCD_CALL",
        elapsed_seconds: 4 * 3600,
        comment: null,
        version: 1,
      },
      {
        id: "unlinked-ocd-interval",
        start_at: "2026-07-06T07:00:00+02:00",
        end_at: "2026-07-06T08:00:00+02:00",
        start_local: "2026-07-06T07:00:00",
        end_local: "2026-07-06T08:00:00",
        work_type: "OCD_CALL",
        elapsed_seconds: 3600,
        comment: null,
        version: 1,
      },
    ];
    const base = fetchMock.getMockImplementation() as (
      input: RequestInfo | URL,
      init?: RequestInit,
    ) => Promise<Response>;
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/me/ocd?"))
        return new Response(
          JSON.stringify([
            {
              id: "assignment-1",
              start_at: "2026-07-06T00:00:00+02:00",
              end_at: "2026-07-06T09:00:00+02:00",
              status: "SCHEDULED",
              scheduled_at: null,
              source: "TEST",
              findings: [],
              calls: [
                {
                  id: "call-1",
                  mode: "ON_SITE",
                  call_start_at: "2026-07-06T02:00:00+02:00",
                  call_end_at: "2026-07-06T05:00:00+02:00",
                  travel_out_minutes: 30,
                  travel_back_minutes: 30,
                  comment: null,
                  status: "RECORDED",
                  version: 1,
                  linked_interval_id: "ocd-interval",
                  span_start_at: "2026-07-06T01:30:00+02:00",
                  span_end_at: "2026-07-06T05:30:00+02:00",
                  elapsed_seconds: 10800,
                },
              ],
            },
          ]),
          { status: 200 },
        );
      return base(input, init);
    });

    renderWorkspace();
    await user.click(await screen.findByRole("button", { name: /^Monday, 6 July 2026/ }));

    // The linked interval states the call's own span and its travel legs.
    expect(await screen.findByText("call 02:00–05:00 · travel 30 + 30 min")).toBeInTheDocument();
    // A call with no link renders the row unchanged — no provenance, no crash.
    const unlinked = document.querySelector('[aria-label="Edit interval 07:00–08:00"]');
    expect(unlinked).not.toBeNull();
    expect(unlinked!.textContent).not.toContain("call ");
  });
  // ---------------------------------------------------------------------------
  // Planned shifts (plan §6.1 D7, §6.1 capability gate)
  // ---------------------------------------------------------------------------

  /** A shift assignment in the shape `GET /me/shifts` returns. */
  function julyShift(over: Record<string, unknown> = {}) {
    return {
      id: "shift-1",
      shift_kind: "NIGHT",
      status: "PLANNED",
      plan_source: "EMPLOYEE_RECORDED",
      approved_revision: {
        id: "revision-1",
        revision_no: 1,
        // 22:00 Berlin on 6 July — a night shift, the ordinary case here.
        planned_start_at: "2026-07-06T20:00:00Z",
        planned_end_at: "2026-07-07T04:00:00Z",
        communicated_at: null,
      },
      actual_start_at: null,
      actual_end_at: null,
      deviation: null,
      findings: [],
      paid_break_status: "EXPLICIT_FACT_REQUIRED",
      paid_breaks: [],
      ...over,
    };
  }

  /** The 7 July plan, used to prove the marker EXCLUDES a non-active status. */
  function withdrawnJulySeventh() {
    return julyShift({
      id: "shift-2",
      status: "WITHDRAWN",
      approved_revision: {
        id: "revision-2",
        revision_no: 1,
        planned_start_at: "2026-07-07T06:00:00Z",
        planned_end_at: "2026-07-07T14:00:00Z",
        communicated_at: null,
      },
    });
  }

  it("marks a planned shift on EVERY day it occupies, and names it in the accessible name", async () => {
    shiftsResponse = [julyShift(), withdrawnJulySeventh()];
    renderWorkspace();

    // The fixture runs 22:00 on the 6th -> 06:00 on the 7th, so the plan genuinely
    // OCCUPIES two calendar days and both cells must show it (INV-02: a
    // cross-midnight interval is an ordinary pair of timestamps, not an edge case).
    // Anchoring the marker to the start date alone left the 7th showing nothing.
    const sixth = await screen.findByRole("button", { name: /^Monday, 6 July 2026/ });
    const opening = sixth.querySelector(".cal-bar") as HTMLElement;
    expect(opening.getAttribute("data-cal-kind")).toBe("SHIFT");
    expect(opening.getAttribute("data-cal-state")).toBe("active");
    // The category is carried by a WORD as well as by the hue.
    expect(opening.querySelector(".cal-bar-label")?.textContent).toBe("Shift");
    // The start time is the trailing detail, which a narrow cell may drop.
    expect(opening.querySelector(".cal-bar-detail")?.textContent).toBe("22:00");

    const seventh = await screen.findByRole("button", { name: /^Tuesday, 7 July 2026/ });
    const continuing = seventh.querySelector(".cal-bar") as HTMLElement;
    expect(continuing.getAttribute("data-cal-kind")).toBe("SHIFT");
    // The second day must NOT repeat the start time: "Shift 22:00" on the 7th would
    // claim a shift beginning at 22:00 on the 7th. The compact "cont." says what is
    // true of that day, and the full sentence is in the accessible name below.
    expect(continuing.querySelector(".cal-bar-detail")?.textContent).toBe("cont.");

    // The bars are aria-hidden, so the cell's accessible name is the ONLY place a
    // screen reader can reach the plan — and it has to say the continuation too.
    expect(sixth).toHaveAccessibleName(/planned night shift/);
    expect(seventh).toHaveAccessibleName(/continues from the previous day/);

    // A WITHDRAWN assignment is retained lineage, never a plan the employee still
    // holds — it must not add a marker of its own (§4.7). The 7th therefore carries
    // exactly the one CONTINUING bar, not two.
    expect(seventh.querySelectorAll(".cal-bar")).toHaveLength(1);
    expect(seventh).toHaveAccessibleName(/planned night shift/);
  });

  it("shows no plan marker at all when no plan is active", async () => {
    shiftsResponse = [withdrawnJulySeventh()];
    renderWorkspace();
    await screen.findByRole("heading", { name: "Month" });
    expect(document.querySelectorAll(".cal-bar")).toHaveLength(0);
  });

  it("marks a cancelled shift as retained history, never as a live plan", async () => {
    shiftsResponse = [julyShift({ status: "CANCELLED" })];
    renderWorkspace();
    const sixth = await screen.findByRole("button", { name: /^Monday, 6 July 2026/ });
    const bar = sixth.querySelector(".cal-bar") as HTMLElement;
    expect(bar.getAttribute("data-cal-state")).toBe("cancelled");
    // The state is in the WORD, so the bar cannot be read as a plan the employee
    // still holds; colour is not carrying it.
    expect(bar.querySelector(".cal-bar-label")?.textContent).toBe("Shift cancelled");
    expect(sixth).toHaveAccessibleName(/cancelled night shift/);
    expect(sixth.getAttribute("aria-label")).not.toMatch(/planned night shift/);
  });

  it("keeps an abortion by the Company distinct from a cancellation", async () => {
    // `ABORTED_BY_COMPANY` is a DEVIATION type, not a Shift status, so the row is
    // still `PLANNED` by status while the shift did not happen as planned. It must
    // not read as an active plan, and it must not be reported as a cancellation —
    // that would misstate what the Company did.
    shiftsResponse = [
      julyShift({
        deviation: {
          id: "deviation-1",
          deviation_type: "ABORTED_BY_COMPANY",
          actual_start_at: "2026-07-06T20:00:00Z",
          actual_end_at: "2026-07-06T22:00:00Z",
          noticed_or_decided_at: null,
          decision_maker_type: null,
          reason: null,
          version: 1,
        },
      }),
    ];
    renderWorkspace();
    const sixth = await screen.findByRole("button", { name: /^Monday, 6 July 2026/ });
    const bar = sixth.querySelector(".cal-bar") as HTMLElement;
    expect(bar.getAttribute("data-cal-state")).toBe("cancelled");
    expect(bar.querySelector(".cal-bar-label")?.textContent).toBe("Shift aborted");
    expect(sixth).toHaveAccessibleName(/aborted by the Company/);
    expect(sixth.getAttribute("aria-label")).not.toMatch(/cancelled/);
  });

  it("caps a crowded day's bars and speaks the events the overflow chip hides", async () => {
    // A day with more events than a cell can carry: the overflow is a COUNT, and
    // the hidden events must still reach assistive technology — otherwise the chip
    // would be a silent truncation rather than an overflow treatment.
    shiftsResponse = [julyShift()];
    dayStatusResponse = {
      period: "2026-07",
      entries: [
        { ...vacationFull, day: "2026-07-06" },
        { ...vacationFull, id: "status-2", day: "2026-07-06", category: "SICK" },
      ],
    };
    renderWorkspace();
    const sixth = await screen.findByRole("button", { name: /^Monday, 6 July 2026/ });
    expect(sixth.querySelectorAll(".cal-bar")).toHaveLength(2);
    const more = sixth.querySelector(".cal-events-more") as HTMLElement;
    expect(more.textContent).toBe("+1 more");
    // Every event is still spoken, including the one with no visible bar.
    const spoken = sixth.getAttribute("aria-label") ?? "";
    expect(spoken).toMatch(/planned night shift/);
    expect(spoken).toMatch(/Vacation \(full day\)/);
    expect(spoken).toMatch(/Sick leave \(full day\)/);
  });

  it("offers the planned-shift form only with SHIFT_PLAN_ENTRY_WRITE, and posts the plan", async () => {
    const user = userEvent.setup();
    renderWorkspace();
    await screen.findByRole("heading", { name: "Month" });
    // Without the capability the server refuses every plan write with
    // POLICY_BLOCKED, so the honest UI offers no control at all rather than a form
    // that can only fail (§6.1). An absent capability is read as "not granted".
    expect(screen.queryByRole("button", { name: "+ Add planned shift" })).toBeNull();
    cleanup();

    identityCapabilities = ["SHIFT_PLAN_ENTRY_WRITE"];
    renderWorkspace();
    await user.click(await screen.findByRole("button", { name: "+ Add planned shift" }));

    // The form is a real fieldset/legend group with a real label on every
    // control, so it is reachable and named without any ARIA stand-in.
    const form = screen.getByRole("group", { name: "Add planned shift" });
    await user.type(within(form).getByLabelText("Planned start"), "06.07.2026 22:00");
    await user.type(within(form).getByLabelText("Planned end"), "07.07.2026 06:00");
    await user.selectOptions(within(form).getByLabelText("Shift kind"), "NIGHT");
    await user.click(within(form).getByRole("button", { name: "Add" }));

    const call = await waitFor(() => {
      const found = fetchMock.mock.calls.find(
        ([input, init]) => String(input) === "/api/v1/me/shifts" && init?.method === "POST",
      );
      if (!found) throw new Error("no planned shift was posted");
      return found;
    });
    expect(JSON.parse(String(call[1]?.body))).toMatchObject({
      planned_start_local: "2026-07-06T22:00",
      planned_end_local: "2026-07-07T06:00",
      shift_kind: "NIGHT",
    });
    expect(await screen.findByText("Planned shift recorded.")).toBeInTheDocument();
  });

  it("states what a disabled plan submit is waiting for", async () => {
    const user = userEvent.setup();
    identityCapabilities = ["SHIFT_PLAN_ENTRY_WRITE"];
    renderWorkspace();
    await user.click(await screen.findByRole("button", { name: "+ Add planned shift" }));
    const form = screen.getByRole("group", { name: "Add planned shift" });
    // A disabled primary must say what it is waiting for (UI-012), and the
    // requirement is attached to the control itself (aria-describedby), so a
    // screen-reader user hears it as part of the button.
    const submit = within(form).getByRole("button", { name: "Add" });
    expect(submit).toBeDisabled();
    expect(submit).toHaveAttribute("aria-describedby", "new-plan-requirement");
    expect(within(form).getByRole("status")).toHaveTextContent(
      "Enter the planned start and end as DD.MM.YYYY HH:MM",
    );
  });
});
