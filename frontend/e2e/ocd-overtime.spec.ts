import { expect, test, type Page, type Route } from "@playwright/test";

/**
 * PHASE-6 OCD assignments/calls and overtime views (FE-SCREEN-004 + overtime
 * entry/election). API is mocked statefully so add/link/save behave like the
 * real service. Desktop and mobile projects both run.
 */

let calls: Array<Record<string, unknown>> = [];
let authorizations: Array<Record<string, unknown>> = [];
let linked: Record<string, string | null> = {};

async function mockApi(route: Route): Promise<void> {
  const request = route.request();
  const url = request.url();
  const method = request.method();
  const json = (body: unknown, status = 200) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

  if (url.includes("/api/v1/me/ocd-calls") && method === "POST") {
    const body = request.postDataJSON() as Record<string, unknown>;
    const call = {
      id: `call-${calls.length + 1}`,
      mode: body.mode,
      call_start_at: "2026-07-19T23:42:00+02:00",
      call_end_at: "2026-07-20T00:18:00+02:00",
      travel_out_minutes: body.mode === "ON_SITE" ? (body.travel_out_minutes ?? null) : null,
      travel_back_minutes: body.mode === "ON_SITE" ? (body.travel_back_minutes ?? null) : null,
      comment: body.comment ?? null,
      status: "RECORDED",
      version: 1,
      linked_interval_id: `interval-${calls.length + 1}`,
      span_start_at: "2026-07-19T23:17:00+02:00",
      span_end_at: "2026-07-20T00:43:00+02:00",
      elapsed_seconds: 5160,
    };
    calls.push(call);
    await json(call, 200);
    return;
  }

  if (url.includes("/api/v1/me/ocd?from=")) {
    await json([
      {
        id: "assignment-1",
        start_at: "2026-07-19T19:00:00+02:00",
        end_at: "2026-07-20T08:00:00+02:00",
        status: "SCHEDULED",
        scheduled_at: "2026-06-01T10:00:00+02:00",
        source: "PILOT",
        calls,
        findings: [
          {
            rule_id: "OCD-002",
            status: "INFO",
            message: "Assignment agreed more than 4 weeks in advance.",
          },
        ],
      },
    ]);
    return;
  }

  if (url.includes("/compensation-preview")) {
    await json({
      report_type: "OCD_COMPENSATION",
      policy_version: "2026-ocd-v1",
      policy_status: "CONFIRMED",
      employee_identifier: "emp-0001",
      ocd_assignment_id: "assignment-1",
      assignment_start_at: "2026-07-19T19:00:00+02:00",
      assignment_end_at: "2026-07-20T08:00:00+02:00",
      calls: calls.map((call) => ({
        call_id: call.id,
        mode: call.mode,
        call_start_at: call.call_start_at,
        call_end_at: call.call_end_at,
        travel_out_minutes: call.travel_out_minutes,
        travel_back_minutes: call.travel_back_minutes,
        span_start_at: call.span_start_at,
        span_end_at: call.span_end_at,
        elapsed_seconds: call.elapsed_seconds,
      })),
      classified_components: [
        {
          rule_id: "OCD-006",
          component: "OCD_CALL_BASE",
          minutes: 86,
          time_equivalent_minutes: 86,
        },
      ],
      duty: {
        duty_minutes: 780,
        duty_kind: "DUTY_GE_12H",
        units: 4,
        basis_points: null,
        short_notice: null,
      },
      election: {
        mode: "PAY",
        version: 1,
        selected_at: "2026-07-19T12:00:00+02:00",
        blocked: false,
      },
      findings: [],
    });
    return;
  }

  if (url.includes("/overtime/preview")) {
    await json({
      report_type: "OVERTIME",
      policy_version: "2026-ot-v1",
      policy_status: "CONFIRMED",
      employee_identifier: "emp-0001",
      period_start: "2026-07-01",
      period_end: "2026-08-01",
      authorizations: authorizations.map((auth) => ({
        id: auth.id,
        valid_from: auth.valid_from,
        valid_to: auth.valid_to,
        purpose: auth.purpose,
        status: auth.status,
        source_ref: auth.source_ref,
      })),
      overtime_intervals: [
        {
          interval_id: "ot-interval-1",
          start_at: "2026-07-13T18:00:00+02:00",
          end_at: "2026-07-13T20:30:00+02:00",
          work_type: "OVERTIME_PAID",
          elapsed_seconds: 9000,
          linked_authorization_id: linked["ot-interval-1"] ?? null,
          version: 1,
        },
        {
          interval_id: "ot-interval-2",
          start_at: "2026-07-14T19:00:00+02:00",
          end_at: "2026-07-14T20:00:00+02:00",
          work_type: "OVERTIME_TIME_COMP",
          elapsed_seconds: 3600,
          linked_authorization_id: null,
          version: 1,
        },
      ],
      classified_components: [
        {
          rule_id: "ALLOW-002",
          component: "OVERTIME_SUPPLEMENT",
          minutes: 150,
          time_equivalent_minutes: null,
        },
        { rule_id: "ALLOW-003", component: "NIGHT_20", minutes: 90, time_equivalent_minutes: 18 },
      ],
      election: { mode: null, version: null, selected_at: null, blocked: false },
      findings: [
        {
          rule_id: "ACCESS-006",
          status: "BLOCKED_POLICY",
          message: "Overtime without an authorization reference cannot be submitted.",
        },
      ],
    });
    return;
  }

  if (url.includes("/overtime-authorizations") && method === "POST") {
    const body = request.postDataJSON() as Record<string, unknown>;
    // An employee POST is a REQUEST: the payload carries no status, no
    // approver and no source (the server refuses them, ACCESS-017).
    const auth = {
      id: `auth-${authorizations.length + 1}`,
      valid_from: body.valid_from,
      valid_to: body.valid_to ?? null,
      purpose: body.purpose ?? null,
      status: "REQUESTED",
      authorized_by: null,
      source_ref: body.source_ref ?? null,
      source: "EMPLOYEE_RECORDED",
      version: 1,
    };
    authorizations.push(auth);
    await json(auth, 200);
    return;
  }

  if (url.includes("/overtime-authorizations")) {
    await json(authorizations);
    return;
  }

  if (url.includes("/compensation-elections")) {
    if (method === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      await json(
        {
          id: "election-1",
          context_type: body.context_type,
          context_id: body.context_id,
          mode: body.mode,
          version: 1,
          selected_at: "2026-07-19T12:00:00+02:00",
        },
        200,
      );
      return;
    }
    await json(null);
    return;
  }

  if (url.includes("/work-intervals/") && method === "PATCH") {
    const body = request.postDataJSON() as Record<string, unknown>;
    const match = url.match(/work-intervals\/([^?]+)/);
    if (match && typeof body.overtime_authorization_id === "string") {
      linked[match[1]] = body.overtime_authorization_id;
    }
    await json({ id: "ot-interval-1", version: 2 });
    return;
  }

  await json({});
}

async function openOcd(page: Page): Promise<void> {
  await page.goto("/month/2026-07/ocd");
  await expect(page.getByRole("heading", { name: /On-call duty — July 2026/ })).toBeVisible();
}

async function openOvertime(page: Page): Promise<void> {
  await page.goto("/month/2026-07/overtime");
  await expect(page.getByRole("heading", { name: /Overtime — July 2026/ })).toBeVisible();
}

test.beforeEach(async ({ page }) => {
  calls = [];
  authorizations = [];
  linked = {};
  await page.route("**/api/v1/**", mockApi);
});

test("OCD: assignment shown as availability; recording a call creates effective work", async ({
  page,
}) => {
  await openOcd(page);
  const card = page.getByLabel(/^OCD assignment 19 Jul/);
  await expect(card).toContainText("Availability");
  await expect(card).toContainText("No calls recorded");
  await expect(card).toContainText("Assignment agreed more than 4 weeks in advance.");

  await page.getByRole("button", { name: "Add call" }).click();
  await page.getByLabel("Mode").selectOption("ON_SITE");
  await page.getByLabel("Call start").fill("2026-07-19T23:42");
  await page.getByLabel("Call end").fill("2026-07-20T00:18");
  await page.getByLabel(/Travel to site/).fill("25");
  await page.getByLabel(/Travel back/).fill("25");
  await page.getByRole("button", { name: "Record call + work time" }).click();
  await expect(page.locator(".status-ok")).toContainText(
    "Call recorded together with its work time",
  );
  await expect(card).toContainText("On-site · travel 25 + 25 min");
  await expect(card).toContainText("23:42");
});

test("OCD: private compensation preview with duty classification and election", async ({
  page,
}) => {
  await openOcd(page);
  await page.getByRole("button", { name: "Preview OCD compensation" }).click();
  await expect(page.getByText("OCD compensation preview — not shared")).toBeVisible();
  await expect(page.getByText("4 hourly-wage unit(s)")).toBeVisible();
  await expect(page.getByText("Call work")).toBeVisible();
  await expect(page.getByText("No monetary amount is calculated")).toBeVisible();

  await page
    .getByRole("group", { name: "Compensation election" })
    .getByRole("radio", { name: "Time compensation" })
    .check();
  await expect(page.locator(".status-ok")).toContainText("Compensation choice saved.");
});

test("overtime: authorization reference, classification preview and interval linking", async ({
  page,
}) => {
  await openOvertime(page);
  await page.getByRole("button", { name: "Show / refresh preview" }).click();
  await expect(page.getByText("Overtime preview — not shared")).toBeVisible();
  await expect(page.getByText("Overtime supplement")).toBeVisible();
  await expect(page.getByText("Overtime not covered by an institutional approval")).toBeVisible();
  await expect(page.getByText("no authorization reference").first()).toBeVisible();

  await page.getByRole("button", { name: "Add authorization reference" }).click();
  await page.getByLabel("Valid from").fill("2026-07-01T00:00");
  await page.getByLabel("Purpose").fill("Beamtime support");
  await page.getByLabel("Source / reference").fill("ref-2026-07");
  await page.getByRole("button", { name: "Save reference" }).click();
  await expect(page.locator(".status-ok")).toContainText("Authorization reference recorded");

  await page.getByRole("button", { name: "Show / refresh preview" }).click();
  const linkSelect = page.getByLabel(/Authorization for interval/).first();
  await linkSelect.selectOption("auth-1");
  await page.getByRole("button", { name: "Link reference" }).first().click();
  await expect(page.locator(".status-ok, .status-error").last()).toContainText(
    "Authorization reference linked",
  );
});
