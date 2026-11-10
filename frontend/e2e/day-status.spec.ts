import { expect, test, type Page, type Route } from "@playwright/test";

/**
 * PHASE-5 day-status journey (EINV-22): an employee declares a vacation day,
 * the SERVER reduces the day's requirement and derives the employee-private
 * WT-STATUS-001 finding when work is still recorded on a full vacation day,
 * and correcting the fact to a half day moves the requirement again.
 *
 * The frontend never reproduces that arithmetic (ARCH-MOD-004), so the stateful
 * in-page mock recomputes it exactly like the policy engine does:
 *
 *   required = scheduled - min(reducing_halves, 2) * scheduled // 2
 *   WT-STATUS-001 (CRITICAL) when worked > 0 and a FULL reducing fact exists
 *
 *   GET    /api/v1/me/day-status?period=YYYY-MM -> {period, entries,
 *                                                   compensation_metrics}
 *   POST   /api/v1/me/day-status                -> 201 DayStatus
 *   PATCH  /api/v1/me/day-status/{id}           -> 200 DayStatus (versioned)
 *
 * The journey proves the frontend is a view of server-derived numbers, not a
 * second calculator: every number asserted below comes from the mock's month
 * payload, never from client-side arithmetic.
 *
 *   GET    /api/v1/me/months/2026-07 -> daily_results carry calendar_class and
 *                                       account_credit_minutes
 */

const DAY = "2026-07-06"; // Monday
const SCHEDULED_MINUTES = 348; // 5:48
const REDUCING_CATEGORIES = new Set(["VACATION", "SICK"]);
const PORTION_HALVES: Record<string, number> = { FULL: 2, HALF: 1 };
const DAY_HALVES = 2;

let dayStatus: Array<Record<string, unknown>> = [];
let intervals: Array<Record<string, unknown>> = [];
let seq = 1;

function reducingHalves(): number {
  return dayStatus
    .filter((entry) => REDUCING_CATEGORIES.has(String(entry.category)))
    .reduce((sum, entry) => sum + (PORTION_HALVES[String(entry.portion)] ?? 0), 0);
}

function requiredMinutes(): number {
  const covered = Math.min(reducingHalves(), DAY_HALVES);
  return SCHEDULED_MINUTES - covered * Math.floor(SCHEDULED_MINUTES / DAY_HALVES);
}

function workedMinutes(): number {
  return intervals.reduce((sum, interval) => sum + Number(interval.elapsed_seconds) / 60, 0);
}

/** The extended day-status payload's read-only metric block: the server counts
 *  the recorded facts and states the limits are guidance, not applied limits. */
function compensationMetrics(): Record<string, unknown> {
  const flex = dayStatus.filter((entry) => entry.category === "FLEX_COMPENSATION");
  return {
    year: 2026,
    flex_compensation_full_days: flex.filter((entry) => entry.portion === "FULL").length,
    flex_compensation_half_days: flex.filter((entry) => entry.portion === "HALF").length,
    flex_compensation_zero_requirement_days: 0,
    flex_compensation_longest_run_days: 0,
    shift_compensation_days: dayStatus.filter((entry) => entry.category === "SHIFT_COMPENSATION")
      .length,
    limits: {
      max_days_per_year: 20,
      max_consecutive_days: 5,
      source: "POL-05",
      enforced: false,
    },
    counting_note:
      "This is guidance, not a limit we apply. How a half day counts towards the 20-day limit is " +
      "an open configuration question.",
  };
}

function findings(): Array<Record<string, unknown>> {
  const fullVacation = dayStatus.some(
    (entry) => entry.portion === "FULL" && entry.category === "VACATION",
  );
  if (workedMinutes() === 0 || !fullVacation) return [];
  return [
    {
      rule_id: "WT-STATUS-001",
      severity: "CRITICAL",
      date: DAY,
      message:
        "Work is recorded on a full vacation day. Correct or remove the recorded interval, " +
        "or change the day status.",
    },
  ];
}

async function mockApi(route: Route): Promise<void> {
  const request = route.request();
  const url = request.url();
  const method = request.method();
  const body = request.postDataJSON() as Record<string, unknown> | null;
  const json = (payload: unknown, status = 200) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(payload) });

  if (url.endsWith("/api/v1/me") && method === "GET") {
    await json({
      subject: "employee",
      display_name: "Employee A",
      capabilities: ["OWN_LEDGER_READ", "OWN_LEDGER_WRITE"],
    });
    return;
  }
  if (url.includes("/api/v1/me/profile")) {
    await json({
      effective_at: DAY,
      employee: { external_employee_id: "emp-0001", legal_name: "Synthetic Employee", email: null },
      contract: {
        valid_from: "2026-01-01",
        valid_to: null,
        fte: 1,
        weekly_required_minutes: 1740,
        pay_group_bucket: "LE_9B",
        source: "MASTER",
        version: 1,
      },
      schedule: {
        valid_from: "2026-01-01",
        valid_to: null,
        weekly_required_minutes: 1740,
        mon_required_minutes: SCHEDULED_MINUTES,
        tue_required_minutes: SCHEDULED_MINUTES,
        wed_required_minutes: SCHEDULED_MINUTES,
        thu_required_minutes: SCHEDULED_MINUTES,
        fri_required_minutes: SCHEDULED_MINUTES,
        sat_required_minutes: 0,
        sun_required_minutes: 0,
        source: "MASTER",
        version: 1,
      },
      preferences: {
        usual_work_type: "ON_SITE",
        preferred_calendar_view: "month",
        normal_day_template: null,
      },
    });
    return;
  }

  // --- day status ---------------------------------------------------------
  if (url.includes("/api/v1/me/day-status") && method === "POST") {
    const created = {
      id: `status-${seq++}`,
      day: body?.day,
      category: body?.category,
      portion: body?.portion,
      day_part: body?.day_part,
      version: 1,
      supersedes_id: null,
      created_at: "2026-07-06T08:00:00+02:00",
    };
    dayStatus.push(created);
    await json(created, 201);
    return;
  }
  if (url.includes("/api/v1/me/day-status/") && method === "PATCH") {
    const id = url.match(/day-status\/([^/?]+)/)?.[1];
    const current = dayStatus.find((entry) => entry.id === id);
    if (!current) {
      await json({ detail: { code: "NOT_FOUND", message: "missing" } }, 404);
      return;
    }
    // Presence-based correction: omitted fields keep their value, and the
    // version advances (the server supersedes rather than overwrites).
    const corrected = {
      ...current,
      category: body?.category ?? current.category,
      portion: body?.portion ?? current.portion,
      day_part: body?.day_part ?? current.day_part,
      version: Number(current.version) + 1,
      supersedes_id: current.id,
    };
    dayStatus = dayStatus.map((entry) => (entry.id === id ? corrected : entry));
    await json(corrected);
    return;
  }
  if (url.includes("/api/v1/me/day-status")) {
    await json({
      period: "2026-07",
      entries: dayStatus,
      compensation_metrics: compensationMetrics(),
    });
    return;
  }

  // --- ledger -------------------------------------------------------------
  if (url.includes("/api/v1/me/work-intervals") && method === "POST") {
    const startLocal = String(body?.start_local ?? "");
    const endLocal = String(body?.end_local ?? "");
    const minutes =
      (Date.parse(`${endLocal.slice(0, 16)}:00Z`) - Date.parse(`${startLocal.slice(0, 16)}:00Z`)) /
      60000;
    const created = {
      id: `interval-${intervals.length + 1}`,
      work_type: body?.work_type ?? "ON_SITE",
      start_at: `${startLocal.slice(0, 16)}:00+02:00`,
      end_at: `${endLocal.slice(0, 16)}:00+02:00`,
      start_local: startLocal,
      end_local: endLocal,
      elapsed_seconds: minutes * 60,
      comment: null,
      version: 1,
    };
    intervals.push(created);
    await json(created, 201);
    return;
  }
  if (url.includes("/api/v1/me/findings")) {
    await json({ closing_balance_minutes: 0, findings: findings() });
    return;
  }
  const dayMatch = url.match(/\/api\/v1\/me\/months\/2026-07\/days\/([0-9-]+)/);
  if (dayMatch) {
    await json({
      date: dayMatch[1],
      intervals: intervals.filter((interval) =>
        String(interval.start_local).startsWith(dayMatch[1]),
      ),
    });
    return;
  }
  if (url.includes("/api/v1/me/months/2026-07")) {
    await json({
      period_month: "2026-07-01",
      status: "OPEN",
      interval_count: intervals.length,
      closing_balance_minutes: 0,
      days: intervals.length ? [{ date: DAY, interval_count: intervals.length }] : [],
      daily_results: [
        {
          date: DAY,
          worked_minutes: workedMinutes(),
          required_minutes: requiredMinutes(),
          delta_minutes: workedMinutes() - requiredMinutes(),
          calendar_class: "WEEKDAY",
          account_credit_minutes: workedMinutes(),
        },
      ],
    });
    return;
  }
  if (url.includes("/api/v1/me/shifts")) {
    await json([]);
    return;
  }

  await json({});
}

/** The day panel's server-calculated totals: "Worked" / "Required" / "Delta". */
function dayTotal(page: Page, label: string) {
  return page.locator(".day-totals > div").filter({ hasText: label }).locator("dd");
}

test.beforeEach(async ({ page }) => {
  dayStatus = [];
  intervals = [];
  seq = 1;
  await page.route("**/api/v1/**", mockApi);
  await page.goto("/month/2026-07");
  await expect(page.getByText("Private — visible only to you").first()).toBeVisible();
  await page.getByRole("button", { name: /^Monday, 6 July 2026/ }).click();
  await expect(page.getByRole("heading", { name: /^Mon, 6 July$/ })).toBeVisible();
});

test("a full-day vacation reduces the requirement, and work on it is a private finding", async ({
  page,
}) => {
  // Before the fact: the scheduled Monday requirement is the only requirement.
  await expect(dayTotal(page, "Required")).toHaveText("5:48");
  await expect(dayTotal(page, "Worked")).toHaveText("0:00");
  await expect(page.getByText("No day status for this day.")).toBeVisible();

  // Declare a full vacation day (the default category and extent).
  await page.getByRole("button", { name: "+ Add day status" }).click();
  await page.getByLabel("Day status category").selectOption("VACATION");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  await expect(page.locator(".day-status-block").locator(".status-ok")).toContainText(
    "Day status saved.",
  );

  // The requirement is the SERVER's reduced number, and the fact is marked in the
  // calendar cell by a semantic event BAR: the category is carried by a WORD as
  // well as by the hue, and the cell's own accessible name states the full spoken
  // phrase. A full day states NO extent — it is the default, and the extra word
  // ellipsized the category it was meant to qualify.
  await expect(dayTotal(page, "Required")).toHaveText("0:00");
  // Scoped to the GRID: "Vacation (full day)" also appears in the day-status
  // section's own "Correct …" / "Remove …" accessible names, so an unscoped role
  // query resolves to three buttons and fails in strict mode.
  const fullDayCell = page
    .locator(".calendar-grid")
    .getByRole("button", { name: /Vacation \(full day\)/ });
  await expect(fullDayCell).toBeVisible();
  await expect(fullDayCell.locator(".cal-bar")).toHaveAttribute("data-cal-kind", "VACATION");
  await expect(fullDayCell.locator(".cal-bar-label")).toHaveText("Vacation");
  await expect(fullDayCell.locator(".cal-bar-detail")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Correct Vacation (full day)" })).toBeVisible();

  // Work is still recorded on that vacation day: the ledger keeps the fact and
  // the employee gets the private finding (never a silent deletion).
  await page.getByRole("button", { name: "+ Add interval" }).click();
  await page.getByLabel("New start time").fill("09:00");
  await page.getByLabel("New end time").fill("13:00");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  await expect(page.getByText("09:00–13:00")).toBeVisible();
  await expect(dayTotal(page, "Worked")).toHaveText("4:00");
  await expect(dayTotal(page, "Required")).toHaveText("0:00");
  await expect(dayTotal(page, "Delta")).toHaveText("+4:00");
  const statusFindings = page.locator(".day-status-findings");
  // The rule is named in words; the engine code is never rendered (7A).
  await expect(statusFindings).toContainText("Work recorded on a full vacation day");
  await expect(statusFindings).not.toContainText("WT-STATUS-001");

  // Correcting the fact to a half day re-derives the requirement: the morning
  // half is vacation, the other half is an ordinary working half (2:54).
  await page.getByRole("button", { name: "Correct Vacation (full day)" }).click();
  await page.getByLabel("Day status extent").selectOption("HALF");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.locator(".day-status-block").locator(".status-ok")).toContainText(
    "Day status updated.",
  );
  await expect(dayTotal(page, "Required")).toHaveText("2:54");
  await expect(dayTotal(page, "Delta")).toHaveText("+1:06");
  // The day-status chip became a semantic calendar bar: the category word and the
  // extent are separate elements (so the phone density can drop the extent), and
  // the cell's accessible name carries the spoken phrase.
  const vacationCell = page
    .locator(".calendar-grid")
    .getByRole("button", { name: /Vacation \(morning half\)/ });
  await expect(vacationCell).toBeVisible();
  await expect(vacationCell.locator(".cal-bar")).toHaveAttribute("data-cal-kind", "VACATION");
  await expect(vacationCell.locator(".cal-bar-label")).toHaveText("Vacation");
  // The BAR states the exception compactly ("am"), because the long form needs
  // twelve characters and ellipsized in every cell that had one; "morning half"
  // stays in the accessible name asserted above and in the day panel.
  await expect(vacationCell.locator(".cal-bar-detail")).toHaveText("am");
  // Only a FULL vacation day collides with recorded work, so the finding clears.
  await expect(page.locator(".day-status-findings")).toHaveCount(0);
});

test("the month page no longer shows the year's compensation-day statistics", async ({ page }) => {
  // Owner directive 2026-09-26: the block was removed from the Month workspace and
  // was NOT replaced by another summary widget. The figures themselves are
  // untouched — the server still computes and sends them — so this pins the
  // FRONTEND removal, and a reintroduction has to be a deliberate decision.
  await page.goto("/month/2026-07");
  await expect(page.getByRole("heading", { name: "Month" })).toBeVisible();
  await expect(page.getByLabel("Compensation days recorded")).toHaveCount(0);
  await expect(page.getByText(/Compensation days recorded in/)).toHaveCount(0);
  await expect(page.getByText("Flex-time, full days")).toHaveCount(0);
  await expect(page.getByText("Longest full-day run")).toHaveCount(0);
  await expect(page.getByText("Shift-compensation days")).toHaveCount(0);
  await expect(page.getByText(/Guidance: 20 days per year/)).toHaveCount(0);
});
