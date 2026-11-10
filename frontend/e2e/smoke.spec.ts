import { expect, test } from "@playwright/test";

test("frontend shell renders the Chronobeam brand", async ({ page }) => {
  // The shell's privacy footer is capability-gated (PHASE-7): the employee
  // principal derives OWN_LEDGER_READ from its row, so mock the identity card.
  await page.route("**/api/v1/me", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        subject: "employee",
        display_name: "Employee A",
        capabilities: [
          "OWN_LEDGER_READ",
          "OWN_LEDGER_WRITE",
          "OWN_SUBMISSION_CREATE",
          "OWN_DISCLOSURE_GRANT",
        ],
      }),
    }),
  );
  await page.goto("/");
  // Brand lives in the header wordmark; the home heading is persona-aware.
  await expect(page.getByRole("img", { name: "Chronobeam" })).toBeVisible();
  await expect(page.getByRole("heading", { level: 1 })).toContainText("Welcome back");
  // 7B: the privacy reassurance now lives in the page eyebrow, not the footer —
  // the footer sentence was the third statement of one fact on this screen.
  await expect(page.getByText("Private — visible only to you")).toBeVisible();
});
