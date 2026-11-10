import { expect, test, type Page, type Route } from "@playwright/test";

/**
 * PHASE-9 travel workflow (TRAVEL-001..004): create trip, add passive/active/
 * driving segments, correct facts, threshold + credit display, cross-midnight
 * entry, validation/error states, reload persistence, 24-hour time. API is
 * mocked statefully so mutations behave like the real service. Desktop and
 * mobile projects both run.
 */

interface Segment {
  id: string;
  trip_id: string;
  start_local: string;
  end_local: string;
  classification: string;
  basis: string;
  work_interval_id: string | null;
  version: number;
}

let trips: Array<Record<string, unknown>> = [];
let segments: Segment[] = [];
let windowState: { eligible: number; threshold: number; excess: number; credit: number } = {
  eligible: 200,
  threshold: 900,
  excess: 0,
  credit: 0,
};
let nextSegmentVersion = 1;

function iso(local: string): string {
  return `${local}+02:00`;
}

function summary(period: string): Record<string, unknown> {
  return {
    period_month: period,
    topups: period === "2026-05" ? [{ day: "2026-05-15", minutes: 120 }] : [],
    credits:
      // TRAVEL-004 credits are visible only in their effective month: the
      // first day after the May-15–June-15 assessment window is July 1.
      period === "2026-07" && windowState.credit > 0
        ? [
            {
              occurred_on: "2026-07-01",
              minutes: windowState.credit,
              rule_id: "TRAVEL-004",
              source_entity_type: "business_trip",
              source_entity_id: "trip-1",
              window_start: "2026-05-15",
              window_end: "2026-06-15",
              eligible_minutes: windowState.eligible,
              threshold_minutes: windowState.threshold,
              excess_minutes: windowState.excess,
            },
          ]
        : [],
    // Derived from this mock's own state rather than hardcoded: the month's trip
    // list is decided by `summary.trips` (`travel.tsx` filters the trip endpoint's
    // answer by it), so a literal entry that ignored the mock's own facts made the
    // mock consistent with nothing — which is why the suite could not catch the
    // ledger's I1 (a declared trip with no segments was invisible in every month).
    // Every declared trip is listed, segment-less included, which is what the
    // service does; the window figures stay segment-derived via `windowState`.
    trips: trips.map((trip) => {
      const start = String(trip.start_local).slice(0, 10);
      const [year, month, day] = start.split("-").map(Number);
      return {
        trip_id: String(trip.id),
        window_start: start,
        // `Date.UTC`'s month is 0-based and the parsed one is 1-based, so this is
        // the trip's own start plus one month — the window the rule uses.
        window_end: new Date(Date.UTC(year, month, day)).toISOString().slice(0, 10),
        eligible_minutes: windowState.eligible,
        threshold_minutes: windowState.threshold,
        excess_minutes: windowState.excess,
        credit_minutes: windowState.credit,
        credit_effective_on: "2026-07-01",
      };
    }),
    findings:
      period === "2026-05" &&
      segments.some((segment) => segment.classification === "DRIVING") &&
      segments.some((segment) => segment.start_local.slice(0, 10) === "2026-05-17")
        ? [
            {
              rule_id: "TRAVEL-SUNDAY-002",
              severity: "WARNING",
              date: "2026-05-17",
              message: "Sunday driving is working time and is generally prohibited; avoid it.",
            },
          ]
        : [],
    input_fingerprint: "e2e-fingerprint",
  };
}

function tripPayload(tripId: string): Record<string, unknown> {
  return {
    ...trips.find((trip) => trip.id === tripId),
    segments: segments
      .filter((segment) => segment.trip_id === tripId)
      .map((segment) => ({
        ...segment,
        start_at: iso(segment.start_local),
        end_at: iso(segment.end_local),
        supersedes_id: null,
        client_request_id: null,
      })),
  };
}

async function mockApi(route: Route): Promise<void> {
  const request = route.request();
  const url = request.url();
  const method = request.method();
  const json = (body: unknown, status = 200) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

  if (
    url.includes("/api/v1/me") &&
    method === "GET" &&
    !url.includes("months") &&
    !url.includes("travel")
  ) {
    await json({
      subject: "dev:employee",
      display_name: "Dev Employee",
      capabilities: ["OWN_LEDGER_READ", "OWN_LEDGER_WRITE"],
    });
    return;
  }
  if (url.includes("/travel-trips?") || (url.endsWith("/travel-trips") && method === "GET")) {
    await json({ trips: trips.map((trip) => tripPayload(String(trip.id))) });
    return;
  }
  if (url.endsWith("/travel-trips") && method === "POST") {
    const body = request.postDataJSON() as Record<string, string>;
    if (body.start_origin === "INVALID") {
      await json({ detail: { code: "VALIDATION_FAILED", message: "Unknown start origin." } }, 422);
      return;
    }
    const trip = {
      id: "trip-1",
      start_at: iso(body.start_local),
      end_at: iso(body.end_local),
      start_local: body.start_local.slice(0, 16),
      end_local: body.end_local.slice(0, 16),
      start_origin: body.start_origin,
      source: "EMPLOYEE_DECLARED",
      reference_label: body.reference_label ?? null,
      version: 1,
      supersedes_id: null,
      client_request_id: body.client_request_id ?? null,
    };
    trips = [trip];
    windowState = { eligible: 0, threshold: 900, excess: 0, credit: 0 };
    await json(tripPayload("trip-1"), 201);
    return;
  }
  if (url.includes("/segments") && method === "POST") {
    const body = request.postDataJSON() as Record<string, string>;
    const trip = trips[0] as { start_local?: string; end_local?: string } | undefined;
    if (
      trip &&
      (body.start_local < (trip.start_local ?? "") || body.end_local > (trip.end_local ?? ""))
    ) {
      await json(
        {
          detail: {
            code: "VALIDATION_TRIP_ENVELOPE",
            message: "Travel segments must stay inside the trip.",
          },
        },
        422,
      );
      return;
    }
    const id = `seg-${segments.length + 1}`;
    segments.push({
      id,
      trip_id: "trip-1",
      start_local: body.start_local.slice(0, 16),
      end_local: body.end_local.slice(0, 16),
      classification: body.classification,
      basis: body.basis ?? "ACTUAL",
      work_interval_id: body.classification === "PASSIVE_TRAVEL" ? null : `interval-${id}`,
      version: nextSegmentVersion++,
    });
    if (body.classification === "PASSIVE_TRAVEL") {
      const minutes = (Date.parse(body.end_local) - Date.parse(body.start_local)) / 60000;
      windowState.eligible += minutes;
      windowState.excess = Math.max(0, windowState.eligible - windowState.threshold);
      windowState.credit = Math.floor(windowState.excess / 4);
    }
    const segment = segments[segments.length - 1];
    await json(
      {
        ...segment,
        start_at: iso(segment.start_local),
        end_at: iso(segment.end_local),
        supersedes_id: null,
        client_request_id: null,
      },
      201,
    );
    return;
  }
  if (url.includes("/travel-segments/") && method === "PATCH") {
    const body = request.postDataJSON() as Record<string, string>;
    const id = url.split("/").pop()!;
    segments = segments.map((segment) =>
      segment.id === id
        ? {
            ...segment,
            start_local: (body.start_local ?? segment.start_local).slice(0, 16),
            end_local: (body.end_local ?? segment.end_local).slice(0, 16),
            version: segment.version + 1,
          }
        : segment,
    );
    const segment = segments.find((item) => item.id === id)!;
    await json({
      ...segment,
      start_at: iso(segment.start_local),
      end_at: iso(segment.end_local),
      supersedes_id: null,
      client_request_id: null,
    });
    return;
  }
  if (url.includes("/travel-summary")) {
    await json(summary(new URL(url).searchParams.get("period") ?? "2026-05"));
    return;
  }
  await json([]);
}

async function seed(page: Page): Promise<void> {
  trips = [];
  segments = [];
  nextSegmentVersion = 1;
  windowState = { eligible: 0, threshold: 900, excess: 0, credit: 0 };
  await page.route("**/api/v1/**", mockApi);
  await page.goto("/travel/2026-05");
}

test("travel workflow: create, classify, correct, threshold, credit, persistence", async ({
  page,
}, testInfo) => {
  await seed(page);

  // Create the trip.
  await page.getByRole("button", { name: /Add business trip/i }).click();
  await page.getByLabel("Trip start date").fill("2026-05-15");
  await page.getByLabel("Trip start time").fill("08:00");
  await page.getByLabel("Trip end date").fill("2026-05-17");
  await page.getByLabel("Trip end time").fill("16:00");
  await page.getByLabel("Trip start origin").selectOption("HOME");
  await page.getByRole("button", { name: "Create trip" }).click();
  await expect(page.getByText(/Business trip · Fri, 15 May/)).toBeVisible();
  await expect(page.getByText(/starts from home/)).toBeVisible();
  await expect(page.getByText("employee-declared", { exact: true })).toBeVisible();

  // Add a driving segment (actual work).
  await page
    .getByRole("button", { name: /Add segment/i })
    .first()
    .click();
  await page.getByLabel("Segment start date").fill("2026-05-15");
  await page.getByLabel("Segment start time").fill("08:00");
  await page.getByLabel("Segment end date").fill("2026-05-15");
  await page.getByLabel("Segment end time").fill("09:30");
  await page.getByLabel("Segment classification").selectOption("DRIVING");
  await expect(page.getByText(/actual work interval/i)).toBeVisible();
  await page.getByRole("button", { name: "Add segment" }).click();
  await expect(page.getByText("Driving", { exact: true })).toBeVisible();
  await expect(page.getByText(/working time in your ledger/)).toBeVisible();
  await expect(page.getByText(/08:00–09:30/)).toBeVisible();

  // Add a cross-midnight passive segment.
  await page
    .getByRole("button", { name: /Add segment/i })
    .first()
    .click();
  await page.getByLabel("Segment start date").fill("2026-05-16");
  await page.getByLabel("Segment start time").fill("22:00");
  await page.getByLabel("Segment end date").fill("2026-05-17");
  await page.getByLabel("Segment end time").fill("01:00");
  await page.getByLabel("Segment classification").selectOption("PASSIVE_TRAVEL");
  await page.getByRole("button", { name: "Add segment" }).click();
  await expect(page.getByText("Passive travel", { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/travel time, not working time/)).toBeVisible();

  // Threshold shows accumulated passive (3:00 = 180 eligible) below 15:00.
  await expect(page.getByText(/3:00 \/ 15:00 threshold/)).toBeVisible();
  await expect(page.getByText(/12:00 remaining/)).toBeVisible();
  await expect(page.getByText("25% time credit")).toHaveCount(0);

  // Add a long passive leg that crosses the threshold (16:00 total).
  await page
    .getByRole("button", { name: /Add segment/i })
    .first()
    .click();
  await page.getByLabel("Segment start date").fill("2026-05-15");
  await page.getByLabel("Segment start time").fill("09:30");
  await page.getByLabel("Segment end date").fill("2026-05-15");
  await page.getByLabel("Segment end time").fill("22:00");
  await page.getByLabel("Segment classification").selectOption("PASSIVE_TRAVEL");
  await page.getByRole("button", { name: "Add segment" }).click();
  // Past the threshold the same three figures appear as labelled fields, so the
  // running line is gone (it would restate them).
  await expect(page.getByText("Eligible non-credited travel")).toBeVisible();
  await expect(page.getByText("Excess")).toBeVisible();
  await expect(page.getByText("0:30", { exact: true })).toBeVisible();
  await expect(page.getByText(/over the threshold/)).toHaveCount(0);
  await expect(page.getByText("25% time credit")).toBeVisible();
  await expect(page.getByText("0:07", { exact: true }).first()).toBeVisible();

  // The May card forecasts the credit, but the derived ledger proposal is
  // actually shown only in its effective month (TRAVEL-004).
  await page.getByRole("link", { name: "Next month" }).click();
  await page.getByRole("link", { name: "Next month" }).click();
  await expect(page.getByText("Generated travel credit · July 2026")).toBeVisible();
  // The day and its credit are separate fields, not one sentence.
  const credits = page.getByLabel("Generated travel credits");
  await expect(credits.getByText("Wed, 1 July")).toBeVisible();
  await expect(credits.getByText("+0:07")).toBeVisible();

  // Correction requires a reason and bumps the version.
  await page
    .getByRole("button", { name: /^Correct$/i })
    .first()
    .click();
  await expect(page.getByRole("button", { name: /Save correction/i })).toBeDisabled();
  await page.getByLabel(/Reason/).fill("fixed leg");
  await page.getByLabel("Corrected segment end time").fill("10:00");
  await page.getByRole("button", { name: /Save correction/i }).click();
  await expect(page.getByText(/08:00–10:00/)).toBeVisible();

  // Reload — facts persisted in the stateful mock.
  await page.reload();
  await expect(page.getByText(/Business trip · Fri, 15 May/)).toBeVisible();
  await expect(page.getByText(/08:00–10:00/)).toBeVisible();

  // No raw minutes anywhere; 24-hour times only.
  await expect(page.locator("body")).not.toContainText(/\b180 minutes\b|\b900 minutes\b/);

  // Layout: no horizontal overflow at this viewport.
  const overflow = await page.evaluate(
    "document.documentElement.scrollWidth - document.documentElement.clientWidth",
  );
  expect(overflow).toBeLessThanOrEqual(1);
  await page.screenshot({
    path: `test-results/travel-workflow-${testInfo.project.name}.png`,
    fullPage: true,
  });
});

test("travel validation errors surface", async ({ page }, testInfo) => {
  await seed(page);
  await page.getByRole("button", { name: /Add business trip/i }).click();
  await page.getByLabel("Trip start date").fill("2026-05-15");
  await page.getByLabel("Trip end date").fill("2026-05-14");
  await expect(page.getByRole("button", { name: "Create trip" })).toBeDisabled();
  await page.getByLabel("Trip end date").fill("2026-05-17");
  await page.getByRole("button", { name: "Create trip" }).click();
  await expect(page.getByRole("heading", { name: /Business trip/ })).toBeVisible();

  await page
    .getByRole("button", { name: /Add segment/i })
    .first()
    .click();
  await page.getByLabel("Segment start date").fill("2026-05-14");
  await page.getByLabel("Segment start time").fill("07:00");
  await page.getByLabel("Segment end date").fill("2026-05-14");
  await page.getByLabel("Segment end time").fill("07:30");
  await page.getByLabel("Segment classification").selectOption("PASSIVE_TRAVEL");
  await page.getByRole("button", { name: "Add segment" }).click();
  await expect(page.getByText(/must stay inside the trip/i)).toBeVisible();
  await page.screenshot({
    path: `test-results/travel-error-${testInfo.project.name}.png`,
    fullPage: true,
  });
});
