import { expect, test, type Page, type Route } from "@playwright/test";

/**
 * §10.7 — the employee plan-entry acceptance journey, end to end:
 *
 *   add a planned shift → day panel + calendar marker → CORRECT its window while
 *   nothing depends on it (the plan is mutable and the approved revision
 *   advances) → confirm worked as planned → the actual fact appears and the plan
 *   freezes → edit/withdraw refused with the reason shown → preview the Shift
 *   Report → provenance line present → a second shift: record a cancellation
 *   (decision role from the dropdown + the reported instant) → plan preserved,
 *   deviation present, report submittable with the review status → a third
 *   shift: withdraw as a mistaken entry → WITHDRAWN, read-only card, no deviation
 *   → an on-call availability with a reported notice → record a call → the
 *   availability freezes → preview OCD compensation → provenance and reported
 *   notice present, short-notice still absent.
 *
 * DETERMINISTIC by construction. The API is mocked STATEFULLY (the
 * `daily-entry.spec.ts` pattern) so every mutation is recorded and the
 * assertions read what the UI actually sent; nothing depends on the real
 * backend and nothing depends on wall-clock time (every date is explicit, and
 * the day panel is opened by clicking the day's own calendar cell rather than
 * relying on the workspace's "today" default). The four viewports come from
 * `playwright.config.ts` — no project list is hard-coded here.
 *
 * One live boundary this journey RESPECTS rather than fakes: the shift card's
 * private "Preview Shift Report" is gated on a recorded deviation, so a shift
 * confirmed AS PLANNED is previewed through the Submissions page (the only route
 * that exists for it). Both preview surfaces are exercised — the recipient
 * projection for the confirmed shift, and the card's private one for the
 * cancelled shift.
 *
 * The mock mirrors the real payloads field for field — including
 * `OcdOut.version`, the optimistic-concurrency token the per-card Correct/Cancel
 * controls read. It is what makes "the availability freezes" observable as a
 * TRANSITION: the controls are offered while no call exists and withheld once
 * one does.
 */

const MONTH = "2026-07";

/** A recorded mutation: what the UI actually sent, for the payload assertions. */
type Recorded = { method: string; url: string; body: Record<string, unknown> | null };
const recorded: Recorded[] = [];

/** Sent (non-GET) requests whose URL contains `fragment`. */
function sent(fragment: string): Recorded[] {
  return recorded.filter((entry) => entry.url.includes(fragment));
}

/** "2026-07-06T20:00" → "2026-07-06T20:00:00+02:00". July is CEST, so the
 *  offset is fixed rather than computed from the machine clock. */
function aware(local: string): string {
  return `${local}:00+02:00`;
}

interface ShiftRec {
  id: string;
  shift_kind: string;
  status: "PLANNED" | "WITHDRAWN" | "CANCELLED" | "SUPERSEDED";
  plan_source: string;
  approved_revision: {
    id: string;
    revision_no: number;
    planned_start_at: string;
    planned_end_at: string;
    communicated_at: string | null;
  };
  actual_start_at: string | null;
  actual_end_at: string | null;
  deviation: Record<string, unknown> | null;
  findings: Array<Record<string, unknown>>;
  paid_break_status: "EXPLICIT_FACT_REQUIRED" | "RECORDED";
  paid_breaks: Array<Record<string, unknown>>;
}

interface CallRec {
  id: string;
  mode: string;
  call_start_at: string;
  call_end_at: string;
  travel_out_minutes: number | null;
  travel_back_minutes: number | null;
  comment: string | null;
  status: string;
  version: number;
  linked_interval_id: string | null;
  span_start_at: string;
  span_end_at: string;
  elapsed_seconds: number;
}

/** An availability. `version` is the row's optimistic-concurrency token, which
 *  `OcdOut` reports — the card reads it from the row on screen. */
interface OcdRec {
  id: string;
  start_at: string;
  end_at: string;
  status: "SCHEDULED" | "CANCELLED" | "SUPERSEDED";
  scheduled_at: string | null;
  reported_notice_at: string | null;
  source: string;
  version: number;
  calls: CallRec[];
  findings: Array<Record<string, unknown>>;
}

// --- stateful stores: survive navigation and refetches within one test -------
const shifts: ShiftRec[] = [];
const ocdAssignments: OcdRec[] = [];
const packages: Array<Record<string, unknown>> = [];
let shiftSeq = 0;
let callSeq = 0;
/** The fingerprint the last preview handed out. A submit that does not echo it
 *  is the SUBMISSION_CHANGED case, exactly as the real service treats it. */
let lastFingerprint = "";

const monthSummary = {
  period_month: `${MONTH}-01`,
  status: "OPEN",
  interval_count: 0,
  closing_balance_minutes: 0,
  // No recorded work anywhere: every day is "no work", so a calendar cell's
  // accessible name carries ONLY the plan marker this spec is about.
  days: [],
  daily_results: [],
};

async function mockApi(route: Route): Promise<void> {
  const request = route.request();
  const url = request.url();
  const method = request.method();
  const path = new URL(url).pathname;
  const raw = request.postData();
  const body = raw ? (JSON.parse(raw) as Record<string, unknown>) : null;
  if (method !== "GET") recorded.push({ method, url, body });
  const json = (payload: unknown, status = 200) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(payload) });

  // --- session + identity: BOTH pilot switches on --------------------------
  // The shell's CSRF/bootstrap gate reads the session first; answering it here
  // (rather than letting it reach a real backend) is what keeps the journey
  // independent of a running server.
  if (path === "/api/v1/auth/session") {
    await json({ authenticated: true, csrf_token: "csrf-e2e", oidc_mode: false });
    return;
  }
  // The two capabilities gate their own domain only (D24); a shift control and
  // an OCD control are offered independently, so the journey needs both.
  if (path === "/api/v1/me" && method === "GET") {
    await json({
      subject: "employee",
      display_name: "Synthetic Employee",
      capabilities: [
        "OWN_LEDGER_READ",
        "OWN_LEDGER_WRITE",
        "OWN_SUBMISSION_CREATE",
        "OWN_DISCLOSURE_GRANT",
        "SHIFT_PLAN_ENTRY_WRITE",
        "OCD_PLAN_ENTRY_WRITE",
      ],
    });
    return;
  }

  // --- planned shifts ------------------------------------------------------
  if (path.startsWith("/api/v1/me/shifts")) {
    if (path === "/api/v1/me/shifts" && method === "POST") {
      const start = String(body?.planned_start_local);
      const end = String(body?.planned_end_local);
      shiftSeq += 1;
      const created: ShiftRec = {
        id: `shift-${shiftSeq}`,
        shift_kind: (body?.shift_kind as string | null) ?? "SHIFT",
        status: "PLANNED",
        plan_source: "EMPLOYEE_RECORDED",
        approved_revision: {
          id: `rev-${shiftSeq}`,
          revision_no: 1,
          planned_start_at: aware(start),
          planned_end_at: aware(end),
          // D9: an employee-authored revision never claims a publication time.
          communicated_at: null,
        },
        actual_start_at: null,
        actual_end_at: null,
        deviation: null,
        findings: [],
        paid_break_status: "EXPLICIT_FACT_REQUIRED",
        paid_breaks: [],
      };
      shifts.push(created);
      await json(created, 201);
      return;
    }
    if (path === "/api/v1/me/shifts" && method === "GET") {
      await json(shifts);
      return;
    }
    // The plan-window correction: `PATCH /api/v1/me/shifts/{id}` — no trailing
    // slash, and it answers with a NEW approved revision, which is what makes the
    // card's NEXT write prove it re-read the token from the row on screen instead
    // of remembering one. The token check mirrors the real service: a stale
    // `approved_revision_id` is a 409, never a silent overwrite of a revision the
    // employee has not seen.
    const patchId = path.match(/^\/api\/v1\/me\/shifts\/([^/]+)$/)?.[1];
    if (patchId !== undefined && method === "PATCH") {
      const target = shifts.find((row) => row.id === patchId);
      if (!target) {
        await json({ detail: { code: "NOT_FOUND", message: "no such shift" } }, 404);
        return;
      }
      if (body?.approved_revision_id !== target.approved_revision.id) {
        await json(
          {
            detail: {
              code: "CONFLICT_SHIFT_PLAN_CHANGED",
              message: "The approved shift plan changed; refresh before correcting it.",
            },
          },
          409,
        );
        return;
      }
      const nextNo = target.approved_revision.revision_no + 1;
      target.shift_kind = (body?.shift_kind as string | null) ?? target.shift_kind;
      target.approved_revision = {
        id: `rev-${target.id}-r${nextNo}`,
        revision_no: nextNo,
        planned_start_at: aware(String(body?.planned_start_local)),
        planned_end_at: aware(String(body?.planned_end_local)),
        communicated_at: null,
      };
      await json(target);
      return;
    }
    const id = path.match(/^\/api\/v1\/me\/shifts\/([^/]+)\//)?.[1];
    const shift = shifts.find((row) => row.id === id);
    if (!shift) {
      await json({ detail: { code: "NOT_FOUND", message: "no such shift" } }, 404);
      return;
    }
    if (path.endsWith("/confirm")) {
      // Exact-as-planned: the actual fact IS the approved plan.
      shift.actual_start_at = shift.approved_revision.planned_start_at;
      shift.actual_end_at = shift.approved_revision.planned_end_at;
      await json(shift);
      return;
    }
    if (path.endsWith("/deviation")) {
      // Act B (§4.6): the plan and its approved revision are left byte-identical
      // — only the reported context is recorded.
      shift.deviation = {
        id: `deviation-${id}`,
        deviation_type: (body?.deviation_type as string | null) ?? "SHORTENED",
        actual_start_at: null,
        actual_end_at: null,
        noticed_or_decided_at: body?.noticed_or_decided_local
          ? aware(String(body.noticed_or_decided_local))
          : null,
        decision_maker_type: (body?.decision_maker_type as string | null) ?? null,
        reason: (body?.reason as string | null) ?? null,
        version: 1,
      };
      await json(shift);
      return;
    }
    if (path.endsWith("/withdraw")) {
      // Act A (§4.6): the employee retracts their OWN plan. No deviation.
      shift.status = "WITHDRAWN";
      await json(shift);
      return;
    }
    if (path.endsWith("/report-preview")) {
      const cancelled = shift.deviation?.deviation_type === "CANCELLED";
      lastFingerprint = `fp-${shift.id}-${shift.deviation ? "deviation" : "plan"}`;
      await json({
        report_type: "SHIFT_REPORT",
        policy_version: "2026-shift-v1",
        policy_status: "PROVISIONAL",
        employee_identifier: "emp-0001",
        shift_assignment_id: shift.id,
        planned_start_at: shift.approved_revision.planned_start_at,
        planned_end_at: shift.approved_revision.planned_end_at,
        actual_start_at: shift.actual_start_at,
        actual_end_at: shift.actual_end_at,
        deviation_type: (shift.deviation?.deviation_type as string | null) ?? null,
        // The reported context, which the recipient projection renders: the
        // instant, the role the employee was TOLD and the stated context.
        noticed_or_decided_at: (shift.deviation?.noticed_or_decided_at as string | null) ?? null,
        decision_maker_type: (shift.deviation?.decision_maker_type as string | null) ?? null,
        reason: (shift.deviation?.reason as string | null) ?? null,
        // The entitlement-class finding an employee-authored cancellation really
        // produces (SHIFT-009, `communicated_at is None`): it stays PRIVATE and
        // must never reach the recipient payload.
        findings: cancelled
          ? [
              {
                rule_id: "SHIFT-009",
                status: "BLOCKED_POLICY",
                message: "Cancellation timing cannot be classified without the communicated time.",
              },
            ]
          : [],
        plan_source: shift.plan_source,
        compensation_review_status: cancelled ? "MANUAL_REVIEW_REQUIRED" : "NOT_APPLICABLE",
        paid_break_status: shift.paid_break_status,
        preview_fingerprint: lastFingerprint,
      });
      return;
    }
  }

  // --- OCD availabilities and calls ----------------------------------------
  if (path === "/api/v1/me/ocd-assignments" && method === "POST") {
    const created: OcdRec = {
      id: `ocd-${ocdAssignments.length + 1}`,
      start_at: aware(String(body?.start_local)),
      end_at: aware(String(body?.end_local)),
      status: "SCHEDULED",
      // D17: `scheduled_at` is INSTITUTIONAL and server-forced NULL here; the
      // employee's own account is preserved separately and read by no classifier.
      scheduled_at: null,
      reported_notice_at: body?.reported_notice_local
        ? aware(String(body.reported_notice_local))
        : null,
      source: "EMPLOYEE_RECORDED",
      // A freshly created availability is version 1; PATCH and cancel are
      // guarded on this token (409 CONFLICT_VERSION when it is stale).
      version: 1,
      calls: [],
      findings: [],
    };
    ocdAssignments.push(created);
    await json(created, 201);
    return;
  }
  if (path.endsWith("/compensation-preview")) {
    const assignment = ocdAssignments.find((row) => path.includes(`/ocd-assignments/${row.id}/`));
    if (!assignment) {
      await json({ detail: { code: "NOT_FOUND", message: "no such assignment" } }, 404);
      return;
    }
    lastFingerprint = `fp-${assignment.id}`;
    await json({
      report_type: "OCD_COMPENSATION",
      policy_version: "2026-ocd-v1",
      policy_status: "CONFIRMED",
      employee_identifier: "emp-0001",
      ocd_assignment_id: assignment.id,
      assignment_start_at: assignment.start_at,
      assignment_end_at: assignment.end_at,
      calls: assignment.calls.map((call) => ({
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
        // The real classification: `scheduled_at` is NULL on an employee-authored
        // row, so OCD-007 cannot classify short notice at all. `null` is a
        // DIFFERENT fact from a classification of "no".
        short_notice: null,
      },
      election: { id: null, mode: null, version: null, selected_at: null, blocked: false },
      findings: [
        {
          rule_id: "OCD-007",
          status: "INFO",
          message: "Short-notice classification is unavailable because scheduled time is absent.",
        },
      ],
      plan_source: "EMPLOYEE_RECORDED",
      reported_notice_at: assignment.reported_notice_at,
      preview_fingerprint: lastFingerprint,
    });
    return;
  }
  if (path === "/api/v1/me/ocd-calls" && method === "POST") {
    const assignment = ocdAssignments.find((row) => row.id === body?.ocd_assignment_id);
    if (!assignment) {
      await json({ detail: { code: "NOT_FOUND", message: "no such assignment" } }, 404);
      return;
    }
    callSeq += 1;
    const call: CallRec = {
      id: `call-${callSeq}`,
      mode: String(body?.mode),
      call_start_at: aware(String(body?.call_start_local)),
      call_end_at: aware(String(body?.call_end_local)),
      travel_out_minutes: (body?.travel_out_minutes as number | null) ?? null,
      travel_back_minutes: (body?.travel_back_minutes as number | null) ?? null,
      comment: (body?.comment as string | null) ?? null,
      status: "RECORDED",
      version: 1,
      linked_interval_id: `interval-${callSeq}`,
      span_start_at: aware("2026-07-19T23:17"),
      span_end_at: aware("2026-07-20T00:43"),
      elapsed_seconds: 5160,
    };
    assignment.calls.push(call);
    await json(call);
    return;
  }
  if (path === "/api/v1/me/ocd" && method === "GET") {
    await json(ocdAssignments);
    return;
  }
  if (path === "/api/v1/me/compensation-elections") {
    await json(method === "POST" ? { id: "election-1", mode: body?.mode, version: 1 } : null);
    return;
  }

  // --- month scaffolding ---------------------------------------------------
  if (path === `/api/v1/me/months/${MONTH}` && method === "GET") {
    await json(monthSummary);
    return;
  }
  if (path.startsWith(`/api/v1/me/months/${MONTH}/days/`)) {
    await json({ date: path.slice(path.lastIndexOf("/") + 1), intervals: [], breaks: [] });
    return;
  }
  if (path === "/api/v1/me/findings") {
    await json({ closing_balance_minutes: 0, findings: [] });
    return;
  }
  if (path === "/api/v1/me/day-status") {
    await json({ entries: [] });
    return;
  }
  if (path === "/api/v1/me/profile") {
    await json({
      effective_at: `${MONTH}-01`,
      employee: {
        external_employee_id: "emp-0001",
        legal_name: "Synthetic Employee",
        email: null,
      },
      contract: null,
      schedule: null,
      preferences: {
        usual_work_type: "ON_SITE",
        preferred_calendar_view: "month",
        normal_day_template: null,
      },
    });
    return;
  }

  // --- submissions / disclosure -------------------------------------------
  if (path === "/api/v1/me/submission-recipients") {
    // Server-controlled registry; the employee still chooses explicitly (INV-05).
    await json([{ subject: "demo-recipient", readable: true }]);
    return;
  }
  if (path.startsWith("/api/v1/me/submissions") && method === "POST") {
    if (body?.preview_fingerprint !== lastFingerprint) {
      await json(
        { detail: { code: "SUBMISSION_CHANGED", message: "Facts changed since preview." } },
        409,
      );
      return;
    }
    const created = {
      id: `package-${packages.length + 1}`,
      purpose: path.slice(path.lastIndexOf("/") + 1),
      period_start: `${MONTH}-01`,
      period_end: `${MONTH}-31`,
      version: 1,
      status: "SUBMITTED",
      supersedes_id: null,
      created_at: "2026-07-31T09:00:00+02:00",
      employee_confirmed_at: "2026-07-31T09:00:00+02:00",
      recipients: ((body?.recipients as string[]) ?? []).map((subject) => ({
        recipient_principal: subject,
        recipient_type: "RECIPIENT",
        read_at: null,
      })),
      payload: {},
    };
    packages.push(created);
    await json(created, 201);
    return;
  }
  if (path.startsWith("/api/v1/me/submissions")) {
    await json(packages);
    return;
  }

  await json({});
}

/** The calendar cell for a July day, addressed by its SPOKEN name.
 *
 * The cell's accessible name now begins with the full weekday and year
 * ("Monday, 6 July 2026") rather than the month and a bare number, and it no
 * longer counts intervals: the event bars and the delta carry that, and the
 * spoken name is the only place a screen reader reaches them. */
function dayCell(page: Page, day: number) {
  return page.getByRole("button", { name: new RegExp(`^\\w+day, ${day} July 2026`) });
}

/** Open a day's panel by clicking its calendar cell. */
async function openDay(page: Page, day: number): Promise<void> {
  await dayCell(page, day).click();
  // The panel is a region named by its OWN visible heading ("Mon, 6 July"), not by
  // a written-out "Timeline for <ISO date>" — the name and the heading cannot drift
  // apart, and the old label stopped describing the panel when it grew its four
  // sections. Anchored so day 6 cannot match 16 or 26.
  await expect(
    page.getByRole("region", { name: new RegExp(`^\\w{3}, ${day} July$`) }),
  ).toBeVisible();
}

/** Add a planned shift through the real form. `kind` is the <select> value. */
async function addPlannedShift(
  page: Page,
  day: number,
  start: string,
  end: string,
  kind: string,
): Promise<void> {
  await page.getByRole("button", { name: "+ Add planned shift" }).click();
  const form = page.getByRole("group", { name: "Add planned shift" });
  await form.getByLabel("Planned start").fill(start);
  await form.getByLabel("Planned end").fill(end);
  await form.getByLabel("Shift kind").selectOption(kind);
  await form.getByRole("button", { name: "Add", exact: true }).click();
  await expect(page.getByText("Planned shift recorded.")).toBeVisible();
  await openDay(page, day);
}

/** Select the assignment whose option text starts with `namePrefix` in a
 *  Submissions scope picker, without depending on the option's position. */
async function chooseAssignment(card: ReturnType<Page["locator"]>, namePrefix: string) {
  const value = await card
    .getByRole("option", { name: new RegExp(`^${namePrefix}`) })
    .getAttribute("value");
  await card.getByLabel("Assignment").selectOption(value as string);
}

test.beforeEach(async ({ page }) => {
  recorded.length = 0;
  shifts.length = 0;
  ocdAssignments.length = 0;
  packages.length = 0;
  shiftSeq = 0;
  callSeq = 0;
  lastFingerprint = "";
  await page.route("**/api/v1/**", mockApi);
});

test("plan entry: a plan, its freeze, a cancellation, a withdrawal and an availability", async ({
  page,
}, testInfo) => {
  const isMobile = testInfo.project.name.startsWith("mobile");
  // ===================================================== 1. add a planned shift
  await page.goto(`/month/${MONTH}`);
  await openDay(page, 6);
  await expect(page.getByRole("button", { name: "+ Add planned shift" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Night shift plan and actual" })).toHaveCount(0);

  await addPlannedShift(page, 6, "06.07.2026 20:00", "06.07.2026 23:00", "NIGHT");

  // The plan is retry-safe (BE-CMD-001): a create carries a command key.
  const planPost = sent("/api/v1/me/shifts").find((entry) => entry.method === "POST");
  expect(planPost?.body).toMatchObject({
    planned_start_local: "2026-07-06T20:00",
    planned_end_local: "2026-07-06T23:00",
    shift_kind: "NIGHT",
  });
  expect(typeof planPost?.body?.client_request_id).toBe("string");

  // The calendar marker is a semantic event BAR: the category is carried by a
  // WORD as well as by the hue, and the cell's accessible name states the whole
  // phrase — the bars are aria-hidden, so that name is the only place assistive
  // technology can reach the plan.
  const sixth = dayCell(page, 6);
  await expect(sixth.locator(".cal-bar")).toHaveCount(1);
  await expect(sixth.locator(".cal-bar")).toHaveAttribute("data-cal-kind", "SHIFT");
  await expect(sixth.locator(".cal-bar-label")).toHaveText("Shift");
  // One markup, two densities: the desktop cell shows the readable category word,
  // the phone cell shows its two-letter glyph instead. The label stays in the DOM
  // either way, which is why `toHaveText` above holds at every width.
  if (isMobile) await expect(sixth.locator(".cal-bar-glyph")).toBeVisible();
  else await expect(sixth.locator(".cal-bar-label")).toBeVisible();
  await expect(sixth).toHaveAttribute("aria-label", /planned night shift/);

  const night = page.getByLabel("Night shift plan and actual");
  await expect(night.getByText("Entered by employee")).toBeVisible();
  await expect(night.getByRole("definition").first()).toContainText(
    "06 Jul 2026, 20:00 → 06 Jul 2026, 23:00",
  );
  await expect(night.getByRole("definition").nth(1)).toHaveText("Not confirmed yet");

  // ============ 1b. correct the window BEFORE anything depends on it (§12.2 row 2)
  // A plan with no linked actual time, no deviation and no paid break is MUTABLE,
  // and a correction advances the approved revision. This is the third act on the
  // card — not a cancellation (which leaves the plan byte-identical) and not a
  // withdrawal (for a shift that was never really planned) — so the form names the
  // other two, and the fields arrive prefilled from the row.
  // The three secondary acts now live behind ONE menu on the card (owner
  // directive 2026-09-26): only "Confirm worked as planned" and "Record a
  // deviation" stay visible, so the destructive removal can no longer dominate the
  // row. Opening the menu is part of reaching them.
  await night.getByRole("button", { name: "More shift actions" }).click();
  await page.getByRole("button", { name: "Correct the planned shift" }).click();
  const correction = page.getByRole("group", { name: "Correct the planned shift" });
  // Prefilled from the row as bare TIMES: each value already sits on the day its
  // own field anchors to (the window's start day, and the window's end day), and
  // a derived day is implied rather than restated. The PATCH body below proves the
  // underlying instants are the row's, unchanged. A STATED date still wins and is
  // shown in full, which the cross-midnight end fill exercises.
  await expect(correction.getByLabel("Planned start")).toHaveValue("20:00");
  await expect(correction.getByLabel("Planned end")).toHaveValue("23:00");
  await expect(correction.getByLabel("Shift kind")).toHaveValue("NIGHT");
  await correction.getByLabel("Planned end").fill("07.07.2026 00:30");
  await correction.getByRole("button", { name: "Save correction" }).click();
  await expect(
    page.getByText("Planned shift corrected. The plan now shows the window you recorded."),
  ).toBeVisible();

  const correctionPatch = sent("/api/v1/me/shifts/shift-1").find(
    (entry) => entry.method === "PATCH",
  );
  // The token is the CURRENT approved revision, read from the row — and there is
  // NO command key: a correction is a version write, not a create.
  expect(correctionPatch?.body).toMatchObject({
    approved_revision_id: "rev-1",
    planned_start_local: "2026-07-06T20:00",
    planned_end_local: "2026-07-07T00:30",
    shift_kind: "NIGHT",
  });
  expect(correctionPatch?.body).not.toHaveProperty("client_request_id");
  // The plan itself moved, and the calendar marker still names the day the plan
  // STARTS (the corrected end crosses midnight, which is an ordinary timestamp).
  await expect(night.getByRole("definition").first()).toContainText(
    "06 Jul 2026, 20:00 → 07 Jul 2026, 00:30",
  );

  // ============================== 2. confirm worked as planned → plan frozen
  await night.getByRole("button", { name: "Confirm worked as planned" }).click();
  await expect(page.getByText("Shift confirmed as planned.")).toBeVisible();
  const confirmPost = sent("/api/v1/me/shifts/shift-1/confirm")[0];
  expect(confirmPost?.method).toBe("POST");
  // The NEXT revision: the card re-read the token after the correction instead of
  // reusing the one it had (the mock answers a stale token with 409).
  expect(confirmPost?.body).toMatchObject({ approved_revision_id: "rev-shift-1-r2" });

  // The ACTUAL fact appears, and the plan is frozen from now on.
  await expect(night.getByRole("definition").nth(1)).toContainText(
    "06 Jul 2026, 20:00 → 07 Jul 2026, 00:30",
  );

  // ====================== 3. edit/withdraw refused with the REASON shown (§6.5)
  await expect(
    night.getByText("This shift already has recorded actual time. Record a deviation instead."),
  ).toBeVisible();
  // The menu is OPENED first: a closed menu renders no items, so an unscoped
  // `toHaveCount(0)` would pass even if the control were still offered — it would
  // prove nothing about the freeze.
  await night.getByRole("button", { name: "More shift actions" }).click();
  await expect(page.getByRole("button", { name: "Remove plan entered by mistake" })).toHaveCount(0);
  // Nor is the correction offered: a frozen plan shows the reason and the correct
  // action instead of a control that can only fail (D3).
  await expect(page.getByRole("button", { name: "Correct the planned shift" })).toHaveCount(0);
  await page.keyboard.press("Escape");
  // Nothing was sent: the refusal is stated rather than left to a 409, so the only
  // correction on record is the one made while the plan was still mutable.
  expect(sent("/withdraw")).toHaveLength(0);
  expect(
    sent("/api/v1/me/shifts/shift-1").filter((entry) => entry.method === "PATCH"),
  ).toHaveLength(1);

  // ============ 4. preview the Shift Report → the provenance line is present
  // A confirmed-as-planned shift has no deviation, so the card does not offer
  // its private preview; the recipient projection is the route that exists.
  await page.goto(`/month/${MONTH}/submissions`);
  await expect(page.getByRole("heading", { name: /Submissions & Sharing/ })).toBeVisible();
  const shiftReport = page
    .locator("section.submission-card")
    .filter({ hasText: "Share Shift Report" });
  await chooseAssignment(shiftReport, "Night shift");
  await shiftReport.getByRole("button", { name: "Preview exact package — Shift report" }).click();
  const recipientView = page.getByRole("region", { name: "What the recipient will see" });
  await expect(recipientView).toContainText("Planned: 06 Jul 2026, 20:00 → 07 Jul 2026, 00:30");
  await expect(recipientView).toContainText("Actual: 06 Jul 2026, 20:00 → 07 Jul 2026, 00:30");
  await expect(recipientView).toContainText("Plan source: Planned shift");
  await expect(recipientView).toContainText("Compensation review: No compensation review needed");

  // =================== 5. a second shift: record a Company cancellation (Act B)
  await page.goto(`/month/${MONTH}`);
  await addPlannedShift(page, 10, "10.07.2026 06:00", "10.07.2026 14:00", "SHIFT");
  const day = page.getByLabel("Shift plan and actual");
  await day.getByRole("button", { name: "More shift actions" }).click();
  await page.getByRole("button", { name: "Record a shift cancellation" }).click();
  await expect(page.getByRole("heading", { name: "Shift cancellation" })).toBeVisible();
  // The field is what the employee was TOLD, never "Cancelled by" (D22), and the
  // dropdown offers the source-backed roles plus the escape hatch.
  const roleField = day.getByLabel("Who decided to cancel (as reported to you)");
  await expect(roleField.getByRole("option")).toHaveText([
    "Director",
    "Department Head",
    "Supervisor / Group Leader",
    "Person nominated by the Management Board",
    "Other / not stated",
  ]);
  await day.getByLabel("When were you told?").fill("08.07.2026 09:00");
  await roleField.selectOption("Department Head");
  await day.getByLabel("Notes / context").fill("Told by the Local Contact.");
  await day.getByRole("button", { name: "Save cancellation" }).click();
  await expect(page.getByText("Cancellation recorded. Your planned shift is kept.")).toBeVisible();

  const cancelPost = sent("/api/v1/me/shifts/shift-2/deviation")[0];
  expect(cancelPost?.body).toMatchObject({
    deviation_type: "CANCELLED",
    noticed_or_decided_local: "2026-07-08T09:00",
    decision_maker_type: "Department Head",
    reason: "Told by the Local Contact.",
  });
  // A cancellation needs no actual times, and the CANCELLED path deletes linked
  // intervals — so sending none is what keeps the partial work out of the blast.
  expect(cancelPost?.body).not.toHaveProperty("actual_start_local");
  expect(cancelPost?.body).not.toHaveProperty("actual_end_local");

  // The plan is PRESERVED byte-identical: the scheduled shift is the basis for
  // any allowance (POL-02), so status stays PLANNED and no status copy
  // appears. Only the deviation is added.
  await expect(day.getByRole("definition").first()).toContainText(
    "10 Jul 2026, 06:00 → 10 Jul 2026, 14:00",
  );
  await expect(day.getByText("Withdrawn")).toHaveCount(0);
  // The deviation is PRESENT: the card no longer offers to record one, and no
  // actual work was invented by the cancellation.
  await expect(day.getByRole("button", { name: "Record a deviation" })).toHaveCount(0);
  // "Not confirmed yet" would read as outstanding work on a shift that never
  // happened, so the Actual cell names the reason instead. The plan itself is
  // untouched — the window above still reads as planned.
  await expect(day.getByRole("definition").nth(1)).toHaveText("None — the shift was cancelled");
  // The menu is OPENED first. A closed menu renders no items at all, so an
  // unscoped `toHaveCount(0)` would pass even if the withdrawal were still
  // offered — it would prove nothing about the cancellation having frozen it.
  await day.getByRole("button", { name: "More shift actions" }).click();
  await expect(page.getByRole("button", { name: "Remove plan entered by mistake" })).toHaveCount(0);
  await page.keyboard.press("Escape");

  // The private preview carries the provenance AND the one review status the
  // recipient gets instead of an entitlement (D19) — never an amount.
  await day.getByRole("button", { name: "Preview Shift Report" }).click();
  const privatePreview = page.getByLabel("Private Shift Report preview");
  await expect(privatePreview).toContainText("Planned shift");
  await expect(privatePreview).toContainText(
    "Compensation review: Compensation needs manual review",
  );
  await expect(privatePreview).not.toContainText("€");

  // ... and the factual report is SUBMITTABLE, with the review status visible.
  await page.goto(`/month/${MONTH}/submissions`);
  const cancelledReport = page
    .locator("section.submission-card")
    .filter({ hasText: "Share Shift Report" });
  await chooseAssignment(cancelledReport, "Shift ·");
  await cancelledReport
    .getByRole("button", { name: "Preview exact package — Shift report" })
    .click();
  const cancelledView = page.getByRole("region", { name: "What the recipient will see" });
  await expect(cancelledView).toContainText("Deviation: Cancelled");
  await expect(cancelledView).toContainText("Noticed or decided: 08 Jul 2026, 09:00");
  await expect(cancelledView).toContainText("Decided or reported by: Department Head");
  await expect(cancelledView).toContainText("Reason given: Told by the Local Contact.");
  await expect(cancelledView).toContainText("Plan source: Planned shift");
  await expect(cancelledView).toContainText(
    "Compensation review: Compensation needs manual review",
  );
  // The employee-private finding stays PRIVATE. The preview payload the server
  // returned DID carry the SHIFT-009 BLOCKED_POLICY finding, and the recipient
  // projection still contains none of it: the client strips `findings` and the
  // server excludes them from the frozen package, so the disclosure cannot leak
  // the entitlement-class finding.
  await expect(cancelledView).toContainText("none shared");
  await expect(cancelledView).not.toContainText("SHIFT-009");
  await expect(cancelledView).not.toContainText("Cancellation timing cannot be classified");
  await cancelledReport.getByRole("checkbox", { name: "demo-recipient" }).check();
  await cancelledView.getByRole("button", { name: "Submit this Shift report package" }).click();
  await expect(page.getByText("Submitted. Recipients can read only this package.")).toBeVisible();
  const submitPost = sent("/api/v1/me/submissions/SHIFT_REPORT")[0];
  expect(submitPost?.body).toMatchObject({
    scope: { assignment_id: "shift-2" },
    recipients: ["demo-recipient"],
    // The fingerprint is the one the PREVIEW handed out — the disclosure is
    // bound to the facts that were reviewed.
    preview_fingerprint: "fp-shift-2-deviation",
  });

  // ==================== 6. a third shift: withdraw as a mistaken entry (Act A)
  await page.goto(`/month/${MONTH}`);
  await addPlannedShift(page, 14, "14.07.2026 08:00", "14.07.2026 16:00", "SHIFT");
  const mistake = page.getByLabel("Shift plan and actual");
  // The withdrawal lives behind the card's one secondary-action menu now, and the
  // control was renamed to name the ACT rather than narrate the situation.
  await mistake.getByRole("button", { name: "More shift actions" }).click();
  await page.getByRole("button", { name: "Remove plan entered by mistake" }).click();
  // Two steps: the first click only arms (UI-006).
  await mistake.getByRole("button", { name: "Remove the planned shift" }).click();
  await expect(page.getByText("Plan withdrawn. No cancellation was recorded.")).toBeVisible();

  const withdrawPost = sent("/api/v1/me/shifts/shift-3/withdraw")[0];
  expect(withdrawPost?.method).toBe("POST");
  expect(withdrawPost?.body).toMatchObject({ approved_revision_id: "rev-3" });
  // A retraction is not a create, so it carries the expected token and NOT a
  // command key.
  expect(withdrawPost?.body).not.toHaveProperty("client_request_id");
  // A withdrawal produces NO deviation — that is what makes it distinguishable
  // from the cancellation above.
  expect(sent("/shifts/shift-3/deviation")).toHaveLength(0);

  // WITHDRAWN is read-only and is NOT an active plan: no action at all, and the
  // card says which act happened rather than borrowing the cancellation's words.
  await expect(mistake.getByText("Withdrawn", { exact: true })).toBeVisible();
  await expect(mistake.getByRole("button")).toHaveCount(0);
  await expect(mistake.getByRole("definition").first()).toContainText(
    "14 Jul 2026, 08:00 → 14 Jul 2026, 16:00",
  );
  // ... and it is excluded from the calendar marker feed, while remaining in the
  // employee's own records (list_shifts still returns it).
  // The fixture is a `SHIFT`-kind plan, whose phrase is "planned shift" — asserting
  // absence of "planned NIGHT shift" could never fail for it, so the assertion
  // protected nothing. Match the phrase the fixture would actually produce.
  await expect(dayCell(page, 14)).not.toHaveAttribute("aria-label", /planned shift/i);
  await expect(dayCell(page, 14).locator(".cal-bar")).toHaveCount(0);

  // ================================ 7. an on-call availability + a recorded call
  await page.goto(`/month/${MONTH}/ocd`);
  await expect(page.getByRole("heading", { name: /On-call duty — July 2026/ })).toBeVisible();
  await page.getByRole("button", { name: "+ Add on-call duty" }).click();
  const ocdForm = page.getByRole("group", { name: "Add on-call duty" });
  await ocdForm.getByLabel("Starts").fill("19.07.2026 19:00");
  await ocdForm.getByLabel("Ends").fill("20.07.2026 08:00");
  await ocdForm
    .getByLabel("When were you told? (optional — recorded as reported)")
    .fill("01.07.2026 09:00");
  await ocdForm.getByRole("button", { name: "Add", exact: true }).click();
  await expect(page.getByText("On-call availability recorded.")).toBeVisible();

  const ocdPost = sent("/api/v1/me/ocd-assignments").find((entry) => entry.method === "POST");
  expect(ocdPost?.body).toMatchObject({
    start_local: "2026-07-19T19:00",
    end_local: "2026-07-20T08:00",
    reported_notice_local: "2026-07-01T09:00",
  });
  // The INSTITUTIONAL notice instant can never be asserted from here (D17): it
  // is the sole input to the remuneration-relevant short-notice classification.
  expect(ocdPost?.body).not.toHaveProperty("scheduled_at");

  const availability = page.getByLabel(/^OCD assignment 19 Jul 2026, 19:00/);
  // Provenance is fail-closed: an employee record never reads "Approved plan".
  // The noun is "on-call duty" — an availability is not a shift (INV-10).
  await expect(availability.getByText("Planned on-call duty")).toBeVisible();
  await expect(availability.getByText("Employee-entered plan.")).toBeVisible();
  // The reported notice is shown AS REPORTED, and the copy says it decides nothing.
  await expect(availability.getByText(/Notice you reported:/)).toContainText(
    "01 Jul 2026, 09:00 — kept as reported.",
  );

  // While NOTHING depends on it, the availability is writable: the row reports
  // its token, so both lifecycle controls are offered.
  await expect(availability.getByRole("button", { name: "Correct", exact: true })).toBeVisible();
  await expect(availability.getByRole("button", { name: "Cancel", exact: true })).toBeVisible();
  await expect(availability.getByText(/already has recorded calls/)).toHaveCount(0);

  await availability.getByRole("button", { name: "Add call" }).click();
  await availability.getByLabel("Call start").fill("19.07.2026 23:42");
  await availability.getByLabel("Call end").fill("20.07.2026 00:18");
  await availability.getByLabel(/Travel to site/).fill("25");
  await availability.getByLabel(/Travel back/).fill("25");
  await availability.getByRole("button", { name: "Record call + work time" }).click();
  await expect(page.getByText("Call recorded together with its work time.")).toBeVisible();

  const callPost = sent("/api/v1/me/ocd-calls")[0];
  expect(callPost?.body).toMatchObject({
    ocd_assignment_id: "ocd-1",
    call_start_local: "2026-07-19T23:42",
    call_end_local: "2026-07-20T00:18",
    mode: "ON_SITE",
    travel_out_minutes: 25,
    travel_back_minutes: 25,
  });

  // The availability is FROZEN: a call now depends on it, so the freeze is
  // stated in words and the controls that could only fail are withheld — the
  // transition from the two assertions above.
  await expect(
    availability.getByText(
      "This availability already has recorded calls, so it can no longer be corrected or cancelled — its calls stay linked to it.",
    ),
  ).toBeVisible();
  // `exact` matters: "Correct" is a substring of the CALL row's own
  // "Correct call", which is a different fact from correcting the availability.
  await expect(availability.getByRole("button", { name: "Correct", exact: true })).toHaveCount(0);
  await expect(availability.getByRole("button", { name: "Cancel", exact: true })).toHaveCount(0);

  // ==================== 8. preview OCD compensation → provenance, notice, no short notice
  await availability.getByRole("button", { name: "Preview OCD compensation" }).click();
  const ocdPreview = page.getByLabel("Private OCD compensation preview");
  await expect(ocdPreview).toContainText("OCD compensation preview — not shared");
  await expect(ocdPreview).toContainText("Call work");
  await expect(ocdPreview).toContainText("No monetary amount is calculated");
  // `scheduled_at` is NULL, so the classifier cannot answer: the state is "not
  // classified", which is a DIFFERENT fact from a classification of "no".
  await expect(ocdPreview).toContainText("short-notice compensation: not classified");
  await expect(ocdPreview).not.toContainText("short-notice compensation: yes");
  await expect(ocdPreview).not.toContainText("short-notice compensation: no (");

  // The disclosed package states the provenance and the reported notice — as
  // REPORTED, never as the Company's notice time.
  await page.goto(`/month/${MONTH}/submissions`);
  const ocdCard = page
    .locator("section.submission-card")
    .filter({ hasText: "Submit OCD Compensation" });
  await chooseAssignment(ocdCard, "On-call duty ·");
  await ocdCard.getByRole("button", { name: "Preview exact package — OCD compensation" }).click();
  const ocdView = page.getByRole("region", { name: "What the recipient will see" });
  // "Planned on-call duty", not "Planned shift": the label takes the noun of what
  // was planned, and an availability is not a shift (INV-10).
  await expect(ocdView).toContainText("Plan source: Planned on-call duty");
  await expect(ocdView).toContainText("Notice (as reported): 01 Jul 2026, 09:00");
  await expect(ocdView).toContainText("short-notice compensation: not classified");
});
