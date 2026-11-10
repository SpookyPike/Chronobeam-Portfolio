import { expect, test } from "@playwright/test";

const intervals = [
  ["08:00", "10:00", "ON_SITE"],
  ["10:15", "12:00", "ON_SITE"],
  ["12:30", "14:00", "REMOTE"],
  ["14:15", "17:00", "REMOTE"],
].map(([start, end, work_type], index) => ({
  id: `interval-${index}`,
  work_type,
  start_at: `2026-07-06T${start}:00+02:00`,
  end_at: `2026-07-06T${end}:00+02:00`,
  start_local: `2026-07-06T${start}:00`,
  end_local: `2026-07-06T${end}:00`,
  elapsed_seconds: 3600,
  comment: null,
  version: 1,
}));

test.beforeEach(async ({ page }) => {
  await page.route("**/api/v1/me/**", async (route) => {
    const url = route.request().url();
    let body: unknown = {};
    if (url.includes("/findings"))
      body = {
        closing_balance_minutes: 18,
        findings: [
          {
            rule_id: "WT-CORE-007",
            severity: "WARNING",
            date: "2026-07-06",
            message: "Rest period below required minimum.",
          },
        ],
      };
    else if (url.includes("/profile"))
      body = {
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
      };
    else if (url.includes("/shifts")) body = [];
    else if (url.includes("/days/2026-07-06")) body = { date: "2026-07-06", intervals };
    else if (url.includes("/days/")) body = { date: "2026-07-01", intervals: [] };
    else if (url.includes("/months/2026-07"))
      body = {
        period_month: "2026-07-01",
        status: "OPEN",
        interval_count: 4,
        closing_balance_minutes: 18,
        days: [{ date: "2026-07-06", interval_count: 4 }],
        daily_results: [
          {
            date: "2026-07-06",
            worked_minutes: 480,
            required_minutes: 348,
            delta_minutes: 132,
            calendar_class: "WEEKDAY",
            account_credit_minutes: 480,
          },
        ],
      };
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
});

test("employee records and reviews a private four-interval month at the active viewport", async ({
  page,
}) => {
  await page.goto("/month/2026-07");
  await expect(page.getByText("Private — visible only to you").first()).toBeVisible();
  await page.getByRole("button", { name: /6.*4 intervals/i }).click();
  // Compact timeline: no permanently expanded forms.
  await expect(page.locator("fieldset")).toHaveCount(0);
  await expect(page.getByText("08:00–10:00")).toBeVisible();
  await expect(page.getByText("Rest period below required minimum.").first()).toBeVisible();
  // Normal-day convenience still works from the compact quick actions.
  await page.getByRole("button", { name: "Apply normal day" }).click();
  await expect(page.getByRole("button", { name: "Apply normal day" })).toBeEnabled();
  await page.getByRole("link", { name: "Review this month" }).click();
  await expect(page.getByRole("heading", { name: "Month review" })).toBeVisible();
  await page.getByRole("checkbox", { name: /I have read these/ }).check();
  await page.getByRole("button", { name: "Mark month reviewed" }).click();
  await expect(page.getByText("Month marked reviewed. No data was shared.")).toBeVisible();
});
