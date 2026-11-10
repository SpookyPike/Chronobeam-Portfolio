import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ProfilePage, formatDuration, parseDuration } from "./profile";

const EMPTY_PROFILE = {
  effective_at: "2026-09-11",
  employee: { external_employee_id: "emp-0001", legal_name: "Synthetic Employee", email: null },
  contract: { fte: 1, weekly_required_minutes: 1740, pay_group_bucket: "LE_9B" },
  schedule: null,
  preferences: {
    usual_work_type: "ON_SITE",
    preferred_calendar_view: "month",
    normal_day_template: null,
  },
};

const SEEDED_PROFILE = {
  ...EMPTY_PROFILE,
  schedule: {
    valid_from: "2026-01-01",
    valid_to: null,
    weekly_required_minutes: 1740,
    mon_required_minutes: 348,
    tue_required_minutes: 348,
    wed_required_minutes: 348,
    thu_required_minutes: 348,
    fri_required_minutes: 348,
    sat_required_minutes: 0,
    sun_required_minutes: 0,
    source: "ADMIN_SEED",
    version: 1,
  },
};

const SAVED_SCHEDULE = {
  valid_from: "2026-09-11",
  valid_to: null,
  weekly_required_minutes: 1740,
  mon_required_minutes: 348,
  tue_required_minutes: 348,
  wed_required_minutes: 348,
  thu_required_minutes: 348,
  fri_required_minutes: 348,
  sat_required_minutes: 0,
  sun_required_minutes: 0,
  source: "EMPLOYEE",
  version: 2,
};

let putPayload: Record<string, unknown> | null;
let profileForTest: Record<string, unknown> = EMPTY_PROFILE;
let workingTimeError: string | null;
let preferencesError: string | null;
let onboardingForTest: Record<string, unknown>;
let onboardingPayload: Record<string, unknown> | null;
let correctionPayload: Record<string, unknown> | null;
let onboardingError: { status: number; code: string; message: string } | null;
/** Advisory payloads keyed by `start_month`, and every month the UI ASKED for —
 *  the record is what proves a refused month sent no request at all. */
let onboardingByMonth: Record<string, Record<string, unknown>>;
let onboardingMonthsRequested: string[];

/** The state after onboarding. Default for the existing editor tests: the
 *  standalone editor is shown once the start state exists (§5.2). */
const COMPLETED_ONBOARDING = {
  state: "COMPLETED",
  month: "2026-09",
  default_start_month: "2026-09",
  opening: {
    cutover_month: "2026-09",
    minutes: -1104,
    seed_minutes: -1104,
    occurred_on: "2026-08-31",
    previous_period: "2026-08",
    confirmed_by: "employee",
    revision: 1,
    corrected_at: null,
  },
  blocked_reason: null,
  blocked_month: null,
  has_facts_in_month: false,
  correctable: true,
  in_use_reason: null,
  schedule: null,
};

/** Before onboarding: the form owns the working-time setup. */
const READY_ONBOARDING = {
  state: "READY",
  month: "2026-09",
  default_start_month: "2026-09",
  opening: null,
  blocked_reason: null,
  blocked_month: null,
  has_facts_in_month: false,
  correctable: false,
  in_use_reason: null,
  schedule: null,
};

/** Match an element whose FULL text content equals the given string (the
 *  totals line is split across <strong> children). */
function textContent(value: string) {
  return (_content: string, node: Element | null) => node?.textContent === value;
}

function renderProfile(client: QueryClient = new QueryClient()) {
  return render(
    <QueryClientProvider client={client}>
      <ProfilePage />
    </QueryClientProvider>,
  );
}

async function profileFetchMock(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = String(input);
  if (init?.method === "PUT" && url.endsWith("/api/v1/me/working-time")) {
    putPayload = JSON.parse(String(init.body));
    if (workingTimeError) {
      return new Response(
        JSON.stringify({ detail: { code: "VALIDATION_FAILED", message: workingTimeError } }),
        { status: 422, headers: { "Content-Type": "application/json" } },
      );
    }
    return new Response(JSON.stringify({ ...SAVED_SCHEDULE, ...putPayload }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }
  if (init?.method === "PATCH" && url.endsWith("/api/v1/me/preferences")) {
    if (preferencesError) {
      return new Response(
        JSON.stringify({ detail: { code: "VALIDATION_FAILED", message: preferencesError } }),
        { status: 422, headers: { "Content-Type": "application/json" } },
      );
    }
    return new Response(JSON.stringify(EMPTY_PROFILE.preferences), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }
  if (init?.method === "POST" && url.endsWith("/api/v1/me/onboarding")) {
    onboardingPayload = JSON.parse(String(init.body));
    if (onboardingError) {
      return new Response(
        JSON.stringify({
          detail: {
            code: onboardingError.code,
            message: onboardingError.message,
            details: { reason: "DUPLICATE_SEED" },
          },
        }),
        { status: onboardingError.status, headers: { "Content-Type": "application/json" } },
      );
    }
    // The refetch after the mutation must see the established state.
    onboardingForTest = COMPLETED_ONBOARDING;
    return new Response(JSON.stringify(COMPLETED_ONBOARDING), {
      status: 201,
      headers: { "Content-Type": "application/json" },
    });
  }
  if (init?.method === "PATCH" && url.endsWith("/api/v1/me/onboarding")) {
    correctionPayload = JSON.parse(String(init.body));
    onboardingForTest = {
      ...COMPLETED_ONBOARDING,
      opening: { ...COMPLETED_ONBOARDING.opening, minutes: -1440, revision: 2 },
    };
    return new Response(JSON.stringify(onboardingForTest), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }
  if (url.includes("/api/v1/me/onboarding")) {
    const requested = new URL(url, "http://localhost").searchParams.get("start_month");
    if (requested) onboardingMonthsRequested.push(requested);
    const payload = (requested && onboardingByMonth[requested]) || onboardingForTest;
    return new Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }
  return new Response(JSON.stringify(profileForTest), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("duration formatting", () => {
  it("parses human-readable durations without floats", () => {
    expect(parseDuration("6:00")).toBe(360);
    expect(parseDuration("7:48")).toBe(468);
    expect(parseDuration("29:15")).toBe(1755);
    expect(parseDuration("29")).toBe(1740);
    expect(parseDuration("0:00")).toBe(0);
    expect(parseDuration("")).toBe(0);
    expect(parseDuration("12:99")).toBeNull();
    expect(parseDuration("abc")).toBeNull();
  });

  it("formats durations with padded minutes and minus sign only when negative", () => {
    expect(formatDuration(1740)).toBe("29:00");
    expect(formatDuration(468)).toBe("7:48");
    expect(formatDuration(0)).toBe("0:00");
    expect(formatDuration(-300)).toBe("−5:00");
  });
});

describe("employee working-time editor", () => {
  beforeEach(() => {
    putPayload = null;
    profileForTest = EMPTY_PROFILE;
    workingTimeError = null;
    preferencesError = null;
    onboardingForTest = COMPLETED_ONBOARDING;
    onboardingPayload = null;
    correctionPayload = null;
    onboardingError = null;
    onboardingByMonth = {};
    onboardingMonthsRequested = [];
    vi.stubGlobal("fetch", vi.fn(profileFetchMock));
  });

  it("first-time setup: contract weekly prefilled, save blocked until the sum matches", async () => {
    const user = userEvent.setup();
    renderProfile();
    const target = await screen.findByLabelText("Weekly target");
    expect(target).toHaveValue("29:00");
    expect(screen.getByText("Allocated")).toBeInTheDocument();

    const save = screen.getByRole("button", { name: "Save working time" });
    expect(save).toBeDisabled();
    await user.type(screen.getByLabelText("Monday required time"), "6:00");
    await user.type(screen.getByLabelText("Tuesday required time"), "6:00");
    await user.type(screen.getByLabelText("Wednesday required time"), "6:00");
    await user.type(screen.getByLabelText("Thursday required time"), "6:00");
    await user.type(screen.getByLabelText("Friday required time"), "5:00");
    expect(screen.getByText(textContent("Weekly target 29:00"))).toBeInTheDocument();
    expect(screen.getByText(textContent("Allocated 29:00"))).toBeInTheDocument();
    expect(screen.getByText(textContent("Remaining 0:00"))).toBeInTheDocument();
    expect(save).toBeEnabled();
  });

  it("blocks save on under-allocation and shows remaining live", async () => {
    const user = userEvent.setup();
    renderProfile();
    await screen.findByLabelText("Weekly target");
    for (const day of ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]) {
      await user.type(screen.getByLabelText(`${day} required time`), "4:00");
    }
    expect(screen.getByText(textContent("Remaining 9:00"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save working time" })).toBeDisabled();
    expect(screen.getByText(/9:00 remaining — allocate exactly/)).toBeInTheDocument();
  });

  it("use remaining fills a day and unblocks save", async () => {
    const user = userEvent.setup();
    renderProfile();
    await screen.findByLabelText("Weekly target");
    for (const day of ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]) {
      await user.type(screen.getByLabelText(`${day} required time`), "4:00");
    }
    expect(screen.getByText(textContent("Remaining 9:00"))).toBeInTheDocument();
    await user.click(screen.getByLabelText("Saturday working day"));
    const saturdayInput = screen.getByLabelText("Saturday required time");
    const saturdayCell = saturdayInput.closest(".weekday-cell") as HTMLElement;
    await user.click(
      within(saturdayCell).getByRole("button", { name: "Use remaining for Saturday" }),
    );
    expect(saturdayInput).toHaveValue("9:00");
    expect(screen.getByText(textContent("Remaining 0:00"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save working time" })).toBeEnabled();
  });

  it("distribute evenly across the picked working days with minute remainder", async () => {
    const user = userEvent.setup();
    renderProfile();
    await screen.findByLabelText("Weekly target");
    await user.click(screen.getByLabelText("Saturday working day"));
    await user.click(screen.getByLabelText("Sunday working day"));
    await user.click(screen.getByRole("button", { name: "Distribute evenly" }));
    const values = [
      "Monday",
      "Tuesday",
      "Wednesday",
      "Thursday",
      "Friday",
      "Saturday",
      "Sunday",
    ].map((day) => (screen.getByLabelText(`${day} required time`) as HTMLInputElement).value);
    expect(values.filter((value) => value === "4:08")).toHaveLength(3);
    expect(values.filter((value) => value === "4:09")).toHaveLength(4);
    expect(screen.getByText(textContent("Remaining 0:00"))).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save working time" })).toBeEnabled();
  });

  it("saves the effective-dated arrangement to the API", async () => {
    const user = userEvent.setup();
    renderProfile();
    const target = await screen.findByLabelText("Weekly target");
    await user.clear(target);
    await user.type(target, "39:00");
    await user.type(screen.getByLabelText("Monday required time"), "7:48");
    await user.type(screen.getByLabelText("Tuesday required time"), "7:48");
    await user.type(screen.getByLabelText("Wednesday required time"), "7:48");
    await user.type(screen.getByLabelText("Thursday required time"), "7:48");
    await user.type(screen.getByLabelText("Friday required time"), "7:48");
    await user.click(screen.getByRole("button", { name: "Save working time" }));
    expect(putPayload).toEqual({
      valid_from: expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/),
      weekly_required_minutes: 2340,
      mon_required_minutes: 468,
      tue_required_minutes: 468,
      wed_required_minutes: 468,
      thu_required_minutes: 468,
      fri_required_minutes: 468,
      sat_required_minutes: 0,
      sun_required_minutes: 0,
    });
    expect(
      // The UI defaults effective_from to berlinToday(); assert against the
      // captured payload so the test cannot rot at a midnight boundary.
      await screen.findByText(
        textContent(
          `Saved. Required time is 39:00/week from ${String(putPayload?.valid_from).split("-").reverse().join(".")}. Your HR contract is unchanged.`,
        ),
      ),
    ).toBeInTheDocument();
  });

  it("uses a chosen future effective-from date", async () => {
    const user = userEvent.setup();
    renderProfile();
    await screen.findByLabelText("Weekly target");
    const from = screen.getByLabelText("Effective from");
    fireEvent.change(from, { target: { value: "2026-12-01" } });
    expect(screen.getByText(/Takes effect on 01\.12\.2026/)).toBeInTheDocument();
    for (const day of ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]) {
      await user.type(screen.getByLabelText(`${day} required time`), "5:48");
    }
    await user.click(screen.getByRole("button", { name: "Save working time" }));
    expect(putPayload?.valid_from).toBe("2026-12-01");
  });

  it("surfaces the server's 422 message after a valid client-side submission", async () => {
    const user = userEvent.setup();
    workingTimeError = "The chosen effective date is unavailable.";
    renderProfile();
    await screen.findByLabelText("Weekly target");
    await user.click(screen.getByRole("button", { name: "Distribute evenly" }));
    await user.click(screen.getByRole("button", { name: "Save working time" }));
    expect(
      await screen.findByText("Could not save: The chosen effective date is unavailable."),
    ).toBeInTheDocument();
  });

  it("announces a refused working-time save to assistive technology", async () => {
    const user = userEvent.setup();
    workingTimeError = "The chosen effective date is unavailable.";
    renderProfile();
    await screen.findByLabelText("Weekly target");
    await user.click(screen.getByRole("button", { name: "Distribute evenly" }));
    await user.click(screen.getByRole("button", { name: "Save working time" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not save: The chosen effective date is unavailable.",
    );
  });

  it("announces a refused preference save to assistive technology", async () => {
    const user = userEvent.setup();
    preferencesError = "Preferences could not be stored.";
    renderProfile();
    // Resolve the working-time allocation first: an unresolved allocation renders
    // its own (pre-existing) validation alert, and this assertion is about the
    // PREFERENCES alert being the one that is announced.
    await screen.findByLabelText("Weekly target");
    await user.click(screen.getByRole("button", { name: "Distribute evenly" }));
    await user.click(await screen.findByRole("button", { name: "Save preferences" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not save preferences.");
  });

  it("turning a working day off zeroes and disables its duration input", async () => {
    const user = userEvent.setup();
    renderProfile();
    await screen.findByLabelText("Weekly target");
    await user.click(screen.getByLabelText("Saturday working day"));
    const saturday = await screen.findByLabelText("Saturday required time");
    await user.type(saturday, "4:00");
    expect(saturday).toBeEnabled();
    await user.click(screen.getByLabelText("Saturday working day"));
    expect(screen.getByLabelText("Saturday required time")).toHaveValue("0:00");
    expect(screen.getByLabelText("Saturday required time")).toBeDisabled();
  });

  it("maintains the arrangement the target resolves to, and never claims it cannot be edited", async () => {
    profileForTest = SEEDED_PROFILE;
    renderProfile();
    const panel = (await screen.findByText("My working time")).closest("section");
    expect(panel).not.toBeNull();
    // The panel states the employee-owned model the onboarding slice introduced,
    // and names the arrangement by the date it starts — the maintenance key.
    expect(
      await within(panel as HTMLElement).findByText(/Editing the arrangement from/),
    ).toBeInTheDocument();
    expect(within(panel as HTMLElement).getByText("01.01.2026")).toBeInTheDocument();
    expect(within(panel as HTMLElement).getByText("29:00/week")).toBeInTheDocument();
    expect(within(panel as HTMLElement).getByText("from master data")).toBeInTheDocument();
    expect(
      within(panel as HTMLElement).getByText(/does not change your employment contract in HR/),
    ).toBeInTheDocument();
    // The pre-onboarding claim is GONE (acceptance N2): this panel IS the editor,
    // so it cannot also say the arrangement is shown read-only.
    expect(within(panel as HTMLElement).queryByText(/cannot be edited/)).not.toBeInTheDocument();
    expect(within(panel as HTMLElement).queryByText(/for transparency/)).not.toBeInTheDocument();
  });

  it("keeps a date the employee typed, and does not block a new arrangement on it", async () => {
    // R1: the normalising effect used to snap EVERY target back to the covering
    // arrangement's `valid_from`, so an in-range date was unusable and the
    // retroactive-effect note could never appear.
    profileForTest = SEEDED_PROFILE;
    renderProfile();
    const panel = (await screen.findByText("My working time")).closest("section");
    const effectiveFrom = await within(panel as HTMLElement).findByLabelText("Effective from");
    // The panel normalises on its own to the resolved arrangement's start…
    await waitFor(() => expect(effectiveFrom).toHaveValue("01.01.2026"));

    // …but a date the employee types stands, and is not snapped back.
    fireEvent.change(effectiveFrom, { target: { value: "01.08.2026" } });
    await waitFor(() => expect(effectiveFrom).toHaveValue("01.08.2026"));
    expect(
      within(panel as HTMLElement).getByText(/Takes effect retroactively from 01\.08\.2026/),
    ).toBeInTheDocument();

    // And the unchanged guard does not block it: a different date is a new
    // arrangement even when the values match.
    const save = within(panel as HTMLElement).getByRole("button", { name: "Save working time" });
    expect(save).toBeEnabled();
  });

  it("keeps authoritative HR facts visually separate from the self-service editor", async () => {
    profileForTest = SEEDED_PROFILE;
    renderProfile();
    const authoritative = (await screen.findByText("Authoritative profile")).closest("section");
    expect(authoritative).not.toBeNull();
    expect(within(authoritative!).getByText("Contractual weekly hours (HR)")).toBeInTheDocument();
    // weekday schedule values are NOT presented as authoritative master data
    expect(within(authoritative!).queryByText("Monday")).not.toBeInTheDocument();
    expect(screen.getByText("My working time")).toBeInTheDocument();
  });
});

describe("Chronobeam start (onboarding)", () => {
  beforeEach(() => {
    putPayload = null;
    profileForTest = EMPTY_PROFILE;
    workingTimeError = null;
    preferencesError = null;
    onboardingForTest = COMPLETED_ONBOARDING;
    onboardingPayload = null;
    correctionPayload = null;
    onboardingError = null;
    onboardingByMonth = {};
    onboardingMonthsRequested = [];
    vi.stubGlobal("fetch", vi.fn(profileFetchMock));
  });

  async function renderReady() {
    onboardingForTest = READY_ONBOARDING;
    renderProfile();
    await screen.findByRole("group", { name: "When Chronobeam starts for you" });
  }

  /**
   * Establishing the cutover state writes a weekly schedule AND the opening
   * balance — the same kind of fact `saveWorkingTime` writes, which is why that
   * mutation uses the full fact-derived breadth. Invalidating `["month"]` alone
   * left the findings list, the day panel and Home's balance card showing the
   * pre-establishment state (review R21, found while checking R18's class).
   */
  it("refreshes every fact-derived view when the cutover state is established", async () => {
    onboardingForTest = READY_ONBOARDING;
    const client = new QueryClient();
    const invalidated: unknown[] = [];
    vi.spyOn(client, "invalidateQueries").mockImplementation(async (filters) => {
      invalidated.push((filters as { queryKey?: unknown })?.queryKey);
    });
    renderProfile(client);
    const user = userEvent.setup();
    await screen.findByRole("group", { name: "When Chronobeam starts for you" });
    await user.type(screen.getByLabelText("Weekly working time"), "29:00");
    await user.click(screen.getByRole("button", { name: "Distribute evenly" }));
    await user.type(screen.getByLabelText("Carried balance"), "18:24");
    await user.click(screen.getByRole("button", { name: "Confirm my start" }));

    await waitFor(() => expect(onboardingPayload).not.toBeNull());
    const keys = invalidated.map((key) => JSON.stringify(key));
    // The opening balance and the schedule move all of these, not just the month.
    for (const family of ["month", "findings", "day", "home-month-summary", "shifts"]) {
      expect(
        keys.some((key) => key.includes(`"${family}"`)),
        `not invalidated: ${family}`,
      ).toBe(true);
    }
  });

  it("groups the three concepts as three named fieldsets and owns the setup", async () => {
    await renderReady();
    // The three concepts the employee must not have to disentangle.
    expect(
      screen.getByRole("group", { name: "When Chronobeam starts for you" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("group", { name: "Your normal weekly working time" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("group", { name: "Balance carried from the previous system" }),
    ).toBeInTheDocument();
    // One editor for one dataset: the standalone editor is hidden only while the
    // form is actually there to take its place.
    expect(screen.queryByText("My working time")).not.toBeInTheDocument();
    // The month boundary and the Excel archive are stated once.
    expect(screen.getByText(/Earlier months stay in the Excel archive/)).toBeInTheDocument();
  });

  it("keeps the working-time editor when onboarding is blocked", async () => {
    onboardingForTest = {
      ...READY_ONBOARDING,
      state: "BLOCKED",
      blocked_reason: "POLICY_EDITION_MISSING",
      blocked_month: "2026-09",
    };
    renderProfile();
    await screen.findByText(/working-time rules for September 2026 are not configured/);
    // No form, and NO "try again" — this is a permanent property of the month.
    expect(screen.queryByRole("group", { name: "When Chronobeam starts for you" })).toBeNull();
    // …and the capability is NOT removed: the editor stays, and it still saves.
    expect(screen.getByText("My working time")).toBeInTheDocument();
    const user = userEvent.setup();
    // The weekly target is already prefilled from the contract, so clear it
    // rather than appending to it.
    await user.clear(screen.getByLabelText("Weekly target"));
    await user.type(screen.getByLabelText("Weekly target"), "29:00");
    await user.click(screen.getByRole("button", { name: "Distribute evenly" }));
    await user.click(screen.getByRole("button", { name: "Save working time" }));
    await waitFor(() => expect(putPayload).not.toBeNull());
    expect(putPayload!.weekly_required_minutes).toBe(1740);
  });

  it("can be changed by clicking the prefilled month field and typing", async () => {
    const user = userEvent.setup();
    await renderReady();
    const field = screen.getByLabelText("Start month");
    expect(field).toHaveValue("09.2026");
    // What a human does: click the field, type the month they want. The field is
    // prefilled to exactly `maxLength`, so unless focusing selects the value every
    // keystroke is dropped and the month silently cannot be changed at all — the
    // other fields on this card are plain inputs and are unaffected, which is what
    // makes it look like this one "comes from nowhere".
    await user.click(field);
    await user.keyboard("11.2026");
    expect(field).toHaveValue("11.2026");
  });

  it("refuses a month before the earliest covered one without asking the server", async () => {
    await renderReady();
    // September 2026 is the earliest covered month, so January is refused from
    // what the card ALREADY knows — a past month is answered in words and no
    // request is sent at all (B3).
    fireEvent.change(screen.getByLabelText("Start month"), { target: { value: "01.2026" } });
    expect(screen.getByText(/cannot start before September 2026/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirm my start" })).toBeDisabled();
    expect(onboardingMonthsRequested).not.toContain("2026-01");
  });

  it("re-reads the verdict for the month the employee actually picks", async () => {
    onboardingByMonth["2026-11"] = {
      ...READY_ONBOARDING,
      month: "2026-11",
      has_facts_in_month: true,
    };
    await renderReady();
    fireEvent.change(screen.getByLabelText("Start month"), { target: { value: "11.2026" } });
    // The warning names the month the employee CHOSE, not the loaded one.
    await screen.findByText(/already recorded facts in November 2026/);
    expect(onboardingMonthsRequested).toContain("2026-11");
  });

  it("refuses a picked month the server reports as unavailable", async () => {
    onboardingByMonth["2026-11"] = {
      ...READY_ONBOARDING,
      state: "BLOCKED",
      month: "2026-11",
      blocked_reason: "CALENDAR_MISSING",
      blocked_month: "2026-11",
    };
    await renderReady();
    fireEvent.change(screen.getByLabelText("Start month"), { target: { value: "11.2026" } });
    await screen.findByText(/public-holiday calendar does not cover November 2026/);
    expect(screen.getByRole("button", { name: "Confirm my start" })).toBeDisabled();
  });

  it("preselects nothing when no covered month exists", async () => {
    onboardingForTest = { ...READY_ONBOARDING, default_start_month: null };
    renderProfile();
    await screen.findByRole("group", { name: "When Chronobeam starts for you" });
    expect(screen.getByLabelText("Start month")).toHaveValue("");
    expect(screen.getByRole("button", { name: "Confirm my start" })).toBeDisabled();
  });

  it("blocks submit on an uneven weekday distribution and reports the shortfall", async () => {
    await renderReady();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Weekly working time"), "29:00");
    await user.click(screen.getByRole("button", { name: "Distribute evenly" }));
    // Monday loses an hour, so the allocation no longer matches the total.
    await user.clear(screen.getByLabelText("Monday required time"));
    await user.type(screen.getByLabelText("Monday required time"), "4:48");
    expect(screen.getByRole("button", { name: "Confirm my start" })).toBeDisabled();
    expect(screen.getByText(/remaining — allocate exactly your weekly total/)).toBeInTheDocument();
  });

  it("maps the balance direction and duration to a signed integer", async () => {
    await renderReady();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Weekly working time"), "29:00");
    await user.click(screen.getByRole("button", { name: "Distribute evenly" }));
    await user.type(screen.getByLabelText("Carried balance"), "18:24");
    // The meaning is stated in words, never carried by a sign alone.
    expect(screen.getByText("Carried deficit 18:24")).toBeInTheDocument();
    await user.click(screen.getByLabelText("I am owed time (credit)"));
    expect(screen.getByText("Carried credit 18:24")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Confirm my start" }));
    expect(onboardingPayload).not.toBeNull();
    expect(onboardingPayload!.opening_balance_minutes).toBe(1104); // credit, positive
    expect(onboardingPayload!.weekly_required_minutes).toBe(1740);
    expect(onboardingPayload!.start_month).toBe("2026-09");
  });

  it("sends the employee-entered weekly total and weekday distribution", async () => {
    await renderReady();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Weekly working time"), "20:00");
    await user.clear(screen.getByLabelText("Monday required time"));
    await user.type(screen.getByLabelText("Monday required time"), "4:00");
    await user.clear(screen.getByLabelText("Tuesday required time"));
    await user.type(screen.getByLabelText("Tuesday required time"), "4:00");
    await user.clear(screen.getByLabelText("Wednesday required time"));
    await user.type(screen.getByLabelText("Wednesday required time"), "4:00");
    await user.clear(screen.getByLabelText("Thursday required time"));
    await user.type(screen.getByLabelText("Thursday required time"), "4:00");
    await user.clear(screen.getByLabelText("Friday required time"));
    await user.type(screen.getByLabelText("Friday required time"), "4:00");
    await user.click(screen.getByRole("button", { name: "Confirm my start" }));
    expect(onboardingPayload!.weekly_required_minutes).toBe(1200);
    expect(onboardingPayload!.mon_required_minutes).toBe(240);
    expect(onboardingPayload!.sat_required_minutes).toBe(0);
  });

  it("moves focus to the card heading after a successful establish", async () => {
    await renderReady();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Weekly working time"), "29:00");
    await user.click(screen.getByRole("button", { name: "Distribute evenly" }));
    await user.click(screen.getByRole("button", { name: "Confirm my start" }));
    const heading = await screen.findByRole("heading", { name: "Your Chronobeam start" });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(screen.getByText("Your Chronobeam start is set up.")).toBeInTheDocument();
  });

  it("associates a per-weekday error with its input", async () => {
    await renderReady();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Monday required time"), "abc");
    const monday = screen.getByLabelText("Monday required time");
    expect(monday).toHaveAttribute("aria-invalid", "true");
    const describedBy = monday.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy!)).toHaveTextContent("Use H:MM");
  });

  it("associates a weekly-total error with the weekly input", async () => {
    // A detached form-level alert left the employee unable to tell WHICH
    // control was wrong, so the field carries the association itself.
    await renderReady();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Weekly working time"), "abc");
    const weekly = screen.getByLabelText("Weekly working time");
    expect(weekly).toHaveAttribute("aria-invalid", "true");
    const describedBy = weekly.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy!)).toHaveTextContent("Use H:MM");
  });

  it("moves focus to the error summary when the server refuses", async () => {
    await renderReady();
    onboardingError = { status: 409, code: "CONFLICT_CUTOVER_SEED", message: "Already set up." };
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Weekly working time"), "29:00");
    await user.click(screen.getByRole("button", { name: "Distribute evenly" }));
    await user.click(screen.getByRole("button", { name: "Confirm my start" }));
    const alert = await screen.findByRole("alert");
    await waitFor(() => expect(alert).toHaveFocus());
  });

  it("reports the cutover arrangement, not today's, in the summary", async () => {
    // The server resolves the reported schedule at the cutover month; the card
    // must render THAT, and must not invent "0:00" when it is absent.
    onboardingForTest = {
      ...COMPLETED_ONBOARDING,
      schedule: { ...SAVED_SCHEDULE, weekly_required_minutes: 1740 },
    };
    renderProfile();
    await screen.findByText("Weekly working time");
    expect(screen.getByText("29:00 (Mon–Fri)")).toBeInTheDocument();
  });

  it("shows the carried balance and offers correction while it is unused", async () => {
    renderProfile();
    const summary = await screen.findByText("Carried balance");
    expect(summary).toBeInTheDocument();
    expect(screen.getByText(/Carried deficit 18:24 from August 2026/)).toBeInTheDocument();
    expect(screen.getByText("Correct the carried balance")).toBeInTheDocument();
  });

  it("sends expected_revision and a reason when correcting", async () => {
    renderProfile();
    const user = userEvent.setup();
    await user.click(await screen.findByText("Correct the carried balance"));
    await user.clear(screen.getByLabelText("Corrected carried balance"));
    await user.type(screen.getByLabelText("Corrected carried balance"), "24:00");
    await user.type(screen.getByLabelText("Reason for the correction"), "mistyped");
    await user.click(screen.getByRole("button", { name: "Save correction" }));
    expect(correctionPayload).not.toBeNull();
    expect(correctionPayload!.expected_revision).toBe(1);
    expect(correctionPayload!.reason).toBe("mistyped");
    expect(correctionPayload!.opening_balance_minutes).toBe(-1440); // still a deficit
  });

  it("closes self-correction with the specific reason and claims no admin workflow", async () => {
    onboardingForTest = {
      ...COMPLETED_ONBOARDING,
      correctable: false,
      in_use_reason: "IN_USE_BY_FACTS",
    };
    renderProfile();
    await screen.findByText("Carried balance");
    // No disabled control: `accessibility` prefers invisibility over disabling.
    expect(screen.queryByText("Correct the carried balance")).toBeNull();
    expect(
      screen.getByText(/You have already recorded work in September 2026\./),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /Self-service correction is no longer available\. Contact the administrator if the carried balance needs to be corrected\./,
      ),
    ).toBeInTheDocument();
  });

  it("names a shared month as the reason the window closed", async () => {
    onboardingForTest = {
      ...COMPLETED_ONBOARDING,
      correctable: false,
      in_use_reason: "IN_USE_BY_DISCLOSURE",
    };
    renderProfile();
    await screen.findByText("Carried balance");
    expect(screen.getByText(/September 2026 has been shared\./)).toBeInTheDocument();
  });

  it("warns before establishing when the month already has facts", async () => {
    onboardingForTest = { ...READY_ONBOARDING, has_facts_in_month: true };
    renderProfile();
    await screen.findByRole("group", { name: "When Chronobeam starts for you" });
    expect(
      screen.getByText(/You have already recorded facts in September 2026\./),
    ).toBeInTheDocument();
    expect(screen.getByText(/no longer correct the carried balance yourself/)).toBeInTheDocument();
  });

  it("surfaces a server refusal on the start form without losing the form", async () => {
    await renderReady();
    onboardingError = { status: 409, code: "CONFLICT_CUTOVER_SEED", message: "Already set up." };
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Weekly working time"), "29:00");
    await user.click(screen.getByRole("button", { name: "Distribute evenly" }));
    await user.click(screen.getByRole("button", { name: "Confirm my start" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Already set up.");
    // The form is still there, so the employee does not lose their entry.
    expect(
      screen.getByRole("group", { name: "When Chronobeam starts for you" }),
    ).toBeInTheDocument();
  });

  it("hides HR hours that this MVP does not synchronise", async () => {
    // D1: with no contract, the page must not claim HR owns hours the employee
    // maintains two cards below.
    profileForTest = { ...EMPTY_PROFILE, contract: null };
    renderProfile();
    await screen.findByText("Authoritative profile");
    expect(screen.queryByText("Contractual weekly hours (HR)")).toBeNull();
    expect(screen.getByText(/you maintain it yourself in/)).toBeInTheDocument();
  });

  it("fails safe on an unrecognised state: no form, and the editor stays", async () => {
    // A stale fixture or a malformed payload must not conjure the onboarding
    // form — that would duplicate the working-time setup and invite the employee
    // to establish a start state the server never asked for.
    onboardingForTest = { ...COMPLETED_ONBOARDING, state: undefined };
    renderProfile();
    await screen.findByText("Your Chronobeam start");
    expect(screen.queryByRole("group", { name: "When Chronobeam starts for you" })).toBeNull();
    expect(screen.queryByText("Carried balance")).toBeNull();
    // The capability is never removed.
    expect(screen.getByText("My working time")).toBeInTheDocument();
    // …and the working-day picker exists exactly once, so nothing is duplicated.
    expect(document.querySelectorAll(".working-day-picker")).toHaveLength(1);
  });
});
