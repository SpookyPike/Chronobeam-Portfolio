import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  createSegment,
  createTrip,
  fetchTravelSummary,
  fetchTravelTrips,
  updateSegment,
} from "../lib/api";
import { hhmm, monthKey, TravelPage } from "./travel";

vi.mock("@tanstack/react-router", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@tanstack/react-router")>()),
  Link: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    fetchTravelTrips: vi.fn(),
    fetchTravelSummary: vi.fn(),
    createTrip: vi.fn(),
    createSegment: vi.fn(),
    updateSegment: vi.fn(),
    deleteSegment: vi.fn(),
    deleteTrip: vi.fn(),
    updateTrip: vi.fn(),
  };
});

const trips = [
  {
    id: "trip-1",
    start_at: "2026-05-15T06:00:00Z",
    end_at: "2026-05-17T14:00:00Z",
    start_local: "2026-05-15T08:00:00",
    end_local: "2026-05-17T16:00:00",
    start_origin: "WORKPLACE" as const,
    source: "EMPLOYEE_DECLARED",
    reference_label: "BESSY II beamtime",
    version: 1,
    supersedes_id: null,
    client_request_id: null,
    segments: [
      {
        id: "seg-drive",
        trip_id: "trip-1",
        start_at: "2026-05-15T06:00:00Z",
        end_at: "2026-05-15T07:30:00Z",
        start_local: "2026-05-15T08:00:00",
        end_local: "2026-05-15T09:30:00",
        classification: "DRIVING" as const,
        basis: "ACTUAL" as const,
        work_interval_id: "interval-1",
        version: 1,
        supersedes_id: null,
        client_request_id: null,
      },
      {
        id: "seg-passive",
        trip_id: "trip-1",
        start_at: "2026-05-15T07:30:00Z",
        end_at: "2026-05-15T11:00:00Z",
        start_local: "2026-05-15T09:30:00",
        end_local: "2026-05-15T13:00:00",
        classification: "PASSIVE_TRAVEL" as const,
        basis: "ACTUAL" as const,
        work_interval_id: null,
        version: 1,
        supersedes_id: null,
        client_request_id: null,
      },
    ],
  },
];

const summary = {
  period_month: "2026-05",
  topups: [{ day: "2026-05-15", minutes: 207 }],
  // TRAVEL-004: the May-15 trip window ends Jun-15, so the 25% credit is
  // effective on the first day after the window = Jul-1. The May summary is
  // period-correct and lists NO credits; the July render supplies one.
  credits: [],
  trips: [
    {
      trip_id: "trip-1",
      window_start: "2026-05-15",
      window_end: "2026-06-15",
      eligible_minutes: 960,
      threshold_minutes: 900,
      excess_minutes: 60,
      credit_minutes: 15,
      credit_effective_on: "2026-07-01",
    },
  ],
  findings: [
    {
      rule_id: "TRAVEL-SUNDAY-001",
      severity: "WARNING" as const,
      date: "2026-05-17",
      message: "Sunday travel work requires a prior overtime application.",
    },
  ],
  input_fingerprint: "fingerprint-token",
};

const julyCredit = {
  occurred_on: "2026-07-01",
  minutes: 15,
  rule_id: "TRAVEL-004",
  source_entity_type: "business_trip",
  source_entity_id: "trip-1",
  window_start: "2026-05-15",
  window_end: "2026-06-15",
  eligible_minutes: 960,
  threshold_minutes: 900,
  excess_minutes: 60,
};

function renderPage(period = "2026-05") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={queryClient}>
      <TravelPage period={period} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(fetchTravelTrips).mockResolvedValue(structuredClone(trips) as never);
  vi.mocked(fetchTravelSummary).mockResolvedValue(structuredClone(summary) as never);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("travel page", () => {
  it("renders one empty-state sentence plus the add action, never two", async () => {
    // 7B: /travel used to say the same thing twice — "No trips recorded this
    // month." and "No business trips recorded yet." The screen read as unfinished
    // and the reader had to work out whether the two lines differed.
    vi.mocked(fetchTravelTrips).mockResolvedValue([] as never);
    vi.mocked(fetchTravelSummary).mockResolvedValue({
      ...structuredClone(summary),
      topups: [],
      credits: [],
      trips: [],
      findings: [],
    } as never);
    renderPage();
    expect(await screen.findByText(/No trips recorded this month/)).toBeTruthy();
    expect(screen.queryByText(/No business trips recorded yet/)).toBeNull();
    expect(screen.getByRole("button", { name: "Add business trip" })).toBeTruthy();
  });

  // The trips endpoint takes no range and returns every trip ever recorded, so
  // the month-scoped summary decides which of them this month shows. Without it
  // a trip from another month sat under this month's heading.
  it("lists only the trips the selected month's summary reports", async () => {
    vi.mocked(fetchTravelSummary).mockResolvedValue({
      ...structuredClone(summary),
      trips: [],
      topups: [],
      credits: [],
      findings: [],
    } as never);
    renderPage();
    expect(await screen.findByText(/No trips recorded this month/)).toBeTruthy();
    expect(screen.queryByText(/Business trip · Fri, 15 May/)).toBeNull();
    expect(screen.getByRole("button", { name: "Add business trip" })).toBeTruthy();
  });
  it("shows trips with classified segments and honest labels", async () => {
    renderPage();
    expect(await screen.findByText(/Business trip · Fri, 15 May/)).toBeTruthy();
    expect(screen.getByText("Driving")).toBeTruthy();
    expect(screen.getAllByText("Passive travel").length).toBeGreaterThan(0);
    expect(screen.getByText("working time in your ledger")).toBeTruthy();
    expect(screen.getByText("travel time, not working time")).toBeTruthy();
    expect(screen.getByText(/08:00–09:30/)).toBeTruthy();
    expect(screen.getByText("employee-declared")).toBeTruthy();
    expect(screen.getByText(/starts at the workplace/)).toBeTruthy();
  });

  it("shows the excess travel and the generated 25% credit as labelled figures", async () => {
    renderPage();
    expect(await screen.findByText("Eligible non-credited travel")).toBeTruthy();
    expect(screen.getByText("Threshold")).toBeTruthy();
    expect(screen.getByText("Excess")).toBeTruthy();
    expect(screen.getByText("25% time credit")).toBeTruthy();
    expect(screen.getByText("0:15")).toBeTruthy();
    expect(screen.getByText("Credit added to balance")).toBeTruthy();
    // The running line restated these same three numbers, so it is gone.
    expect(screen.queryByText(/over the threshold/)).toBeNull();
  });

  it("shows the running threshold line only while the window is still short of it", async () => {
    vi.mocked(fetchTravelSummary).mockResolvedValue({
      ...structuredClone(summary),
      trips: [
        {
          ...structuredClone(summary.trips[0]),
          eligible_minutes: 600,
          excess_minutes: 0,
          credit_minutes: 0,
        },
      ],
    } as never);
    renderPage();
    expect(await screen.findByText(/10:00 \/ 15:00 threshold/)).toBeTruthy();
    expect(screen.getByText(/5:00 remaining/)).toBeTruthy();
    // Nothing is over the threshold, so there is no credit breakdown to show.
    expect(screen.queryByText("Eligible non-credited travel")).toBeNull();
  });

  it("lists travel findings and top-ups without exposing raw minutes", async () => {
    renderPage();
    expect(await screen.findByText(/Sunday travel work requires/)).toBeTruthy();
    // The day and its credit are separate fields, not one sentence.
    const topups = screen.getByLabelText("Travel day top-ups this month");
    expect(within(topups).getByText("Fri, 15 May")).toBeTruthy();
    expect(within(topups).getByText("+3:27 credited")).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/\b960\b|\b900\b|\b207\b/);
  });

  it("creates a trip with employee-declared provenance", async () => {
    vi.mocked(createTrip).mockResolvedValue(structuredClone(trips[0]) as never);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Add business trip/i }));
    fireEvent.change(screen.getByLabelText("Trip start date"), { target: { value: "2026-06-01" } });
    fireEvent.change(screen.getByLabelText("Trip start time"), { target: { value: "07:30" } });
    fireEvent.change(screen.getByLabelText("Trip end date"), { target: { value: "2026-06-03" } });
    fireEvent.change(screen.getByLabelText("Trip end time"), { target: { value: "18:00" } });
    fireEvent.change(screen.getByLabelText("Trip start origin"), { target: { value: "HOME" } });
    fireEvent.click(screen.getByRole("button", { name: "Create trip" }));
    await waitFor(() => expect(createTrip).toHaveBeenCalledTimes(1));
    const payload = vi.mocked(createTrip).mock.calls[0][0];
    expect(payload.start_local).toContain("2026-06-01T07:30");
    expect(payload.end_local).toContain("2026-06-03T18:00");
    expect(payload.start_origin).toBe("HOME");
    expect(payload.client_request_id).toBeTruthy();
  });

  it("blocks submitting a trip whose end precedes its start", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Add business trip/i }));
    fireEvent.change(screen.getByLabelText("Trip start date"), { target: { value: "2026-06-05" } });
    fireEvent.change(screen.getByLabelText("Trip end date"), { target: { value: "2026-06-01" } });
    expect(screen.getByRole("button", { name: "Create trip" })).toBeDisabled();
    expect(screen.getByText(/end must be after its start/)).toBeTruthy();
  });

  it("requires a reason for segment corrections", async () => {
    vi.mocked(updateSegment).mockResolvedValue(structuredClone(trips[0].segments[0]) as never);
    renderPage();
    await screen.findByText(/Business trip/);
    fireEvent.click(screen.getAllByRole("button", { name: /^Correct$/i })[0]);
    const save = screen.getByRole("button", { name: /Save correction/i });
    expect(save).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: "fixed leg end" } });
    expect(save).not.toBeDisabled();
    fireEvent.click(save);
    await waitFor(() => expect(updateSegment).toHaveBeenCalledTimes(1));
    expect(vi.mocked(updateSegment).mock.calls[0][1].reason).toBe("fixed leg end");
    expect(vi.mocked(updateSegment).mock.calls[0][1].version).toBe(1);
  });

  it("associates the trip requirement with the primary that cannot proceed", async () => {
    vi.mocked(createTrip).mockResolvedValue(structuredClone(trips[0]) as never);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Add business trip/i }));
    const create = screen.getByRole("button", { name: "Create trip" });
    const requirement = screen.getByText(/Enter the trip's start and end date and time/);
    expect(create).toBeDisabled();
    expect(create).toHaveAttribute("aria-describedby", requirement.id);
    expect(screen.getByLabelText("Trip start date")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("Trip end date")).toHaveAttribute("aria-invalid", "true");

    // Once the values are conforming the requirement disappears, the association
    // goes with it, and the request is issued.
    fireEvent.change(screen.getByLabelText("Trip start date"), { target: { value: "2026-06-01" } });
    fireEvent.change(screen.getByLabelText("Trip end date"), { target: { value: "2026-06-03" } });
    expect(screen.queryByText(/Enter the trip's start and end/)).toBeNull();
    expect(create).not.toBeDisabled();
    expect(create).not.toHaveAttribute("aria-describedby");
    fireEvent.click(create);
    await waitFor(() => expect(createTrip).toHaveBeenCalledTimes(1));
  });

  /**
   * `TimeField`'s native `pattern` is `([01][0-9]|2[0-3]):[0-5][0-9]`. A
   * shape-only completeness test accepted `29:00`, so the browser refused the
   * submit with no request and no message and the primary stayed enabled — the
   * exact defect the stated requirement exists to remove (review R2b).
   */
  it("states the time's own range, so an out-of-range clock cannot silently block Create", async () => {
    vi.mocked(createTrip).mockResolvedValue(structuredClone(trips[0]) as never);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Add business trip/i }));
    const create = screen.getByRole("button", { name: "Create trip" });

    fireEvent.change(screen.getByLabelText("Trip start date"), { target: { value: "2026-06-01" } });
    fireEvent.change(screen.getByLabelText("Trip end date"), { target: { value: "2026-06-03" } });
    expect(create).not.toBeDisabled();

    fireEvent.change(screen.getByLabelText("Trip start time"), { target: { value: "29:00" } });
    const requirement = screen.getByText(/Enter the trip's start and end date and time/);
    expect(create).toBeDisabled();
    expect(create).toHaveAttribute("aria-describedby", requirement.id);
    fireEvent.click(create);
    expect(createTrip).not.toHaveBeenCalled();

    // A conforming clock restores the action.
    fireEvent.change(screen.getByLabelText("Trip start time"), { target: { value: "08:30" } });
    expect(screen.queryByText(/Enter the trip's start and end/)).toBeNull();
    expect(create).not.toBeDisabled();
    fireEvent.click(create);
    await waitFor(() => expect(createTrip).toHaveBeenCalledTimes(1));
  });

  it("states the segment requirement at the primary, then issues the request", async () => {
    vi.mocked(createSegment).mockResolvedValue(structuredClone(trips[0].segments[1]) as never);
    renderPage();
    await screen.findByText(/Business trip/);
    fireEvent.click(screen.getAllByRole("button", { name: /Add segment/i })[0]);
    const add = screen.getByRole("button", { name: "Add segment" });
    // Prefilled from the trip, so the pair is already submittable…
    expect(add).not.toBeDisabled();
    expect(add).not.toHaveAttribute("aria-describedby");

    fireEvent.change(screen.getByLabelText("Segment end date"), {
      target: { value: "2026-05-01" },
    });
    // …and once it is not, the rule is stated and the control points at it.
    const requirement = screen.getByText(/The segment end must be after its start/);
    expect(add).toBeDisabled();
    expect(add).toHaveAttribute("aria-describedby", requirement.id);
    expect(screen.getByLabelText("Segment end date")).toHaveAttribute("aria-invalid", "true");

    fireEvent.change(screen.getByLabelText("Segment end date"), {
      target: { value: "2026-05-17" },
    });
    expect(add).not.toBeDisabled();
    fireEvent.click(add);
    await waitFor(() => expect(createSegment).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createSegment).mock.calls[0][1].end_local).toBe("2026-05-17T16:00:00");
  });

  it("states the correction requirement at its own primary", async () => {
    renderPage();
    await screen.findByText(/Business trip/);
    fireEvent.click(screen.getAllByRole("button", { name: /^Correct$/i })[0]);
    const save = screen.getByRole("button", { name: /Save correction/i });
    const requirement = screen.getByText(/Enter a reason for this correction/);
    expect(save).toBeDisabled();
    expect(save).toHaveAttribute("aria-describedby", requirement.id);
    expect(screen.getByLabelText(/Reason/)).toHaveAttribute("aria-invalid", "true");
  });

  it("accepts a same-day trip once the full local datetimes are compared", async () => {
    vi.mocked(createTrip).mockResolvedValue(structuredClone(trips[0]) as never);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Add business trip/i }));
    // Same start AND end date with end time after start time is legitimate
    // (F6a: the old date-only comparison rejected it).
    fireEvent.change(screen.getByLabelText("Trip start date"), { target: { value: "2026-10-25" } });
    fireEvent.change(screen.getByLabelText("Trip start time"), { target: { value: "06:00" } });
    fireEvent.change(screen.getByLabelText("Trip end date"), { target: { value: "2026-10-25" } });
    fireEvent.change(screen.getByLabelText("Trip end time"), { target: { value: "22:00" } });
    const create = screen.getByRole("button", { name: "Create trip" });
    expect(create).not.toBeDisabled();
    fireEvent.click(create);
    await waitFor(() => expect(createTrip).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createTrip).mock.calls[0][0].start_local).toBe("2026-10-25T06:00:00");
    expect(vi.mocked(createTrip).mock.calls[0][0].end_local).toBe("2026-10-25T22:00:00");
  });

  it("requires an occurrence choice for ambiguous DST fall-back boundaries", async () => {
    vi.mocked(createTrip).mockResolvedValue(structuredClone(trips[0]) as never);
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /Add business trip/i }));
    // 2026-10-25 02:30 exists twice in Europe/Berlin (fall-back).
    fireEvent.change(screen.getByLabelText("Trip start date"), { target: { value: "2026-10-25" } });
    fireEvent.change(screen.getByLabelText("Trip start time"), { target: { value: "02:30" } });
    fireEvent.change(screen.getByLabelText("Trip end date"), { target: { value: "2026-10-25" } });
    fireEvent.change(screen.getByLabelText("Trip end time"), { target: { value: "22:00" } });
    const create = screen.getByRole("button", { name: "Create trip" });
    const occurrence = screen.getByLabelText("Occurrence for trip start");
    // The server rejects ambiguous wall times without a fold — the form must
    // not submit until the employee chooses the occurrence (docs/ARCHITECTURE.md).
    expect(create).toBeDisabled();
    fireEvent.change(occurrence, { target: { value: "0" } });
    expect(create).not.toBeDisabled();
    fireEvent.click(create);
    await waitFor(() => expect(createTrip).toHaveBeenCalledTimes(1));
    const payload = vi.mocked(createTrip).mock.calls[0][0];
    expect(payload.start_local).toBe("2026-10-25T02:30:00");
    expect(payload.start_fold).toBe(0);
    expect(payload.end_fold).toBeUndefined();
  });

  it("fences the previous month's summary data from a new month heading", async () => {
    // A stale payload answering for a different period must not render under
    // the May heading (F7): no findings, top-ups, credit section, or window.
    vi.mocked(fetchTravelSummary).mockResolvedValue({
      ...structuredClone(summary),
      period_month: "2026-04",
    } as never);
    renderPage();
    // And while the summary answers for ANOTHER period it says nothing about
    // this month's membership, so no trip renders at all: falling back to every
    // lifetime trip put out-of-month trips under the May heading, where the
    // employee could act on them (review round 2, Major 2).
    expect(await screen.findByText(/This month's travel summary could not be read/)).toBeTruthy();
    expect(screen.queryByText(/Loading this month's trips/)).toBeNull();
    expect(screen.queryByText(/Business trip/)).toBeNull();
    expect(screen.queryByText("+3:27 credited")).toBeNull();
    expect(screen.queryByText(/Sunday travel work requires/)).toBeNull();
    expect(screen.queryByText(/threshold/)).toBeNull();
    expect(screen.queryByText(/Generated travel credit/)).toBeNull();
  });

  it("shows the derived travel credit only in its effective month", async () => {
    // May's summary must not list the July-1 credit…
    renderPage();
    await screen.findByText(/Business trip/);
    expect(screen.queryByText(/Generated travel credit/)).toBeNull();
    // …and July's summary shows exactly it.
    vi.mocked(fetchTravelSummary).mockResolvedValue({
      ...structuredClone(summary),
      period_month: "2026-07",
      topups: [],
      credits: [structuredClone(julyCredit)],
    } as never);
    renderPage("2026-07");
    expect(await screen.findByText("Generated travel credit · July 2026")).toBeTruthy();
    const credits = screen.getByLabelText("Generated travel credits");
    expect(within(credits).getByText("Wed, 1 July")).toBeTruthy();
    expect(within(credits).getByText("+0:15")).toBeTruthy();
  });

  it("explains actual work vs compensable travel when adding segments", async () => {
    vi.mocked(createSegment).mockResolvedValue(structuredClone(trips[0].segments[1]) as never);
    renderPage();
    await screen.findByText(/Business trip/);
    fireEvent.click(screen.getAllByRole("button", { name: /Add segment/i })[0]);
    const select = screen.getByLabelText("Segment classification");
    fireEvent.change(select, { target: { value: "ACTIVE_WORK" } });
    expect(screen.getByText(/actual work interval .*never double-counted/i)).toBeTruthy();
    fireEvent.change(select, { target: { value: "PASSIVE_TRAVEL" } });
    fireEvent.click(screen.getByLabelText(/Fictitious business route/i));
    expect(screen.getByLabelText(/Fictitious business route/i)).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Add segment" }));
    await waitFor(() => expect(createSegment).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createSegment).mock.calls[0][1].basis).toBe("FICTITIOUS_BUSINESS_ROUTE");
  });
});

describe("display helpers", () => {
  it("formats durations for humans in 24-hour style", () => {
    expect(hhmm(0)).toBe("0:00");
    expect(hhmm(960)).toBe("16:00");
    expect(hhmm(901)).toBe("15:01");
    expect(hhmm(-15)).toBe("−0:15");
  });

  it("derives calendar months TZ-safely from window dates", () => {
    expect(monthKey("2026-06-15")).toBe("2026-06");
    expect(monthKey("2026-12-31")).toBe("2026-12");
  });
});
