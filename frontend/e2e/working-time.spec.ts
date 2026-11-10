import { expect, test, type Page, type Route } from "@playwright/test";

/**
 * PHASE-5A employee working-time setup (FE-SCREEN-005, DATA-011..013).
 *
 * The API is mocked statefully so saving + reloading behaves like the real
 * effective-dated service (a saved arrangement with valid_from <= today
 * becomes the "current arrangement"). Desktop and mobile projects both run.
 */

const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;
// Berlin's current date must match the UI's live berlinToday(); a fixed
// constant rots at a midnight boundary.
const TODAY = new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Berlin" }).format(new Date());

let schedules: Array<Record<string, unknown>> = [];
let nextVersion = 2;

async function mockApi(route: Route): Promise<void> {
  const request = route.request();
  const url = request.url();
  const method = request.method();
  const json = (body: unknown) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });

  if (url.includes("/api/v1/me/working-time") && method === "PUT") {
    const body = request.postDataJSON() as Record<string, number>;
    const allocated = DAYS.reduce((sum, day) => sum + (body[`${day}_required_minutes`] ?? 0), 0);
    if (allocated !== body.weekly_required_minutes) {
      await route.fulfill({
        status: 422,
        contentType: "application/json",
        body: JSON.stringify({
          detail: {
            code: "VALIDATION_FAILED",
            message: "Weekday allocation must sum exactly to the weekly target.",
          },
        }),
      });
      return;
    }
    schedules = schedules.filter((entry) => entry.valid_from !== body.valid_from);
    const saved = {
      valid_from: body.valid_from,
      valid_to: null,
      weekly_required_minutes: body.weekly_required_minutes,
      ...Object.fromEntries(
        DAYS.map((day) => [`${day}_required_minutes`, body[`${day}_required_minutes`]]),
      ),
      source: "EMPLOYEE",
      version: nextVersion++,
    };
    schedules.push(saved);
    await json(saved);
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
    const effective =
      schedules
        .filter((entry) => String(entry.valid_from) <= TODAY)
        .sort((a, b) => String(b.valid_from).localeCompare(String(a.valid_from)))[0] ?? null;
    await json({
      effective_at: TODAY,
      employee: { external_employee_id: "emp-0001", legal_name: "Synthetic Employee", email: null },
      contract: { fte: 1, weekly_required_minutes: 1740, pay_group_bucket: "LE_9B" },
      schedule: effective,
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

function dayCell(page: Page, day: string) {
  return page.locator(".weekday-cell").filter({ has: page.getByLabel(`${day} required time`) });
}

async function fillDays(page: Page, values: Array<[string, string]>): Promise<void> {
  for (const [day, value] of values) {
    const input = page.getByLabel(`${day} required time`);
    await input.fill(value);
  }
}

test.beforeEach(async ({ page }) => {
  schedules = [];
  nextVersion = 2;
  await page.route("**/api/v1/**", mockApi);
  await page.goto("/profile");
  await expect(page.getByRole("heading", { name: "Profile", exact: true })).toBeVisible();
});

test("part-time 29:00 with uneven days and a Sunday working day", async ({ page }, testInfo) => {
  await expect(page.getByLabel("Weekly target")).toHaveValue("29:00");
  await expect(page.getByText("Allocated")).toBeVisible();

  // Under-allocation blocks save with live feedback.
  await fillDays(page, [
    ["Monday", "6:00"],
    ["Tuesday", "6:00"],
    ["Wednesday", "6:00"],
    ["Thursday", "6:00"],
    ["Friday", "5:00"],
  ]);
  await expect(page.locator(".working-time-totals")).toContainText("Weekly target 29:00");
  await expect(page.locator(".working-time-totals")).toContainText("Allocated 29:00");
  await expect(page.locator(".working-time-totals")).toContainText("Remaining 0:00");
  await expect(page.getByRole("button", { name: "Save working time" })).toBeEnabled();

  // Weekend working day: adding Sunday over-allocates and blocks the save.
  await page.getByLabel("Sunday working day").check();
  await page.getByLabel("Sunday required time").fill("2:00");
  await expect(page.locator(".working-time-totals")).toContainText("Remaining −2:00");
  await expect(page.getByRole("button", { name: "Save working time" })).toBeDisabled();
  await expect(page.getByText(/over-allocated/)).toBeVisible();
  await page.screenshot({
    path: `/tmp/chronobeam-shots/${testInfo.project.name}-parttime-overallocated.png`,
  });

  // Fix by reallocating Friday; then save and reload.
  await page.getByLabel("Friday required time").fill("3:00");
  await expect(page.locator(".working-time-totals")).toContainText("Remaining 0:00");
  await page.getByRole("button", { name: "Save working time" }).click();
  await expect(page.getByText(/Saved. Required time is 29:00\/week/)).toBeVisible();
  await page.screenshot({
    path: `/tmp/chronobeam-shots/${testInfo.project.name}-parttime-saved.png`,
  });

  await page.reload();
  // The panel names the arrangement by the date it starts — the maintenance key
  // — and never claims it is shown read-only (acceptance N2).
  await expect(page.getByText(/Editing the arrangement from/)).toBeVisible();
  await expect(page.getByText("set by you")).toBeVisible();
  await expect(page.getByText("29:00/week")).toBeVisible();
  await expect(page.getByLabel("Sunday required time")).toHaveValue("2:00");
});

test("helpers: even distribution with minute remainder and Use remaining at 29:15", async ({
  page,
}) => {
  const target = page.getByLabel("Weekly target");
  await target.fill("29:15");

  // All seven weekdays become working days; distribute must split 1755 minutes
  // into 5 x 4:11 + 2 x 4:10 (integer minutes only, remainder on the first days).
  for (const day of [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
  ]) {
    await page.getByLabel(`${day} working day`).check();
  }
  await page.getByRole("button", { name: "Distribute evenly" }).click();
  for (const day of ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]) {
    await expect(page.getByLabel(`${day} required time`)).toHaveValue("4:11");
  }
  for (const day of ["Saturday", "Sunday"]) {
    await expect(page.getByLabel(`${day} required time`)).toHaveValue("4:10");
  }
  await expect(page.locator(".working-time-totals")).toContainText("Remaining 0:00");

  // Zero a day, then Use remaining on that very day restores the exact sum.
  await page.getByLabel("Friday required time").fill("0:00");
  await expect(page.locator(".working-time-totals")).toContainText("Remaining 4:11");
  await expect(page.getByRole("button", { name: "Save working time" })).toBeDisabled();
  await dayCell(page, "Friday").getByRole("button", { name: "Use remaining" }).click();
  await expect(page.getByLabel("Friday required time")).toHaveValue("4:11");
  await expect(page.locator(".working-time-totals")).toContainText("Remaining 0:00");

  // Keyboard: clear and re-type a day without corrupting the total.
  await page.getByLabel("Tuesday required time").focus();
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.press("Backspace");
  await page.keyboard.type("4:11");
  await expect(page.locator(".working-time-totals")).toContainText("Remaining 0:00");
  await page.getByRole("button", { name: "Save working time" }).press("Enter");

  // Effective change is dated: saving a PAST effective-from takes effect
  // immediately; earlier dates keep the previous arrangement (mock returns it).
  await expect(page.getByText(/Saved. Required time is 29:15\/week/)).toBeVisible();
  await page.getByLabel("Effective from").fill("2026-08-01");
  // UI-029: one date convention on this card — the German `DD.MM.YYYY` that
  // the `Effective from` field beside it already uses.
  await expect(page.getByText(/Takes effect retroactively from 01\.08\.2026/)).toBeVisible();
  await page.getByRole("button", { name: "Save working time" }).click();
  await page.reload();
  // The panel names the arrangement by the date it starts — the maintenance key
  // — and never claims it is shown read-only (acceptance N2).
  await expect(page.getByText(/Editing the arrangement from/)).toBeVisible();
  await expect(page.getByText(/29:15\/week/)).toBeVisible();
  await expect(page.getByText("set by you")).toBeVisible();
});

test("full-time 39:00 via Distribute evenly, from the keyboard", async ({ page }, testInfo) => {
  await page.getByLabel("Weekly target").fill("39:00");
  await page.getByRole("button", { name: "Distribute evenly" }).click();
  for (const day of ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]) {
    await expect(page.getByLabel(`${day} required time`)).toHaveValue("7:48");
  }
  // Saturday stays a non-working day: disabled with an empty (0:00) value.
  const saturday = page.getByLabel("Saturday required time");
  await expect(saturday).toBeDisabled();
  await expect(saturday).toHaveValue("");
  await expect(page.locator(".working-time-totals")).toContainText("Remaining 0:00");
  await page.getByRole("button", { name: "Save working time" }).click();
  await expect(page.getByText(/Saved. Required time is 39:00\/week/)).toBeVisible();
  await page.reload();
  // The panel names the arrangement by the date it starts — the maintenance key
  // — and never claims it is shown read-only (acceptance N2).
  await expect(page.getByText(/Editing the arrangement from/)).toBeVisible();
  await expect(page.getByText("39:00/week")).toBeVisible();
  await expect(page.getByLabel("Friday required time")).toHaveValue("7:48");
  await page.screenshot({ path: `/tmp/chronobeam-shots/${testInfo.project.name}-fulltime.png` });
});

test("layout does not overflow and stays operable on this viewport", async ({ page }) => {
  await page.getByLabel("Weekly target").fill("29:00");
  await page.getByRole("button", { name: "Distribute evenly" }).click();
  const overflow = await page.evaluate(
    "document.documentElement.scrollWidth > window.innerWidth + 1",
  );
  expect(overflow).toBe(false);
  const saveVisible = await page.getByRole("button", { name: "Save working time" }).isVisible();
  expect(saveVisible).toBe(true);
});
