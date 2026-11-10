import { expect, test, type Page, type Route } from "@playwright/test";

/**
 * Employee self-onboarding (the one-time Chronobeam start).
 *
 * The API is mocked statefully so establishing and correcting behave like the
 * real append-only service: a POST creates the opening state, a PATCH appends a
 * correction, and the GET reflects whichever exists.
 *
 * Only the JOURNEY lives here. The rules — the delta arithmetic, the
 * concurrency, the in-use predicate — are covered deterministically in pytest.
 */

type Opening = {
  cutover_month: string;
  minutes: number;
  seed_minutes: number;
  occurred_on: string;
  previous_period: string;
  confirmed_by: string;
  revision: number;
  corrected_at: string | null;
};

let opening: Opening | null;
let correctable: boolean;
let inUseReason: string | null;
let blockedReason: string | null;
let posted: Record<string, number | string> | null;
let patched: Record<string, number | string> | null;

function onboardingBody() {
  if (blockedReason) {
    return {
      state: "BLOCKED",
      month: "2026-09",
      default_start_month: "2026-10",
      opening: null,
      blocked_reason: blockedReason,
      blocked_month: "2026-09",
      has_facts_in_month: false,
      correctable: false,
      in_use_reason: null,
      schedule: null,
    };
  }
  return {
    state: opening ? "COMPLETED" : "READY",
    month: "2026-09",
    default_start_month: "2026-09",
    opening,
    blocked_reason: null,
    blocked_month: null,
    has_facts_in_month: false,
    correctable,
    in_use_reason: inUseReason,
    // The server resolves this at the CUTOVER month, so the summary reports the
    // arrangement the employee established rather than today's.
    schedule: opening
      ? {
          valid_from: `${opening.cutover_month}-01`,
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
          version: 1,
        }
      : null,
  };
}

async function mockApi(route: Route): Promise<void> {
  const request = route.request();
  const url = request.url();
  const method = request.method();
  const json = (body: unknown) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });

  if (url.includes("/api/v1/me/onboarding") && method === "POST") {
    posted = request.postDataJSON() as Record<string, number | string>;
    const minutes = Number(posted.opening_balance_minutes);
    opening = {
      cutover_month: String(posted.start_month),
      minutes,
      seed_minutes: minutes,
      occurred_on: "2026-08-31",
      previous_period: "2026-08",
      confirmed_by: "employee",
      revision: 1,
      corrected_at: null,
    };
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify(onboardingBody()),
    });
    return;
  }
  if (url.includes("/api/v1/me/onboarding") && method === "PATCH") {
    patched = request.postDataJSON() as Record<string, number | string>;
    const minutes = Number(patched.opening_balance_minutes);
    opening = opening
      ? {
          ...opening,
          minutes,
          revision: opening.revision + 1,
          corrected_at: "2026-09-23T10:00:00Z",
        }
      : null;
    await json(onboardingBody());
    return;
  }
  if (url.includes("/api/v1/me/onboarding")) {
    await json(onboardingBody());
    return;
  }
  if (url.endsWith("/api/v1/me") && !url.includes("/me/")) {
    await json({
      subject: "employee",
      display_name: "Employee A",
      capabilities: [
        "OWN_LEDGER_READ",
        "OWN_LEDGER_WRITE",
        "OWN_SUBMISSION_CREATE",
        "OWN_DISCLOSURE_GRANT",
      ],
    });
    return;
  }
  if (url.includes("/api/v1/me/profile")) {
    await json({
      effective_at: "2026-09-23",
      employee: { external_employee_id: "emp-0001", legal_name: "Synthetic Employee", email: null },
      contract: { fte: 1, weekly_required_minutes: 1740, pay_group_bucket: "LE_9B" },
      schedule: null,
      preferences: {
        usual_work_type: "ON_SITE",
        preferred_calendar_view: "month",
        normal_day_template: null,
      },
    });
    return;
  }
  await json({});
}

async function openProfile(page: Page): Promise<void> {
  await page.route("**/api/v1/**", mockApi);
  await page.goto("/profile");
  await expect(page.getByRole("heading", { name: "Profile", exact: true })).toBeVisible();
}

/** Fill the whole start form: 29:00 over Mon-Fri and a signed carried balance. */
async function fillStartForm(page: Page, balance: string, credit = false): Promise<void> {
  await page.getByLabel("Weekly working time").fill("29:00");
  await page.getByRole("button", { name: "Distribute evenly" }).click();
  if (credit) await page.getByLabel("I am owed time (credit)").check();
  await page.getByLabel("Carried balance").fill(balance);
}

test.beforeEach(async () => {
  opening = null;
  correctable = true;
  inUseReason = null;
  blockedReason = null;
  posted = null;
  patched = null;
});

test("establishes the start state and replaces the form with the summary", async ({ page }) => {
  await openProfile(page);

  // The three concepts are three named groups, and the month boundary is
  // explained once — earlier months stay in the archive.
  await expect(page.getByRole("group", { name: "When Chronobeam starts for you" })).toBeVisible();
  await expect(page.getByRole("group", { name: "Your normal weekly working time" })).toBeVisible();
  await expect(
    page.getByRole("group", { name: "Balance carried from the previous system" }),
  ).toBeVisible();
  await expect(page.getByText(/Earlier months stay in the Excel archive/)).toBeVisible();
  // One setup surface while the form is present: the standalone editor is not
  // duplicated beside it.
  await expect(page.getByRole("heading", { name: "My working time" })).toHaveCount(0);

  // The start month is preselected from the server's covered-month suggestion.
  await expect(page.getByLabel("Start month")).toHaveValue("09.2026");

  await fillStartForm(page, "18:24");
  // The meaning is stated in words, never by a sign or a colour alone.
  // The confirm line restates the balance, so scope to the live summary.
  await expect(page.locator(".balance-summary strong")).toHaveText("Carried deficit 18:24");
  await page.getByRole("button", { name: "Confirm my start" }).click();

  expect(posted).not.toBeNull();
  expect(posted!.start_month).toBe("2026-09");
  expect(posted!.weekly_required_minutes).toBe(1740);
  expect(posted!.mon_required_minutes).toBe(348);
  expect(posted!.sat_required_minutes).toBe(0);
  expect(posted!.opening_balance_minutes).toBe(-1104); // deficit is negative

  // The card becomes a summary, the announcement survives the swap, and focus
  // lands on the card heading rather than falling to <body>.
  await expect(page.getByText("Your Chronobeam start is set up.")).toBeVisible();
  await expect(page.getByText(/Carried deficit 18:24 from August 2026/)).toBeVisible();
  // The reported arrangement is the one established at the cutover month.
  await expect(page.getByText("29:00 (Mon–Fri)")).toBeVisible();
  await expect(page.getByRole("group", { name: "When Chronobeam starts for you" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Your Chronobeam start" })).toBeFocused();
  // Maintenance is now the editor's job.
  await expect(page.getByRole("heading", { name: "My working time" })).toBeVisible();
});

test("blocks an uneven weekly distribution and states the shortfall", async ({ page }) => {
  await openProfile(page);

  await page.getByLabel("Weekly working time").fill("29:00");
  await page.getByRole("button", { name: "Distribute evenly" }).click();
  // Over-allocate: Monday gets an extra hour.
  await page.getByLabel("Monday required time").fill("6:48");
  await expect(page.getByRole("button", { name: "Confirm my start" })).toBeDisabled();
  await expect(page.getByText(/over-allocated — allocate exactly your weekly total/)).toBeVisible();

  // Correcting it unblocks the action.
  await page.getByLabel("Monday required time").fill("5:48");
  await expect(page.getByRole("button", { name: "Confirm my start" })).toBeEnabled();
  expect(posted).toBeNull();
});

test("carries a credit and reports zero as nothing carried over", async ({ page }) => {
  await openProfile(page);
  await fillStartForm(page, "12:00", true);
  await expect(page.locator(".balance-summary strong")).toHaveText("Carried credit 12:00");
  await page.getByRole("button", { name: "Confirm my start" }).click();
  expect(posted!.opening_balance_minutes).toBe(720); // credit is positive
  await expect(page.getByText(/Carried credit 12:00 from August 2026/)).toBeVisible();
});

test("corrects the carried balance while it is still unused", async ({ page }) => {
  opening = {
    cutover_month: "2026-09",
    minutes: -1104,
    seed_minutes: -1104,
    occurred_on: "2026-08-31",
    previous_period: "2026-08",
    confirmed_by: "employee",
    revision: 1,
    corrected_at: null,
  };
  await openProfile(page);

  await page.getByText("Correct the carried balance").click();
  await page.getByLabel("Corrected carried balance").fill("24:00");
  await page.getByLabel("Reason for the correction").fill("The carried figure was mistyped");
  await page.getByRole("button", { name: "Save correction" }).click();

  expect(patched).not.toBeNull();
  expect(patched!.expected_revision).toBe(1);
  expect(patched!.reason).toBe("The carried figure was mistyped");
  expect(patched!.opening_balance_minutes).toBe(-1440); // still a deficit
  await expect(page.getByText(/Carried deficit 24:00 from August 2026/)).toBeVisible();
});

test("closes self-correction after the first work fact, naming the reason", async ({ page }) => {
  opening = {
    cutover_month: "2026-09",
    minutes: -1104,
    seed_minutes: -1104,
    occurred_on: "2026-08-31",
    previous_period: "2026-08",
    confirmed_by: "employee",
    revision: 1,
    corrected_at: null,
  };
  correctable = false;
  inUseReason = "IN_USE_BY_FACTS";
  await openProfile(page);

  await expect(page.getByText(/You have already recorded work in September 2026\./)).toBeVisible();
  // The copy claims no workflow that does not exist, and shows no dead control.
  await expect(
    page.getByText(
      /Self-service correction is no longer available\. Contact the administrator if the carried balance needs to be corrected\./,
    ),
  ).toBeVisible();
  await expect(page.getByText("Correct the carried balance")).toHaveCount(0);
});

test("keeps the working-time editor when onboarding is blocked", async ({ page }) => {
  blockedReason = "CALENDAR_MISSING";
  await openProfile(page);

  await expect(
    page.getByText(/public-holiday calendar does not cover September 2026 yet/),
  ).toBeVisible();
  // No form and no "try again" — a permanent property of the month, not a fault.
  await expect(page.getByRole("group", { name: "When Chronobeam starts for you" })).toHaveCount(0);
  // Onboarding never removes the capability: the editor stays and still works.
  await expect(page.getByRole("heading", { name: "My working time" })).toBeVisible();
  await page.getByLabel("Weekly target").fill("29:00");
  await page.getByRole("button", { name: "Distribute evenly" }).click();
  await expect(page.locator(".working-time-totals")).toContainText("Remaining 0:00");
  await expect(page.getByRole("button", { name: "Save working time" })).toBeEnabled();
});

test("preselects nothing when no covered month exists", async ({ page }) => {
  await page.route("**/api/v1/**", async (route) => {
    if (route.request().url().includes("/api/v1/me/onboarding")) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          ...onboardingBody(),
          default_start_month: null,
        }),
      });
      return;
    }
    await mockApi(route);
  });
  await page.goto("/profile");
  await expect(page.getByRole("group", { name: "When Chronobeam starts for you" })).toBeVisible();
  // Nothing is preselected rather than guessed, and the action stays blocked.
  await expect(page.getByLabel("Start month")).toHaveValue("");
  await expect(page.getByRole("button", { name: "Confirm my start" })).toBeDisabled();
});
