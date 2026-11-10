import { expect, test, type Page } from "@playwright/test";

/**
 * PHASE-5B daily-entry UX. Runs on all four viewports (1440×900, 1280×800,
 * 390×844, 360×800) unless a test is explicitly mobile/desktop-specific.
 * Mutations are recorded so the spec can verify exact request payloads
 * (idempotent mocks keep reloads deterministic).
 */

const SHOT_DIR = "/tmp/chronobeam-shots-5b";

const sixJuly = [
  ["08:00", "10:00", "ON_SITE"],
  ["10:15", "12:00", "ON_SITE"],
  ["12:30", "14:00", "REMOTE"],
  ["14:15", "17:00", "REMOTE"],
].map(([start, end, work_type], index) => {
  const [sh, sm] = start.split(":").map(Number);
  const [eh, em] = end.split(":").map(Number);
  return {
    id: `interval-${index}`,
    work_type,
    start_at: `2026-07-06T${start}:00+02:00`,
    end_at: `2026-07-06T${end}:00+02:00`,
    start_local: `2026-07-06T${start}:00`,
    end_local: `2026-07-06T${end}:00`,
    elapsed_seconds: (eh * 60 + em - (sh * 60 + sm)) * 60,
    comment: index === 0 ? "Team sync" : null,
    version: 1,
  };
});

const sevenJuly = [
  ["09:00", "11:00", "ON_SITE"],
  ["13:00", "15:00", "REMOTE"],
].map(([start, end, work_type], index) => ({
  id: `july7-${index}`,
  work_type,
  start_at: `2026-07-07T${start}:00+02:00`,
  end_at: `2026-07-07T${end}:00+02:00`,
  start_local: `2026-07-07T${start}:00`,
  end_local: `2026-07-07T${end}:00`,
  elapsed_seconds: 3600,
  comment: null,
  version: 1,
}));

type Recorded = { method: string; url: string; body: unknown };
const recorded: Recorded[] = [];

test.beforeEach(async ({ page }) => {
  recorded.length = 0;
  await page.route("**/api/v1/me/**", async (route) => {
    const method = route.request().method();
    const url = route.request().url();
    const postBody = route.request().postData();
    if (method !== "GET") {
      recorded.push({ method, url, body: postBody ? JSON.parse(postBody) : null });
    }
    let body: unknown = {};
    if (url.includes("/findings")) {
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
    } else if (url.includes("/profile")) {
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
    } else if (url.includes("/shifts")) {
      body = [];
    } else if (url.includes("/week-report")) {
      body = [];
    } else if (url.includes("/days/2026-07-06")) {
      body = { date: "2026-07-06", intervals: sixJuly };
    } else if (url.includes("/days/2026-07-07")) {
      body = { date: "2026-07-07", intervals: sevenJuly };
    } else if (url.includes("/days/")) {
      body = { date: "2026-07-01", intervals: [] };
    } else if (url.includes("/months/2026-07")) {
      body = {
        period_month: "2026-07-01",
        status: "OPEN",
        interval_count: 6,
        closing_balance_minutes: 18,
        days: [
          { date: "2026-07-05", interval_count: 2 },
          { date: "2026-07-06", interval_count: 4 },
          { date: "2026-07-07", interval_count: 2 },
        ],
        daily_results: [
          {
            date: "2026-07-06",
            worked_minutes: 480,
            required_minutes: 348,
            delta_minutes: 132,
            calendar_class: "WEEKDAY",
            account_credit_minutes: 480,
          },
          {
            date: "2026-07-07",
            worked_minutes: 240,
            required_minutes: 348,
            delta_minutes: -108,
            calendar_class: "WEEKDAY",
            account_credit_minutes: 240,
          },
        ],
      };
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
});

async function openSixJuly(page: Page) {
  await page.goto("/month/2026-07");
  await page.getByRole("button", { name: /^Monday, 6 July 2026/ }).click();
  await expect(page.getByText("08:00–10:00")).toBeVisible();
}

test("compact day workspace: totals first, timeline, breaks, finding pin, 24h times", async ({
  page,
}) => {
  await openSixJuly(page);
  // Server-derived summary sits above the interval list.
  const totals = page.getByLabel("Server-calculated day totals");
  await expect(totals).toContainText("Worked");
  await expect(totals).toContainText("8:00");
  await expect(totals).toContainText("5:48");
  await expect(totals).toContainText("+2:12");
  // No permanently expanded forms.
  await expect(page.locator("fieldset")).toHaveCount(0);
  // Four compact rows with 24-hour times and durations.
  for (const time of ["08:00–10:00", "10:15–12:00", "12:30–14:00", "14:15–17:00"]) {
    await expect(page.getByText(time)).toBeVisible();
  }
  await expect(page.getByText("1:45")).toBeVisible();
  await expect(page.getByText("2:45")).toBeVisible();
  // Mixed work types.
  await expect(page.getByText("On-site")).toHaveCount(2);
  await expect(page.getByText("Remote")).toHaveCount(2);
  // Note indicator only where a note exists.
  await expect(page.getByText("Team sync")).toBeVisible();
  // Derived break labels between the positive gaps.
  await expect(page.getByText("15 min break")).toHaveCount(2);
  await expect(page.getByText("30 min break")).toBeVisible();
  // Finding pin near the totals: a single finding is shown fully by the
  // summary (the repeated detail list stays hidden — one presentation).
  await expect(page.getByText("⚠ Rest period below required minimum.")).toBeVisible();
  await expect(page.locator("details.finding-pin.single")).toHaveCount(1);
  // 24-hour presentation: no AM/PM anywhere on this page (word-bounded so
  // "Chronobeam" does not match the "am" suffix).
  await expect(page.getByText(/\b(?:AM|PM)\b/i)).toHaveCount(0);
});

test("editing is on demand: open, edit start/type/note, save, editor closes", async ({ page }) => {
  await openSixJuly(page);
  await page.getByRole("button", { name: "Edit interval 08:00–10:00" }).click();
  const editor = page.locator("fieldset");
  await expect(editor).toHaveCount(1);
  await expect(page.getByText("Edit interval")).toBeVisible();
  // Existing note is shown compactly and editable.
  await expect(page.getByLabel("Note")).toHaveValue("Team sync");
  await page.getByLabel("Start time").fill("08:30");
  await page.getByLabel("Work type").selectOption("REMOTE");
  await page.getByLabel("Note").fill("Team sync, moved");
  await page.screenshot({ path: `${SHOT_DIR}/${test.info().project.name}-editor-open.png` });
  await page.getByRole("button", { name: "Save" }).click();
  await expect(page.locator("fieldset")).toHaveCount(0);
  await expect(page.getByText("Interval updated.")).toBeVisible();
  const patch = recorded.find(
    (entry) => entry.method === "PATCH" && entry.url.includes("/work-intervals/interval-0"),
  );
  expect(patch).toBeTruthy();
  expect(patch?.body).toMatchObject({
    version: 1,
    start_local: "2026-07-06T08:30:00",
    end_local: "2026-07-06T10:00:00",
    work_type: "REMOTE",
    comment: "Team sync, moved",
  });
});

test("cancel discards unsaved edits without calling the API", async ({ page }) => {
  await openSixJuly(page);
  await page.getByRole("button", { name: "Edit interval 08:00–10:00" }).click();
  await page.getByLabel("End time").fill("11:00");
  await page.getByRole("button", { name: "Cancel" }).click();
  await expect(page.locator("fieldset")).toHaveCount(0);
  expect(recorded.filter((entry) => entry.url.includes("/work-intervals/interval-0"))).toHaveLength(
    0,
  );
});

test("add interval: single primary action, intelligent defaults, explicit confirmation", async ({
  page,
}) => {
  await openSixJuly(page);
  await expect(page.getByRole("button", { name: "+ Add interval" })).toBeVisible();
  if (test.info().project.name.startsWith("mobile")) {
    // On mobile the calendar stacks above the day workspace; re-scroll so the
    // screenshot actually shows the day view.
    await page.locator(".day-panel").scrollIntoViewIfNeeded();
  }
  await page.screenshot({ path: `${SHOT_DIR}/${test.info().project.name}-day-view.png` });
  await page.getByRole("button", { name: "+ Add interval" }).click();
  const add = page.locator("fieldset");
  await expect(add).toHaveCount(1);
  // Defaults: start = end of the latest interval, work type from the day.
  await expect(page.getByLabel("New start time")).toHaveValue("17:00");
  await expect(page.getByLabel("New work type")).toHaveValue("REMOTE");
  // End is never invented; Add stays disabled until entered.
  await expect(page.getByRole("button", { name: "Add", exact: true })).toBeDisabled();
  await page.getByLabel("New end time").fill("18:00");
  await page.screenshot({ path: `${SHOT_DIR}/${test.info().project.name}-add-form.png` });
  await page.getByRole("button", { name: "Add", exact: true }).click();
  await expect(page.getByText("Interval saved.")).toBeVisible();
  const post = recorded.find(
    (entry) =>
      entry.method === "POST" &&
      entry.url.includes("/work-intervals") &&
      !entry.url.includes("/split"),
  );
  expect(post).toBeTruthy();
  expect(post?.body).toMatchObject({
    start_local: "2026-07-06T17:00:00",
    end_local: "2026-07-06T18:00:00",
    work_type: "REMOTE",
  });
  // A successful Add returns the workspace to its compact normal state. A
  // second entry must start from the refreshed latest interval, not stale form
  // values that would overlap the record just created.
  await expect(page.locator("fieldset")).toHaveCount(0);
});

test("duplicate, split and delete stay behind the overflow menu", async ({ page }) => {
  await openSixJuly(page);
  await expect(page.getByRole("button", { name: "Duplicate" })).toHaveCount(0);
  // Duplicate.
  await page.getByRole("button", { name: "Interval actions for 08:00–10:00" }).click();
  await page.getByRole("button", { name: "Duplicate" }).click();
  let post = recorded.find(
    (entry) =>
      entry.method === "POST" &&
      entry.url.includes("/work-intervals") &&
      !entry.url.includes("/split"),
  );
  expect(post).toBeTruthy();
  expect(post?.body).toMatchObject({
    start_local: "2026-07-06T10:00",
    end_local: "2026-07-06T12:00",
    work_type: "ON_SITE",
  });
  // Split opens the focused editor with split controls only.
  await page.getByRole("button", { name: "Interval actions for 08:00–10:00" }).click();
  await page.getByRole("button", { name: "Split" }).click();
  await expect(page.locator("fieldset")).toHaveCount(1);
  await expect(page.getByText("Edit interval")).toBeVisible();
  await page.getByLabel("Split interval at").fill("09:00");
  await page.getByRole("button", { name: "Split here" }).click();
  await expect(page.locator("fieldset")).toHaveCount(0);
  await expect(page.getByText("Interval split.")).toBeVisible();
  const split = recorded.find(
    (entry) => entry.method === "POST" && entry.url.includes("/work-intervals/interval-0/split"),
  );
  expect(split).toBeTruthy();
  expect(split?.body).toMatchObject({ version: 1, split_local: "2026-07-06T09:00:00" });
  // Delete via the menu (Menu open state screenshot on mobile first).
  await page.getByRole("button", { name: "Interval actions for 12:30–14:00" }).click();
  await page.screenshot({ path: `${SHOT_DIR}/${test.info().project.name}-menu-open.png` });
  await page.getByRole("button", { name: "Delete" }).click();
  // Two steps (UI-006): the first click only arms, so nothing is sent yet.
  await expect(page.getByRole("button", { name: "Delete interval" })).toBeVisible();
  await expect(page.getByText("Interval deleted.")).not.toBeVisible();
  await page.screenshot({ path: `${SHOT_DIR}/${test.info().project.name}-delete-armed.png` });
  await page.getByRole("button", { name: "Delete interval" }).click();
  await expect(page.getByText("Interval deleted.")).toBeVisible();
  const remove = recorded.find(
    (entry) => entry.method === "DELETE" && entry.url.includes("/work-intervals/interval-2"),
  );
  expect(remove).toBeTruthy();
});

test("quick actions: Copy yesterday and Apply normal day; More… holds the week preview", async ({
  page,
}) => {
  await openSixJuly(page);
  await page.getByRole("button", { name: "Copy yesterday" }).click();
  await expect(page.getByText("Yesterday’s intervals copied.")).toBeVisible();
  const copy = recorded.find((entry) => entry.url.includes("/copy-previous"));
  expect(copy).toBeTruthy();
  await page.getByRole("button", { name: "Apply normal day" }).click();
  const apply = recorded.find((entry) => entry.url.includes("/apply-template"));
  expect(apply).toBeTruthy();
  expect(apply?.body).toMatchObject({ dates: ["2026-07-06"] });
  // Rare month-level flow lives behind More…, not as a full-width control.
  await page.getByRole("button", { name: "More actions" }).click();
  await page.getByRole("button", { name: "Preview normal work week" }).click();
  await expect(page.getByText("Preview before creating actual work")).toBeVisible();
  await page.getByRole("button", { name: "Apply to these days" }).click();
});

test("keyboard navigation: Enter opens the editor, Escape closes it", async ({ page }) => {
  await openSixJuly(page);
  await page.getByRole("button", { name: "Edit interval 10:15–12:00" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("fieldset")).toHaveCount(1);
  await page.keyboard.press("Escape");
  await expect(page.locator("fieldset")).toHaveCount(0);
  // Tab moves from the row menu to the follow-up controls.
  await page.getByRole("button", { name: "Edit interval 10:15–12:00" }).focus();
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("button", { name: "Interval actions for 10:15–12:00" }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Duplicate" })).toBeFocused();
  await page.keyboard.press("Escape");
});

test("selecting another day switches the compact workspace (server-driven state)", async ({
  page,
}) => {
  await openSixJuly(page);
  await page.getByRole("button", { name: /7.*2 intervals/i }).click();
  await expect(page.getByText("09:00–11:00")).toBeVisible();
  await expect(page.getByLabel("Server-calculated day totals")).toContainText("−1:48");
  await expect(page.getByText("Tue, 7 July")).toBeVisible();
});

test("no horizontal overflow and no clipped controls on any viewport", async ({ page }) => {
  await openSixJuly(page);
  const overflow = await page.evaluate(
    "document.documentElement.scrollWidth - document.documentElement.clientWidth",
  );
  expect(overflow).toBeLessThanOrEqual(1);
  // With an open editor and an open inline actions bar, nothing is clipped
  // horizontally, the bar is fully reachable, and it pushes the following
  // row instead of covering it (the day workspace itself may scroll).
  await page.getByRole("button", { name: "Edit interval 08:00–10:00" }).click();
  await page.getByRole("button", { name: "Interval actions for 10:15–12:00" }).click();
  await page.locator(".row-menu-inline").scrollIntoViewIfNeeded();
  const boxes = (await page.evaluate(`(() => {
    const editor = document.querySelector("fieldset")?.getBoundingClientRect();
    const actions = document.querySelector(".row-menu-inline")?.getBoundingClientRect();
    const rows = [...document.querySelectorAll(".interval-row")];
    const openIndex = rows.findIndex((row) => row.classList.contains("row-actions-open"));
    const next = openIndex >= 0 && openIndex + 1 < rows.length ? rows[openIndex + 1].getBoundingClientRect() : null;
    return {
      editorRight: editor ? editor.right : 0,
      actionsRight: actions ? actions.right : 0,
      actionsTop: actions ? actions.top : 0,
      actionsBottom: actions ? actions.bottom : 0,
      nextRowTop: next ? next.top : null,
      viewport: window.innerWidth,
      viewportHeight: window.innerHeight,
    };
  })()`)) as {
    editorRight: number;
    actionsRight: number;
    actionsTop: number;
    actionsBottom: number;
    nextRowTop: number | null;
    viewport: number;
    viewportHeight: number;
  };
  expect(boxes.editorRight).toBeLessThanOrEqual(boxes.viewport + 1);
  expect(boxes.actionsRight).toBeLessThanOrEqual(boxes.viewport + 1);
  expect(boxes.actionsBottom).toBeLessThanOrEqual(boxes.viewportHeight + 1);
  // In-flow push, not an overlay: the row below starts at/after the bar.
  expect(boxes.nextRowTop).not.toBeNull();
  expect(boxes.nextRowTop as number).toBeGreaterThanOrEqual(boxes.actionsBottom - 1);
});

test("reload restores the compact day state from the server", async ({ page }) => {
  await openSixJuly(page);
  await page.reload();
  await page.getByRole("button", { name: /^Monday, 6 July 2026/ }).click();
  await expect(page.getByText("08:00–10:00")).toBeVisible();
  await expect(page.locator("fieldset")).toHaveCount(0);
});

test("desktop calendar keeps richer cells; mobile cells are compact and scanable", async ({
  page,
}, testInfo) => {
  const isMobile = testInfo.project.name.startsWith("mobile");
  await openSixJuly(page);
  const cell = page.getByRole("button", { name: /^Monday, 6 July 2026/ });
  // The delta is the work indicator at BOTH densities. The retired `.cell-dot`
  // duplicated it, and a day that has work but no plan and no day status has no
  // event bar at all — its delta is the whole statement about the day, so this is
  // the assertion that holds for a plain worked day. The bar's own two-density
  // encoding is pinned in plan-entry.spec.ts, which has a real planned shift.
  await expect(cell.locator(".cell-delta")).toBeVisible();
  if (isMobile) {
    // The phone density renders the finding as a GLYPH, not a word: the severity
    // text is collapsed to `font-size: 0` and a dot (WARNING) or hollow ring
    // (CRITICAL/BLOCKED_POLICY) is painted in its place, so severity survives
    // without the word and without relying on colour. The old assertion was
    // `toBeHidden()`, which stopped describing the design the moment the word was
    // collapsed instead of the element being removed.
    await expect(cell.locator(".cell-finding")).toHaveCSS("font-size", "0px");
    await expect(cell).toHaveAttribute("aria-label", /4 intervals/);
    // Re-scroll: clicking the day cell auto-scrolled the day panel into view.
    await page.locator(".calendar-panel").scrollIntoViewIfNeeded();
    await page.screenshot({ path: `${SHOT_DIR}/${testInfo.project.name}-months-view.png` });
  } else {
    await expect(cell.locator(".cell-finding")).toBeVisible();
  }
});
