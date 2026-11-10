import { expect, test, type Route } from "@playwright/test";

/**
 * PHASE-7 employee-controlled disclosure journeys (FE-SCREEN-007/008/009).
 * API is mocked statefully against the PHASE-7 wire contract:
 *
 *   GET  /api/v1/me                      -> identity (drives HR vs employee UI)
 *   POST /api/v1/hr/access-requests      -> request row (metadata only)
 *   GET  /api/v1/hr/access-requests      -> requester's own requests (+ grant_id pulse)
 *   GET  /api/v1/me/access-requests      -> requests for the employee
 *   POST /api/v1/me/access-requests/{id}/grant
 *   GET  /api/v1/disclosures/{grant_id}  -> frozen projection (grantee only)
 *   GET/POST /api/v1/me/submissions      -> own packages / confirm with fingerprint
 *   GET  /api/v1/me/submission-recipients -> selectable recipients (registry)
 *   GET  /api/v1/recipient/submissions   -> assigned queue
 *
 * Four journeys: HR request reveals nothing and the grant opens exactly one
 * month; the employee reviews then grants; overtime submission previews the
 * exact payload and fingerprint drift forces re-preview; the recipient queue
 * shows only the minimized frozen package.
 */

let principal = "employee";
let requests: Array<Record<string, unknown>> = [];
let grants: Array<Record<string, unknown>> = [];
let packages: Array<Record<string, unknown>> = [];
let previewFingerprint = "";
let packageFingerprint = "";

function json(route: Route, body: unknown, status = 200): Promise<void> {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function makeRequest(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: `request-${requests.length + 1}`,
    requester_principal: "hr-requester",
    purpose: "RANDOM_CHECK",
    period_start: "2026-07-01",
    period_end: "2026-07-31",
    view_type: "work-time-record",
    message: "Quarterly random working-time check.",
    status: "PENDING",
    created_at: "2026-07-10T09:00:00+02:00",
    closed_at: null,
    grant_id: null,
    ...overrides,
  };
}

function makeGrant(request: Record<string, unknown>): Record<string, unknown> {
  return {
    id: `grant-${grants.length + 1}`,
    access_request_id: request.id,
    employee_identifier: "emp-0001",
    grantee_principal: "hr-requester",
    purpose: request.purpose,
    period_start: request.period_start,
    period_end: request.period_end,
    view_type: request.view_type,
    granted_at: "2026-07-11T10:00:00+02:00",
    expires_at: null,
    supersedes_id: null,
    active: true,
  };
}

const frozenView = {
  grant_id: "grant-1",
  purpose: "RANDOM_CHECK",
  period_start: "2026-07-01",
  period_end: "2026-07-31",
  view_type: "work-time-record",
  granted_at: "2026-07-11T10:00:00+02:00",
  snapshot: {
    employee_identifier: "emp-0001",
    period_start: "2026-07-01",
    period_end: "2026-07-31",
    generated_at: "2026-07-11T10:00:00+02:00",
    daily: [
      { date: "2026-07-06", worked_minutes: 480, required_minutes: 348, delta_minutes: 132 },
      { date: "2026-07-07", worked_minutes: 240, required_minutes: 348, delta_minutes: -108 },
    ],
    intervals: [
      {
        start_at: "2026-07-06T08:00:00+02:00",
        end_at: "2026-07-06T16:00:00+02:00",
        work_type: "ON_SITE",
        elapsed_seconds: 28800,
      },
    ],
  },
};

function makePackage(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: `package-${packages.length + 1}`,
    purpose: "OVERTIME_PAID",
    period_start: "2026-07-01",
    period_end: "2026-07-31",
    version: 1,
    status: "SUBMITTED",
    supersedes_id: null,
    created_at: "2026-07-11T11:00:00+02:00",
    employee_confirmed_at: "2026-07-11T11:00:00+02:00",
    recipients: [
      { recipient_principal: "hr-requester", recipient_type: "RECIPIENT", read_at: null },
    ],
    payload: {
      report_type: "OVERTIME",
      policy_version: "2026-ot-v1",
      policy_status: "CONFIRMED",
      employee_identifier: "emp-0001",
      period_start: "2026-07-01",
      period_end: "2026-07-31",
      authorizations: [
        {
          id: "auth-1",
          valid_from: "2026-07-01T00:00:00+02:00",
          valid_to: null,
          purpose: "Beamtime support",
          status: "REQUESTED",
          source_ref: "ref-2026-07",
        },
      ],
      overtime_intervals: [
        {
          interval_id: "ot-interval-1",
          start_at: "2026-07-13T18:00:00+02:00",
          end_at: "2026-07-13T20:30:00+02:00",
          work_type: "OVERTIME_PAID",
          elapsed_seconds: 9000,
          linked_authorization_id: "auth-1",
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
      election: {
        mode: "PAY",
        version: 1,
        selected_at: "2026-07-11T10:30:00+02:00",
        blocked: false,
      },
    },
    ...overrides,
  };
}

async function mockApi(route: Route): Promise<void> {
  const request = route.request();
  const url = request.url();
  const method = request.method();
  const body = request.postDataJSON() as Record<string, unknown> | null;

  if (url.endsWith("/api/v1/me") && method === "GET") {
    const capabilities: Record<string, string[]> = {
      employee: [
        "OWN_LEDGER_READ",
        "OWN_LEDGER_WRITE",
        "OWN_SUBMISSION_CREATE",
        "OWN_DISCLOSURE_GRANT",
      ],
      "hr-requester": ["ACCESS_REQUEST_CREATE", "DISCLOSURE_READ_GRANTED"],
      recipient: ["SUBMISSION_READ_ASSIGNED"],
    };
    await json(route, {
      subject: principal,
      display_name:
        principal === "hr-requester"
          ? "HR requester"
          : principal === "recipient"
            ? "Disclosure recipient"
            : "Employee A",
      capabilities: capabilities[principal] ?? [],
    });
    return;
  }

  if (url.includes("/api/v1/hr/access-requests") && method === "POST") {
    const created = makeRequest({
      employee_external_id: body?.employee_external_id,
      message: body?.message ?? null,
    });
    requests.push(created);
    await json(route, created);
    return;
  }
  if (url.includes("/api/v1/hr/access-requests")) {
    await json(
      route,
      requests.map((row) =>
        row.status === "GRANTED"
          ? { ...row, grant_id: grants[grants.length - 1]?.id ?? null }
          : row,
      ),
    );
    return;
  }

  if (url.includes("/api/v1/me/access-requests") && url.endsWith("/grant")) {
    const id = url.match(/access-requests\/([^/]+)\/grant/)?.[1];
    const pending = requests.find((row) => row.id === id);
    if (!pending) {
      await json(route, { detail: { code: "NOT_FOUND", message: "missing" } }, 404);
      return;
    }
    const grant = makeGrant(pending);
    grants.push(grant);
    pending.status = "GRANTED";
    pending.closed_at = grant.granted_at;
    await json(route, grant);
    return;
  }
  if (url.includes("/api/v1/me/access-requests")) {
    await json(route, requests);
    return;
  }

  if (url.includes("/api/v1/disclosures/")) {
    const id = url.match(/disclosures\/([^/]+)/)?.[1];
    const grant = grants.find((row) => row.id === id);
    if (!grant) {
      await json(route, { detail: { code: "NOT_FOUND", message: "missing" } }, 410);
      return;
    }
    await json(route, frozenView);
    return;
  }

  if (url.includes("/api/v1/me/submission-recipients")) {
    // The selectable recipients are a server fact (the registry), never a
    // client constant: nothing is pre-selected and a package is addressed to
    // exactly what the employee chose.
    await json(route, [{ subject: "demo-recipient", readable: true }]);
    return;
  }
  if (url.includes("/api/v1/me/submissions") && method === "POST") {
    if (body?.preview_fingerprint !== packageFingerprint) {
      await json(
        route,
        {
          detail: {
            code: "SUBMISSION_CHANGED",
            message: "facts changed since preview",
            details: { re_preview: true },
          },
        },
        409,
      );
      return;
    }
    const created = makePackage({
      recipients: ((body?.recipients as string[]) ?? []).map((subject) => ({
        recipient_principal: subject,
        recipient_type: "RECIPIENT",
        read_at: null,
      })),
    });
    packages.push(created);
    await json(route, created);
    return;
  }
  if (url.includes("/api/v1/me/submissions")) {
    await json(route, packages);
    return;
  }

  if (url.includes("/api/v1/recipient/submissions/")) {
    await json(route, packages[0]);
    return;
  }
  if (url.includes("/api/v1/recipient/submissions")) {
    await json(
      route,
      packages.map(({ payload: _p, recipients: _r, ...meta }) => meta),
    );
    return;
  }

  if (url.includes("/api/v1/me/shifts")) {
    await json(route, []);
    return;
  }
  if (url.includes("/api/v1/me/ocd?")) {
    await json(route, []);
    return;
  }

  if (url.includes("/overtime/preview")) {
    await json(route, {
      report_type: "OVERTIME",
      policy_version: "2026-ot-v1",
      policy_status: "CONFIRMED",
      employee_identifier: "emp-0001",
      period_start: "2026-07-01",
      period_end: "2026-08-01",
      authorizations: [
        {
          id: "auth-1",
          valid_from: "2026-07-01T00:00:00+02:00",
          valid_to: null,
          purpose: "Beamtime support",
          status: "REQUESTED",
          source_ref: "ref-2026-07",
        },
      ],
      overtime_intervals: [
        {
          interval_id: "ot-interval-1",
          start_at: "2026-07-13T18:00:00+02:00",
          end_at: "2026-07-13T20:30:00+02:00",
          work_type: "OVERTIME_PAID",
          elapsed_seconds: 9000,
          linked_authorization_id: "auth-1",
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
      election: {
        mode: "PAY",
        version: 1,
        selected_at: "2026-07-11T10:30:00+02:00",
        blocked: false,
      },
      findings: [],
      preview_fingerprint: previewFingerprint,
    });
    return;
  }

  await json(route, {});
}

test.beforeEach(async ({ page }) => {
  requests = [];
  grants = [];
  packages = [];
  principal = "employee";
  previewFingerprint = "fp-" + Math.random().toString(36).slice(2);
  packageFingerprint = previewFingerprint;
  await page.route("**/api/v1/**", mockApi);
});

test("HR random check: creating a request reveals no ledger data", async ({ page }) => {
  principal = "hr-requester";
  await page.goto("/access-requests");
  await expect(page.getByRole("heading", { name: "Access Requests" })).toBeVisible();
  await page.getByLabel("Employee ID").fill("emp-0001");
  await page.getByLabel("Month").fill("2026-07");
  await page.getByRole("button", { name: "Request random check" }).click();
  await expect(
    page.getByText("Random check · 01.07.2026–31.07.2026 · Waiting for the employee"),
  ).toBeVisible();
  // The request list is metadata only: no worked time, intervals, or totals.
  await expect(page.getByText(/worked_minutes|intervals|elapsed_seconds/i)).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Open the granted view" })).toHaveCount(0); // nothing to open while PENDING
});

test("employee reviews the month first, then opens exactly the requested scope", async ({
  page,
}) => {
  requests = [makeRequest({ id: "request-1" })];
  await page.goto("/access-requests");
  await expect(page.getByRole("heading", { name: "Access Requests" })).toBeVisible();
  await expect(page.getByText("Quarterly random working-time check.")).toBeVisible();
  await expect(page.getByRole("link", { name: "Review July 2026 first" })).toHaveAttribute(
    "href",
    "/month/2026-07",
  );
  await page.goto("/access-requests");
  await page.getByRole("button", { name: "Open exactly this scope to hr-requester" }).click();
  await expect(page.locator(".status-ok, .status-error").last()).toContainText("opened");
  await expect(page.getByText(`Requested by: hr-requester`)).toBeVisible();
});

test("HR opens the granted frozen view from the request list", async ({ page }) => {
  principal = "hr-requester";
  grants = [makeGrant(makeRequest({ id: "request-1", status: "GRANTED" }))];
  requests = [makeRequest({ id: "request-1", status: "GRANTED", grant_id: grants[0].id })];
  await page.goto("/access-requests");
  const row = page.getByRole("listitem").filter({ hasText: "Opened by the employee" });
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: "Open the granted view" }).click();
  await expect(page.getByText(/06\.07\.2026/)).toBeVisible(); // European day-first rows
  await expect(page.getByText(/06 Jul 2026, 08:00 → 06 Jul 2026/)).toBeVisible();
  // 7A: the raw JSON disclosure is gone, but the facts it carried are rendered —
  // including whose record this is, which the raw view was previously the only
  // place to show. Without it HR can attribute sensitive data to the wrong person.
  await expect(page.getByText(/Employee reference: emp-0001/)).toBeVisible();
  await expect(page.locator(".frozen-view .tech-details")).toHaveCount(0);
});

test("overtime submission previews the exact payload; drifted facts force re-preview", async ({
  page,
}) => {
  await page.goto("/month/2026-07/submissions");
  await expect(page.getByRole("heading", { name: /Submissions & Sharing/ })).toBeVisible();
  const payCard = page
    .locator("section.submission-card")
    .filter({ hasText: "Submit Overtime — Pay" });
  await expect(payCard.getByRole("heading", { name: "Submit Overtime — Pay" })).toBeVisible();
  await payCard.getByLabel("Overtime month").fill("2026-07");
  // UI-043: opening a preview must not move or resize any of the four cards.
  // Measured RELATIVE TO THE GRID in one browser call: getBoundingClientRect is
  // viewport-relative, so a page scroll would move every value by the same
  // amount and a false failure would look like a layout change.
  const measureCards = () =>
    page.locator(".submission-grid").evaluate((grid) => {
      const base = grid.getBoundingClientRect();
      return [...grid.querySelectorAll("section.submission-card")].map((node) => {
        const box = node.getBoundingClientRect();
        return [box.x - base.x, box.y - base.y, box.width, box.height];
      });
    });
  // Measure at REST. Slice 10 gives cards and panels a 160 ms entrance
  // transition, so a sample taken mid-flight differs by a sub-pixel fraction
  // (observed: y off by 0.027 px) — which is an animation, not a layout change.
  // Reduced motion neutralises every transition deterministically, and this
  // doubles as proof that the reduced-motion block covers these surfaces.
  await page.emulateMedia({ reducedMotion: "reduce" });
  const before = await measureCards();
  await payCard.getByRole("button", { name: "Preview exact package — Overtime — Pay" }).click();
  const payPanel = page.locator("section.preview-panel");
  await expect(payPanel).toBeVisible();
  const after = await measureCards();
  expect(after).toEqual(before);
  // The preview shows EXACTLY the recipient payload; private findings stay out.
  await expect(payPanel.getByText("What the recipient will see")).toBeVisible();
  await expect(
    payPanel.getByText(/Overtime intervals \(1\): 1 linked to an authorization reference/),
  ).toBeVisible();
  // The human component label carries the line; the engine code is gone (7A).
  await expect(payPanel.getByText(/Overtime supplement: /)).toBeVisible();
  await expect(payPanel.getByText(/ACCESS-006/)).toHaveCount(0);
  await expect(payPanel.locator(".tech-details")).toHaveCount(0);
  // 7A: every substantive payload field the raw JSON used to carry is rendered in
  // words — the interval's own window and duration, and the authorization
  // reference behind it. Losing these would make "What the recipient will see"
  // untrue.
  await expect(
    payPanel.getByText(/13 Jul 2026, 18:00 → 20:30 · Overtime paid · 2:30/),
  ).toBeVisible();
  // Which authorization supports WHICH interval — the payload carries the link
  // (linked_authorization_id); the numbered list makes it readable.
  await expect(payPanel.getByText(/Overtime paid · 2:30 · reference 1/)).toBeVisible();
  await expect(payPanel.getByText("Authorization references (1):")).toBeVisible();
  await expect(payPanel.getByText(/Reference 1: 01 Jul 2026, 00:00 → open-ended/)).toBeVisible();
  await expect(payPanel.getByText(/Beamtime support · ref-2026-07/)).toBeVisible();
  // Selecting a recipient is the explicit disclosure act (INV-05).
  await payCard.getByRole("checkbox", { name: "demo-recipient" }).check();
  await payPanel.getByRole("button", { name: /Submit this Overtime/ }).click();
  await expect(page.locator(".status-ok, .status-error").last()).toContainText(
    "Recipients can read only this package.",
  );
  await expect(page.getByText("Version 1 · Submitted")).toBeVisible();

  // Facts change between preview and confirm => 409 => re-preview notice.
  packageFingerprint = "fp-changed";
  await payCard.getByRole("button", { name: "Preview exact package — Overtime — Pay" }).click();
  await payPanel.getByRole("button", { name: /Submit this Overtime/ }).click();
  await expect(page.locator(".status-ok, .status-error").last()).toContainText(
    /changed|re-preview/i,
  );
});

test("recipient queue shows only the minimized frozen package", async ({ page }) => {
  principal = "recipient";
  packages = [makePackage()];
  await page.goto("/recipient-queue");
  await expect(page.getByRole("heading", { name: "Submission queue" })).toBeVisible();
  await page.getByRole("button", { name: "Open package" }).click();
  // The recipient sees the human reading of the package first...
  await expect(page.getByRole("heading", { name: "What you will receive" })).toBeVisible();
  await expect(page.getByText(/Overtime intervals \(\d+\)/)).toBeVisible();
  // The recipient receives the same complete projection, not a summary.
  await expect(page.getByText(/Employee reference: emp-0001/)).toBeVisible();
  await expect(page.getByText(/13 Jul 2026, 18:00 → 20:30 · Overtime paid · 2:30/)).toBeVisible();
  await expect(page.getByText(/Beamtime support/)).toBeVisible();
  await expect(page.locator(".recipient-detail .tech-details")).toHaveCount(0);
  // No ledger, no private findings, no comments, no employee names.
  await expect(page.getByText(/ACCESS-006|OCD-00|comment|legal_name/i)).toHaveCount(0);
});
