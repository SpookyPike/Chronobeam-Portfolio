import { expect, test, type Route } from "@playwright/test";

/**
 * PHASE-10 FULL-PRODUCT walkthrough (the future PHASE-11 demo journey).
 *
 * One deterministic synthetic narrative against the ACTUAL rendered UI at all
 * four viewports (1440/1280/390/360 via the projects in playwright.config):
 *
 *   Administrator provisions an employee and required access
 *     -> employee checks profile/schedule
 *     -> records ordinary work (multi-interval, mixed ON_SITE/REMOTE,
 *        cross-midnight, correction add, findings/review)
 *     -> records shift (exact-as-planned + deviation), OCD call, overtime,
 *        travel (passive + driving)
 *     -> reviews month
 *     -> previews Shift/OCD/Overtime packages, submits ONE minimized package
 *     -> HR receives only that package
 *     -> HR creates a random-check request
 *     -> employee reviews and explicitly grants the requested month
 *     -> HR reads exactly that granted view (adjacent data denied)
 *     -> technical/application admin manages the service but cannot browse
 *        the employee ledger (403 + identity-only surfaces)
 *     -> reload persists the deterministic state
 *
 * The API is a stateful in-page mock of the wire contract (same approach as
 * disclosure.spec.ts). Deterministic calculation edge cases stay in lower-
 * level tests; this journey is a user narrative, not an edge-case dump.
 */

let principal = "admin";
let progress = "";
const capabilitiesByPrincipal: Record<string, string[]> = {
  admin: ["ACCESS_ADMIN", "SYSTEM_STATUS_READ"],
  mara: ["OWN_LEDGER_READ", "OWN_LEDGER_WRITE", "OWN_SUBMISSION_CREATE", "OWN_DISCLOSURE_GRANT"],
  "hr-requester": ["ACCESS_REQUEST_CREATE", "DISCLOSURE_READ_GRANTED"],
  recipient: ["SUBMISSION_READ_ASSIGNED"],
};

// --- stateful stores (survive navigation/reload within a test) -------------
interface User {
  id: string;
  external_employee_id: string | null;
  legal_name: string;
  email: string | null;
  active: boolean;
  created_at: string;
  idp_subject: string | null;
  // Readiness rides on the summary (and the detail inherits it): presence facts
  // about provisioning, derived here the way the server derives them.
  has_idp_subject: boolean;
  has_effective_contract: boolean;
  has_effective_schedule: boolean;
  role_assignments: Array<{
    id: string;
    role: string;
    granted_by_principal: string;
    granted_at: string;
    revoked_at: string | null;
  }>;
  effective_capabilities: string[];
}
const users: User[] = [];
let userSeq = 1;
let assignmentSeq = 1;

const intervals: Record<string, Array<Record<string, unknown>>> = {};
interface ShiftRec {
  id: string;
  status: string;
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
  [key: string]: unknown;
}
const shifts: ShiftRec[] = [];
const ocdCalls: Array<Record<string, unknown>> = [];
interface TripSegmentRec {
  id: string;
  trip_id: string;
  classification: string;
  basis?: string;
  work_interval_id: string | null;
  [key: string]: unknown;
}
interface TripRec {
  id: string;
  segments: TripSegmentRec[];
  [key: string]: unknown;
}
const trips: TripRec[] = [];
let segmentsSeq = 1;
const packages: Array<Record<string, unknown>> = [];
const requests: Array<Record<string, unknown>> = [];
const grants: Array<Record<string, unknown>> = [];
let packageFingerprint = "";
let overtimeFingerprint = "";
// PHASE-11 F04: the walkthrough now links an authorization reference and
// reflects that in the preview mock before submitting the overtime package.
let otAuthorization: Record<string, unknown> | null = null;
let otLinked = false;

function provisionedCaps(user: User): string[] {
  if (!user.active || !user.idp_subject) return [];
  const roleCaps: Record<string, string[]> = {
    APPLICATION_ADMIN: ["ACCESS_ADMIN", "SYSTEM_STATUS_READ"],
    HR_REQUESTER: ["ACCESS_REQUEST_CREATE", "DISCLOSURE_READ_GRANTED"],
    SUBMISSION_RECIPIENT: ["SUBMISSION_READ_ASSIGNED"],
  };
  return Array.from(
    new Set([
      "OWN_LEDGER_READ",
      "OWN_LEDGER_WRITE",
      "OWN_SUBMISSION_CREATE",
      "OWN_DISCLOSURE_GRANT",
      ...user.role_assignments
        .filter((row) => !row.revoked_at)
        .flatMap((row) => roleCaps[row.role] ?? []),
    ]),
  );
}

function json(route: Route, body: unknown, status = 200): Promise<void> {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

function jsonError(route: Route, code: string, message: string, status: number): Promise<void> {
  return json(route, { detail: { code, message } }, status);
}

function seed() {
  users.length = 0;
  userSeq = 1;
  assignmentSeq = 1;
  users.push(
    {
      id: "user-admin",
      external_employee_id: "emp-0000",
      legal_name: "Ada Admin (synthetic)",
      email: "ada@example.invalid",
      active: true,
      created_at: "2026-08-01T08:00:00Z",
      idp_subject: "admin",
      has_idp_subject: true,
      has_effective_contract: true,
      has_effective_schedule: true,
      role_assignments: [
        {
          id: "assignment-admin",
          role: "APPLICATION_ADMIN",
          granted_by_principal: "BOOTSTRAP_CLI",
          granted_at: "2026-08-01T08:00:00Z",
          revoked_at: null,
        },
      ],
      effective_capabilities: ["ACCESS_ADMIN", "SYSTEM_STATUS_READ"],
    },
    {
      id: "user-lena",
      external_employee_id: "emp-0999",
      legal_name: "Lena Demo (synthetic)",
      email: null,
      active: false,
      created_at: "2026-08-02T08:00:00Z",
      idp_subject: null,
      has_idp_subject: false,
      has_effective_contract: false,
      has_effective_schedule: false,
      role_assignments: [],
      effective_capabilities: [],
    },
  );
  intervals["2026-07-06"] = [
    {
      id: "i-1",
      work_type: "ON_SITE",
      start_at: "2026-07-06T08:00:00+02:00",
      end_at: "2026-07-06T10:00:00+02:00",
      start_local: "2026-07-06T08:00:00",
      end_local: "2026-07-06T10:00:00",
      elapsed_seconds: 7200,
      comment: null,
      version: 1,
    },
    {
      id: "i-2",
      work_type: "ON_SITE",
      start_at: "2026-07-06T10:15:00+02:00",
      end_at: "2026-07-06T12:00:00+02:00",
      start_local: "2026-07-06T10:15:00",
      end_local: "2026-07-06T12:00:00",
      elapsed_seconds: 6300,
      comment: null,
      version: 1,
    },
    {
      id: "i-3",
      work_type: "REMOTE",
      start_at: "2026-07-06T12:30:00+02:00",
      end_at: "2026-07-06T14:00:00+02:00",
      start_local: "2026-07-06T12:30:00",
      end_local: "2026-07-06T14:00:00",
      elapsed_seconds: 5400,
      comment: "Deep work",
      version: 1,
    },
    {
      id: "i-4",
      work_type: "REMOTE",
      start_at: "2026-07-06T14:15:00+02:00",
      end_at: "2026-07-07T00:30:00+02:00",
      start_local: "2026-07-06T14:15:00",
      end_local: "2026-07-07T00:30:00",
      elapsed_seconds: 36900,
      comment: null,
      version: 1,
    }, // cross-midnight
  ];
  intervals["2026-07-07"] = [
    {
      id: "i-5",
      work_type: "SHIFT",
      start_at: "2026-07-07T19:00:00+02:00",
      end_at: "2026-07-08T03:00:00+02:00",
      start_local: "2026-07-07T19:00:00",
      end_local: "2026-07-08T03:00:00",
      elapsed_seconds: 28800,
      comment: null,
      version: 1,
    },
  ];
  shifts.length = 0;
  shifts.push(
    {
      id: "shift-1",
      shift_kind: "SHIFT",
      status: "PLANNED",
      approved_revision: {
        id: "rev-1",
        revision_no: 1,
        planned_start_at: "2026-07-07T19:00:00+02:00",
        planned_end_at: "2026-07-08T03:00:00+02:00",
        communicated_at: "2026-07-01T10:00:00+02:00",
      },
      actual_start_at: null,
      actual_end_at: null,
      deviation: null,
      findings: [],
      paid_break_status: "EXPLICIT_FACT_REQUIRED",
      paid_breaks: [],
    },
    {
      id: "shift-2",
      shift_kind: "SHIFT",
      status: "PLANNED",
      approved_revision: {
        id: "rev-2",
        revision_no: 1,
        planned_start_at: "2026-07-21T06:00:00+02:00",
        planned_end_at: "2026-07-21T14:00:00+02:00",
        communicated_at: "2026-07-01T10:00:00+02:00",
      },
      actual_start_at: null,
      actual_end_at: null,
      deviation: null,
      findings: [],
      paid_break_status: "EXPLICIT_FACT_REQUIRED",
      paid_breaks: [],
    },
  );
  ocdCalls.length = 0;
  trips.length = 0;
  segmentsSeq = 1;
  packages.length = 0;
  requests.length = 0;
  grants.length = 0;
  packageFingerprint = "fp-package-" + Math.random().toString(36).slice(2);
  overtimeFingerprint = packageFingerprint;
}

async function mockApi(route: Route): Promise<void> {
  const request = route.request();
  const url = request.url();
  const method = request.method();
  const body = request.postDataJSON() as Record<string, unknown> | null;
  const path = new URL(url).pathname;

  // --- identity -----------------------------------------------------------
  if (url.endsWith("/api/v1/me") && method === "GET") {
    const member = users.find((row) => row.idp_subject === principal);
    // The bootstrap admin persona manages ACCESS only: no employee baseline,
    // so no personal ledger either — exactly the "cannot pivot into the
    // employee ledger" demo (server: capability-less principal + admin caps).
    const capabilities =
      principal === "admin"
        ? capabilitiesByPrincipal.admin
        : member && member.active
          ? provisionedCaps(member)
          : (capabilitiesByPrincipal[principal] ?? []);
    await json(route, {
      subject: principal,
      display_name: member?.legal_name ?? principal,
      capabilities,
    });
    return;
  }

  // --- administration ------------------------------------------------------
  if (url.includes("/api/v1/admin/system")) {
    await json(route, {
      application: { name: "chronobeam-backend", version: "0.3.0" },
      database: {
        reachable: true,
        schema_revision: "0010_role_assignment",
        expected_revision: "0010_role_assignment",
        schema_current: true,
      },
      auth: { mode: "DEVELOPMENT_HEADER", oidc_configured: false },
      integrations: {
        shift_plan: "SEED_SEAM",
        hr_absence: "BLOCKED_INTEGRATION",
        travel_feed: "BLOCKED_INTEGRATION",
        recipient_registry: "CONFIGURED",
        calendar: "LOADED",
      },
      calendar: {
        editions: [
          {
            version: "sh-statutory-2026-2035-v1",
            valid_from: "2026-01-01",
            valid_to: "2035-12-31",
            source: "SH_STATUTORY_2026-2035 (SFTG SH 2004 §2(1))",
          },
        ],
        first_uncovered_month: "2036-01",
      },
    });
    return;
  }
  if (path.startsWith("/api/v1/admin/users") && method === "POST") {
    const created: User = {
      id: `user-${userSeq++}`,
      external_employee_id: (body?.external_employee_id as string | null) ?? null,
      legal_name: String(body?.legal_name),
      email: (body?.email as string | null) ?? null,
      active: false,
      created_at: "2026-09-12T12:00:00Z",
      idp_subject: (body?.idp_subject as string | null) ?? null,
      has_idp_subject: ((body?.idp_subject as string | null) ?? null) !== null,
      // Provisioning creates the account, never the master data.
      has_effective_contract: false,
      has_effective_schedule: false,
      role_assignments: [],
      effective_capabilities: [],
    };
    users.push(created);
    await json(route, created, 201);
    return;
  }
  const roleMatch = url.match(/\/api\/v1\/admin\/users\/([^/]+)\/roles\/([^/]+)/);
  const userMatch = url.match(/\/api\/v1\/admin\/users\/([^/]+)/);
  if (roleMatch && method === "PUT") {
    const member = users.find((row) => row.id === roleMatch[1]);
    if (!member) return jsonError(route, "NOT_FOUND", "missing", 404);
    const existing = member.role_assignments.find(
      (row) => row.role === roleMatch[2] && !row.revoked_at,
    );
    if (existing) return json(route, existing);
    const assignment = {
      id: `assignment-${assignmentSeq++}`,
      role: roleMatch[2],
      granted_by_principal: "admin",
      granted_at: "2026-09-12T12:05:00Z",
      revoked_at: null,
    };
    member.role_assignments.push(assignment);
    member.effective_capabilities = provisionedCaps(member);
    return json(route, assignment);
  }
  if (roleMatch && method === "DELETE") {
    const member = users.find((row) => row.id === roleMatch[1]);
    const assignment = member?.role_assignments.find(
      (row) => row.role === roleMatch[2] && !row.revoked_at,
    );
    if (member && assignment) {
      assignment.revoked_at = "2026-09-12T12:10:00Z";
      member.effective_capabilities = provisionedCaps(member);
    }
    await route.fulfill({ status: 204, body: "" });
    return;
  }
  if (path.startsWith("/api/v1/admin/users") && method === "PATCH" && userMatch) {
    const member = users.find((row) => row.id === userMatch[1]);
    if (!member) return jsonError(route, "NOT_FOUND", "missing", 404);
    if (body?.active !== undefined) member.active = Boolean(body.active);
    if (body?.idp_subject !== undefined) member.idp_subject = body.idp_subject as string | null;
    member.effective_capabilities = provisionedCaps(member);
    return json(route, member);
  }
  if (path.startsWith("/api/v1/admin/users") && userMatch) {
    const member = users.find((row) => row.id === userMatch[1]);
    if (!member) return jsonError(route, "NOT_FOUND", "missing", 404);
    if (principal === "admin") return json(route, member);
    // non-admin seeing an ADMIN user detail must not happen; deny loudly
    return jsonError(route, "AUTH_FORBIDDEN", "Access to this resource is restricted.", 403);
  }
  if (path.startsWith("/api/v1/admin/users")) {
    // The summary is the detail MINUS its history and capabilities, PLUS the
    // ACTIVE role names — derived here the way the server derives them
    // (``revoked_at IS NULL``, sorted), so the fixture cannot drift from the
    // contract it is standing in for. The binding is reported as PRESENCE, so the
    // list cannot republish the subject it is standing in for either.
    const visible = users.map(
      ({ role_assignments, effective_capabilities: _c, idp_subject, ...summary }) => ({
        ...summary,
        has_idp_subject: idp_subject !== null,
        roles: role_assignments
          .filter((row) => row.revoked_at === null)
          .map((row) => row.role)
          .sort(),
      }),
    );
    await json(route, visible);
    return;
  }

  // --- employee ledger ----------------------------------------------------
  if (url.includes("/api/v1/me/profile")) {
    await json(route, {
      effective_at: "2026-07-06",
      employee: {
        external_employee_id: "emp-0101",
        legal_name: "Mara Klein (synthetic)",
        email: "mara@example.invalid",
        active: true,
      },
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
        mon_required_minutes: 348,
        tue_required_minutes: 348,
        wed_required_minutes: 348,
        thu_required_minutes: 348,
        fri_required_minutes: 348,
        sat_required_minutes: 0,
        sun_required_minutes: 0,
        mon_typical_start: "08:00:00",
        mon_typical_end: "16:48:00",
        tue_typical_start: "08:00:00",
        tue_typical_end: "16:48:00",
        wed_typical_start: "08:00:00",
        wed_typical_end: "16:48:00",
        thu_typical_start: "08:00:00",
        thu_typical_end: "16:48:00",
        fri_typical_start: "08:00:00",
        fri_typical_end: "16:48:00",
        sat_typical_start: null,
        sat_typical_end: null,
        sun_typical_start: null,
        sun_typical_end: null,
        source: "MASTER",
        version: 1,
      },
      preferences: {
        usual_work_type: "ON_SITE",
        preferred_calendar_view: "month",
        normal_day_template: [{ start: "08:00:00", end: "16:18:00", work_type: "ON_SITE" }],
      },
    });
    return;
  }
  if (url.includes("/api/v1/me/findings")) {
    await json(route, {
      closing_balance_minutes: 60,
      findings: [
        {
          rule_id: "WT-CORE-007",
          severity: "WARNING",
          date: "2026-07-06",
          message: "Rest period below required minimum.",
        },
        {
          rule_id: "TRAVEL-LOCAL-001",
          severity: "INFO",
          date: null,
          message: "Host-country rules apply during business travel.",
        },
      ],
    });
    return;
  }
  if (url.includes("/api/v1/me/work-intervals") && method === "POST") {
    const created = {
      id: `i-added-${intervals["2026-07-06"].length + 1}`,
      work_type: body?.work_type ?? "REMOTE",
      start_at: "2026-07-06T18:00:00+02:00",
      end_at: "2026-07-06T19:30:00+02:00",
      start_local: "2026-07-06T18:00:00",
      end_local: "2026-07-06T19:30:00",
      elapsed_seconds: 5400,
      comment: null,
      version: 1,
    };
    intervals["2026-07-06"].push(created);
    await json(route, created, 201);
    return;
  }
  const dayMatch = url.match(/\/api\/v1\/me\/months\/2026-07\/days\/([0-9-]+)/);
  if (dayMatch) {
    await json(route, { date: dayMatch[1], intervals: intervals[dayMatch[1]] ?? [] });
    return;
  }
  if (url.includes("/api/v1/me/months/2026-07") && method === "POST" && url.includes("/review")) {
    await json(route, {
      period_month: "2026-07-01",
      status: "REVIEWED",
      interval_count: 6,
      closing_balance_minutes: 60,
      days: [
        { date: "2026-07-06", interval_count: 5 },
        { date: "2026-07-07", interval_count: 1 },
      ],
      daily_results: [
        {
          date: "2026-07-06",
          worked_minutes: 510,
          required_minutes: 348,
          delta_minutes: 162,
          calendar_class: "WEEKDAY",
          account_credit_minutes: 510,
        },
      ],
    });
    return;
  }
  if (url.includes("/api/v1/me/months/2026-07")) {
    await json(route, {
      period_month: "2026-07-01",
      status: "OPEN",
      interval_count: 6,
      closing_balance_minutes: 60,
      days: Object.entries(intervals).map(([date, list]) => ({
        date,
        interval_count: list.length,
      })),
      daily_results: [
        {
          date: "2026-07-06",
          worked_minutes: 510,
          required_minutes: 348,
          delta_minutes: 162,
          calendar_class: "WEEKDAY",
          account_credit_minutes: 510,
        },
        {
          date: "2026-07-07",
          worked_minutes: 480,
          required_minutes: 348,
          delta_minutes: 132,
          calendar_class: "WEEKDAY",
          account_credit_minutes: 480,
        },
      ],
    });
    return;
  }

  // --- shift --------------------------------------------------------------
  if (url.includes("/api/v1/me/shifts") && method === "POST" && url.endsWith("/confirm")) {
    const id = url.match(/shifts\/([^/]+)\/confirm/)?.[1];
    const shift = shifts.find((row) => row.id === id);
    if (!shift) return jsonError(route, "NOT_FOUND", "missing", 404);
    shift.status = "PLANNED";
    shift.actual_start_at = shift.approved_revision.planned_start_at;
    shift.actual_end_at = shift.approved_revision.planned_end_at;
    await json(route, shift);
    return;
  }
  if (url.includes("/api/v1/me/shifts") && method === "POST" && url.endsWith("/deviation")) {
    const id = url.match(/shifts\/([^/]+)\/deviation/)?.[1];
    const shift = shifts.find((row) => row.id === id);
    if (!shift) return jsonError(route, "NOT_FOUND", "missing", 404);
    shift.deviation = {
      id: `deviation-${id}`,
      deviation_type: body?.deviation_type,
      actual_start_at: body?.actual_start_local ? "2026-07-21T06:30:00+02:00" : null,
      actual_end_at: body?.actual_end_local ? "2026-07-21T13:30:00+02:00" : null,
      noticed_or_decided_at: body?.noticed_or_decided_local ?? null,
      decision_maker_type: body?.decision_maker_type ?? null,
      reason: body?.reason ?? null,
      version: 1,
    };
    shift.actual_start_at = (shift.deviation?.actual_start_at as string | null) ?? null;
    shift.actual_end_at = (shift.deviation?.actual_end_at as string | null) ?? null;
    await json(route, shift);
    return;
  }
  if (url.includes("/api/v1/me/shifts") && url.includes("/report-preview")) {
    await json(route, {
      report_type: "SHIFT_REPORT",
      policy_version: "2026-shift-v1",
      policy_status: "PROVISIONAL",
      employee_identifier: "emp-0101",
      shift_assignment_id: "shift-2",
      planned_start_at: "2026-07-21T06:00:00+02:00",
      planned_end_at: "2026-07-21T14:00:00+02:00",
      actual_start_at: "2026-07-21T06:30:00+02:00",
      actual_end_at: "2026-07-21T13:30:00+02:00",
      deviation_type: "SHORTENED",
      findings: [],
    });
    return;
  }
  if (url.includes("/api/v1/me/shifts")) {
    await json(route, shifts);
    return;
  }

  // --- OCD ----------------------------------------------------------------
  if (url.includes("/api/v1/me/ocd-calls") && method === "POST") {
    const created = {
      id: `call-${ocdCalls.length + 1}`,
      mode: body?.mode ?? "ON_SITE",
      call_start_at: "2026-07-19T23:42:00+02:00",
      call_end_at: "2026-07-20T00:18:00+02:00",
      travel_out_minutes: body?.travel_out_minutes ?? null,
      travel_back_minutes: body?.travel_back_minutes ?? null,
      comment: null,
      status: "RECORDED",
      version: 1,
      linked_interval_id: "i-call-1",
      span_start_at: "2026-07-19T23:17:00+02:00",
      span_end_at: "2026-07-20T00:43:00+02:00",
      elapsed_seconds: 5160,
    };
    ocdCalls.push(created);
    await json(route, created, 201);
    return;
  }
  if (url.includes("/api/v1/me/ocd-assignments/") && url.includes("/compensation-preview")) {
    await json(route, {
      report_type: "OCD_COMPENSATION",
      policy_version: "2026-ocd-v1",
      policy_status: "CONFIRMED",
      employee_identifier: "emp-0101",
      ocd_assignment_id: "ocd-1",
      assignment_start_at: "2026-07-19T19:00:00+02:00",
      assignment_end_at: "2026-07-20T07:00:00+02:00",
      calls: ocdCalls,
      classified_components: [
        {
          rule_id: "OCD_CALL_BASE",
          component: "CALL_BASE",
          minutes: 36,
          time_equivalent_minutes: null,
        },
      ],
      duty: {
        duty_minutes: 720,
        duty_kind: "DUTY_GE_12H",
        units: 4,
        basis_points: 1250,
        short_notice: false,
      },
      election: { mode: "TIME", version: 1, selected_at: "2026-07-21T09:00:00+02:00" },
      findings: [],
    });
    return;
  }
  if (url.includes("/api/v1/me/ocd?")) {
    await json(route, [
      {
        id: "ocd-1",
        start_at: "2026-07-19T19:00:00+02:00",
        end_at: "2026-07-20T07:00:00+02:00",
        status: "SCHEDULED",
        scheduled_at: "2026-07-10T08:00:00+02:00",
        source: "ADMIN_SEED",
        calls: ocdCalls,
        findings: [],
      },
    ]);
    return;
  }

  // --- overtime -----------------------------------------------------------
  if (url.includes("/api/v1/me/overtime/preview")) {
    overtimeFingerprint = packageFingerprint;
    await json(route, {
      report_type: "OVERTIME",
      policy_version: "2026-ot-v1",
      policy_status: "CONFIRMED",
      employee_identifier: "emp-0101",
      period_start: "2026-07-01",
      period_end: "2026-08-01",
      authorizations: otAuthorization ? [otAuthorization] : [],
      overtime_intervals: [
        {
          interval_id: "ot-1",
          start_at: "2026-07-15T18:00:00+02:00",
          end_at: "2026-07-15T20:30:00+02:00",
          work_type: "OVERTIME_PAID",
          elapsed_seconds: 9000,
          linked_authorization_id: otLinked ? "ot-auth-1" : null,
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
      election: { mode: "PAY", version: 1, selected_at: "2026-07-21T09:30:00+02:00" },
      findings: [],
      preview_fingerprint: overtimeFingerprint,
    });
    return;
  }
  if (url.includes("/api/v1/me/compensation-elections") && method === "POST") {
    await json(
      route,
      {
        id: "election-1",
        context_type: body?.context_type,
        context_id: body?.context_id,
        mode: body?.mode,
        version: 1,
        selected_at: "2026-07-21T09:30:00+02:00",
      },
      201,
    );
    return;
  }
  if (url.includes("/api/v1/me/compensation-elections")) {
    await json(route, {
      id: "election-1",
      context_type: "OVERTIME",
      context_id: "ot-1",
      mode: "PAY",
      version: 1,
      selected_at: "2026-07-21T09:30:00+02:00",
    });
    return;
  }
  if (url.includes("/api/v1/me/overtime-authorizations") && method === "POST") {
    // An employee POST is a REQUEST: no status, no approver, no source.
    otAuthorization = {
      id: "ot-auth-1",
      valid_from: (body?.valid_from as string) ?? "2026-07-01T00:00",
      valid_to: (body?.valid_to as string | null) ?? null,
      purpose: (body?.purpose as string | null) ?? null,
      status: "REQUESTED",
      authorized_by: null,
      source_ref: (body?.source_ref as string | null) ?? null,
      source: "EMPLOYEE_RECORDED",
      version: 1,
    };
    await json(route, otAuthorization, 201);
    return;
  }
  if (url.includes("/api/v1/me/overtime-authorizations")) {
    await json(route, otAuthorization ? [otAuthorization] : []);
    return;
  }
  if (url.includes("/api/v1/me/work-intervals/") && method === "PATCH") {
    if ((body?.overtime_authorization_id as string | undefined) === "ot-auth-1") otLinked = true;
    await json(route, { id: url.split("/").pop(), version: 2 });
    return;
  }

  // --- travel -------------------------------------------------------------
  if (url.includes("/api/v1/me/travel-trips") && method === "POST" && !url.includes("/segments")) {
    const created = {
      id: `trip-${trips.length + 1}`,
      start_at: "2026-07-22T08:00:00+02:00",
      end_at: "2026-07-22T18:00:00+02:00",
      start_local: "2026-07-22T08:00:00",
      end_local: "2026-07-22T18:00:00",
      start_origin: body?.start_origin ?? "HOME",
      source: "EMPLOYEE_DECLARED",
      reference_label: (body?.reference_label as string | null) ?? null,
      version: 1,
      segments: [],
    };
    trips.push(created);
    await json(route, created, 201);
    return;
  }
  const segmentMatch = url.match(/\/api\/v1\/me\/travel-trips\/([^/]+)\/segments/);
  if (segmentMatch && method === "POST") {
    const trip = trips.find((row) => row.id === segmentMatch[1]);
    if (!trip) return jsonError(route, "NOT_FOUND", "missing", 404);
    const segment = {
      id: `segment-${segmentsSeq++}`,
      trip_id: trip.id,
      start_at: "2026-07-22T08:00:00+02:00",
      end_at: "2026-07-22T09:30:00+02:00",
      start_local: "2026-07-22T08:00:00",
      end_local: "2026-07-22T09:30:00",
      classification: String(body?.classification ?? "DRIVING"),
      basis: String(body?.basis ?? "ACTUAL"),
      work_interval_id: body?.classification === "DRIVING" ? "i-6" : null,
      version: 1,
    };
    trip.segments.push(segment);
    await json(route, segment, 201);
    return;
  }
  if (url.includes("/api/v1/me/travel-trips")) {
    await json(route, { trips });
    return;
  }
  if (url.includes("/api/v1/me/travel-summary")) {
    const hasDriving = trips.some((trip) =>
      trip.segments.some((segment) => segment.classification === "DRIVING"),
    );
    await json(route, {
      period_month: "2026-07-01",
      topups: [],
      credits: hasDriving
        ? [
            {
              occurred_on: "2026-08-01",
              minutes: 15,
              rule_id: "TRAVEL-004",
              source_entity_type: "business_trip",
              source_entity_id: trips[0].id,
              window_start: "2026-07-22",
              window_end: "2026-07-22",
              eligible_minutes: 570,
              threshold_minutes: 900,
              excess_minutes: 0,
            },
          ]
        : [],
      trips: trips.map((trip) => ({
        trip_id: trip.id,
        window_start: "2026-07-22",
        window_end: "2026-07-22",
        eligible_minutes: 570,
        threshold_minutes: 900,
        excess_minutes: 0,
        credit_minutes: 0,
        credit_effective_on: "2026-08-01",
      })),
      findings: [
        {
          rule_id: "TRAVEL-LOCAL-001",
          severity: "INFO",
          date: null,
          message: "Host-country rules apply during business travel.",
        },
      ],
      input_fingerprint: "fp-" + trips.length,
    });
    return;
  }

  // --- submissions / disclosure ------------------------------------------
  if (url.includes("/api/v1/me/submission-recipients")) {
    // Server-controlled registry: the employee chooses explicitly (INV-05).
    await json(route, [{ subject: "demo-recipient", readable: true }]);
    return;
  }
  if (url.includes("/api/v1/me/submissions") && method === "POST") {
    if (body?.preview_fingerprint !== packageFingerprint) {
      return jsonError(
        route,
        "SUBMISSION_CHANGED",
        "Facts changed since preview; re-preview before submitting.",
        409,
      );
    }
    const created = {
      id: `package-${packages.length + 1}`,
      purpose: body?.purpose ?? "OVERTIME_PAID",
      period_start: "2026-07-01",
      period_end: "2026-07-31",
      version: 1,
      status: "SUBMITTED",
      supersedes_id: null,
      created_at: "2026-07-31T09:00:00+02:00",
      employee_confirmed_at: "2026-07-31T09:00:00+02:00",
      recipients: ((body?.recipients as string[]) ?? ["recipient"]).map((subject) => ({
        recipient_principal: subject,
        recipient_type: "RECIPIENT",
        read_at: null,
      })),
      payload: {
        report_type: "OVERTIME",
        policy_version: "2026-ot-v1",
        policy_status: "CONFIRMED",
        employee_identifier: "emp-0101",
        period_start: "2026-07-01",
        period_end: "2026-07-31",
        authorizations: [],
        overtime_intervals: [],
        classified_components: [],
        election: { id: "election-1", mode: "PAY", version: 1 },
      },
    };
    packages.push(created);
    await json(route, created, 201);
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
      packages.map(({ payload: _payload, ...meta }) => meta),
    );
    return;
  }
  if (url.includes("/api/v1/hr/access-requests") && method === "POST") {
    const created = {
      id: `request-${requests.length + 1}`,
      requester_principal: "hr-requester",
      purpose: body?.purpose ?? "RANDOM_CHECK",
      period_start: "2026-07-01",
      period_end: "2026-07-31",
      view_type: "work-time-record",
      message: body?.message ?? null,
      status: "PENDING",
      created_at: "2026-07-25T09:00:00+02:00",
      closed_at: null,
      grant_id: null,
    };
    requests.push(created);
    await json(route, created, 201);
    return;
  }
  if (url.includes("/api/v1/hr/access-requests")) {
    await json(
      route,
      requests.map((row) => ({
        ...row,
        grant_id: row.status === "GRANTED" ? (grants[grants.length - 1]?.id ?? null) : null,
      })),
    );
    return;
  }
  if (url.includes("/api/v1/me/access-requests") && url.endsWith("/grant")) {
    const id = url.match(/access-requests\/([^/]+)\/grant/)?.[1];
    const pending = requests.find((row) => row.id === id);
    if (!pending) return jsonError(route, "NOT_FOUND", "missing", 404);
    const grant = {
      id: `grant-${grants.length + 1}`,
      access_request_id: pending.id,
      employee_identifier: "emp-0101",
      grantee_principal: "hr-requester",
      purpose: pending.purpose,
      period_start: "2026-07-01",
      period_end: "2026-07-31",
      view_type: "work-time-record",
      granted_at: "2026-07-26T09:00:00+02:00",
      expires_at: null,
      supersedes_id: null,
      active: true,
    };
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
    await json(route, {
      grant_id: "grant-1",
      purpose: "RANDOM_CHECK",
      period_start: "2026-07-01",
      period_end: "2026-07-31",
      view_type: "work-time-record",
      granted_at: "2026-07-26T09:00:00+02:00",
      snapshot: {
        employee_identifier: "emp-0101",
        period_start: "2026-07-01",
        period_end: "2026-07-31",
        generated_at: "2026-07-26T09:00:00+02:00",
        daily: [
          { date: "2026-07-06", worked_minutes: 510, required_minutes: 348, delta_minutes: 162 },
        ],
        intervals: intervals["2026-07-06"].map(
          ({ id: _id, work_type, start_at, end_at, elapsed_seconds }) => ({
            start_at,
            end_at,
            work_type,
            elapsed_seconds,
          }),
        ),
      },
    });
    return;
  }

  progress = progress || "unhandled:" + url;
  await json(route, {});
}

test.beforeEach(async ({ page }) => {
  principal = "admin";
  progress = "";
  seed();
  await page.route("**/api/v1/**", mockApi);
});

test("full product: administration, employee work, shift/OCD/overtime/travel, one package, reload persistence", async ({
  page,
}) => {
  // ---------------------------------------------------------------- ADMIN
  await page.goto("/admin");
  await expect(page.getByRole("heading", { name: "Administration" })).toBeVisible();
  // identity chip in the header duplicates the table row, so scope to rows
  await expect(
    // 10.3: the user list is a table now, so rows resolve as rows.
    page.getByRole("row").filter({ hasText: "Ada Admin (synthetic)" }),
  ).toBeVisible();
  await expect(page.getByRole("row").filter({ hasText: "Lena Demo (synthetic)" })).toBeVisible();
  // Readiness is on the LIST now: a fully provisioned account reads Ready, and one
  // that was never finished names what it is missing — no click-through needed.
  await expect(page.getByRole("row").filter({ hasText: "Ada Admin (synthetic)" })).toContainText(
    "Ready",
  );
  await expect(page.getByRole("row").filter({ hasText: "Lena Demo (synthetic)" })).toContainText(
    "Missing SSO",
  );

  // Provision Mara (inactive by default), then activate.
  await page.getByRole("button", { name: "Provision user" }).click();
  await page.getByLabel(/Legal name/).fill("Mara Klein (synthetic)");
  await page.getByLabel(/Identity subject/).fill("mara");
  await page.getByLabel(/External employee id/).fill("emp-0101");
  await page.getByLabel("Email").fill("mara@example.invalid");
  await page.getByRole("button", { name: "Provision user" }).click();
  await expect(page.getByRole("heading", { name: "Mara Klein (synthetic)" })).toBeVisible();
  const maraRow = page.getByRole("row").filter({ hasText: "Mara Klein (synthetic)" });
  await expect(maraRow.locator("td", { hasText: /^Inactive$/ })).toBeVisible();
  await page.getByRole("button", { name: "Activate account" }).click();
  await page.getByRole("button", { name: /Activate Mara Klein/ }).click();
  await expect(maraRow.locator("td", { hasText: /^Active$/ })).toBeVisible();

  // Assign a role, then revoke it — access follows the assignment.
  await page.getByLabel("Assign role").selectOption("SUBMISSION_RECIPIENT");
  await page.getByRole("button", { name: "Assign", exact: true }).click();
  await page.getByRole("button", { name: /Assign Submission recipient/ }).click();
  await expect(page.getByText("SUBMISSION_READ_ASSIGNED")).toBeVisible();
  await page.getByRole("button", { name: "Revoke", exact: true }).click();
  await page.getByRole("button", { name: /Revoke Submission recipient/ }).click();
  await expect(page.getByText("SUBMISSION_READ_ASSIGNED")).toHaveCount(0);

  // System tab: human statuses lead; engine codes/revisions behind details.
  await page.getByRole("tab", { name: "System" }).click();
  await expect(page.getByText("Application available")).toBeVisible();
  await expect(page.getByText("Database connected")).toBeVisible();
  await expect(page.getByText("Development identity mode (test only)")).toBeVisible();
  await expect(page.getByText("External HR absence system", { exact: true })).toBeVisible();
  // The calendar answers "what is covered, and when does it run out?" rather than
  // only reporting LOADED.
  await expect(page.getByRole("heading", { name: "Calendar" })).toBeVisible();
  await expect(page.getByText("sh-statutory-2026-2035-v1")).toBeVisible();
  await expect(page.getByText("01 Jan 2026 → 31 Dec 2035")).toBeVisible();
  await expect(
    page.getByText("Coverage gap: January 2036 is the first month with no covering edition."),
  ).toBeVisible();
  await expect(page.getByText(/chronobeam-backend/)).toBeHidden();
  await page.locator(".admin-system-card", { hasText: "Application" }).locator("summary").click();
  await expect(page.getByText(/chronobeam-backend/)).toBeVisible();
  await expect(page.getByText(/Mara Klein|emp-0101|worked_minutes/i)).toHaveCount(0);

  // ------------------------------------------------------------ EMPLOYEE
  principal = "mara";
  await page.goto("/profile");
  await expect(page.getByRole("heading", { name: /Profile/ })).toBeVisible();
  await expect(page.getByRole("main").getByText("Mara Klein (synthetic)").first()).toBeVisible();
  // UI-038: the working-days group reads the full day name with no letter
  // prefix. Scoped to the group: the day-target grid below renders the same
  // names, so an unscoped getByText would match twice.
  const workingDays = page.locator(".working-day-picker");
  await expect(workingDays.getByText("Monday", { exact: true })).toBeVisible(); // schedule section present
  await expect(workingDays).not.toContainText("M Monday");

  await page.goto("/month/2026-07");
  await expect(page.getByText("Private — visible only to you").first()).toBeVisible();
  // Multi-interval mixed day with a cross-midnight interval and a finding.
  await page.getByRole("button", { name: /6.*4 intervals/i }).click();
  await expect(page.getByText("08:00–10:00")).toBeVisible();
  await expect(page.getByText("Deep work")).toBeVisible();
  await expect(page.getByText("14:15–00:30")).toBeVisible(); // cross-midnight
  // findings surface collapsed (summarized) and expand to full messages
  await expect(page.getByText("2 things to check")).toBeVisible();
  await page.locator("details.finding-pin summary").click();
  await expect(page.getByText("Rest period below required minimum.").first()).toBeVisible();
  // Add an interval.
  await page.getByRole("button", { name: "+ Add interval" }).click();
  await page.getByLabel("New start time").fill("18:00");
  await page.getByLabel("New end time").fill("19:30");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  await expect(page.getByText("18:00–19:30")).toBeVisible();

  // -------------------------------------------------------------- SHIFT
  // Shifts live in the selected day's timeline: open 2026-07-07 first.
  // Exact-as-planned confirmation on shift 1 (2026-07-07).
  await page.getByRole("button", { name: /^Tuesday, 7 July 2026/ }).click();
  await page.getByRole("button", { name: "Confirm worked as planned" }).first().click();
  // F26: the card announces the confirmation and refreshes (action gone)
  await expect(page.getByText("Shift confirmed as planned.")).toBeVisible();
  await expect(page.getByText("Not confirmed yet")).toHaveCount(0, { timeout: 10_000 });
  // Deviation on shift 2 (2026-07-21).
  await page.getByRole("button", { name: /^Tuesday, 21 July 2026/ }).click();
  await page.getByRole("button", { name: "Record a deviation" }).click();
  await page.getByLabel("Deviation").selectOption("SHORTENED");
  await page.getByRole("button", { name: /Save deviation/ }).click();
  // deviation saved: the card moves to preview mode with a Shift Report action
  await expect(page.getByRole("button", { name: "Preview Shift Report" })).toBeVisible({
    timeout: 10_000,
  });

  // Review the month.
  await page.getByRole("link", { name: "Review this month" }).click();
  await expect(page.getByRole("heading", { name: "Month review" })).toBeVisible();
  await page.getByRole("checkbox", { name: /I have read these/ }).check();
  await page.getByRole("button", { name: "Mark month reviewed" }).click();
  await expect(page.getByText("Month marked reviewed. No data was shared.")).toBeVisible();

  // --------------------------------------------------------------- OCD
  await page.goto("/month/2026-07/ocd");
  await expect(page.getByRole("heading", { name: /On-call duty — July 2026/ })).toBeVisible();
  await page
    .getByLabel(/^OCD assignment 19 Jul 2026/)
    .getByRole("button", { name: "Add call" })
    .click();
  await page.getByLabel("Mode").selectOption("ON_SITE");
  await page.getByLabel("Call start").fill("2026-07-19T23:42");
  await page.getByLabel("Call end").fill("2026-07-20T00:18");
  await page.getByRole("button", { name: "Record call + work time" }).click();
  await expect(page.locator(".status-ok")).toContainText(
    "Call recorded together with its work time",
  );
  await page.getByRole("button", { name: "Preview OCD compensation" }).click();
  await expect(page.getByText("OCD compensation preview — not shared")).toBeVisible();
  await expect(page.getByText("No monetary amount is calculated")).toBeVisible();

  // ----------------------------------------------------------- OVERTIME
  await page.goto("/month/2026-07/overtime");
  await expect(page.getByRole("heading", { name: /Overtime — July 2026/ })).toBeVisible();
  // F04 readiness: record an authorization reference first.
  await page.getByRole("button", { name: "Add authorization reference" }).click();
  await page.getByLabel("Valid from").fill("2026-07-01T00:00");
  await page.getByLabel("Purpose").fill("Beamtime support");
  await page.getByRole("button", { name: "Save reference" }).click();
  await expect(page.locator(".status-ok")).toContainText("Authorization reference recorded");
  await page.getByRole("button", { name: "Show / refresh preview" }).click();
  await expect(page.getByText("Overtime preview — not shared")).toBeVisible();
  await expect(page.getByText("Overtime supplement")).toBeVisible();
  await expect(page.getByText(/no authorization reference/)).toBeVisible();
  // Link the reference to the overtime interval so the package becomes submittable.
  await page.getByLabel(/Authorization for interval/).selectOption("ot-auth-1");
  await page.getByRole("button", { name: "Link reference" }).click();
  // the linked interval now carries a persistent marker (the transient notice
  // lives in the row, which unmounts once the preview refresh completes)
  await expect(page.getByText(/· authorization reference linked/)).toBeVisible();
  await expect(page.getByText(/no authorization reference/)).toHaveCount(0, { timeout: 10_000 });

  // ------------------------------------------------------------- TRAVEL
  await page.goto("/travel/2026-07");
  await page.getByRole("button", { name: /Add business trip/i }).click();
  await page.getByLabel("Trip start date").fill("2026-07-22");
  await page.getByLabel("Trip start time").fill("08:00");
  await page.getByLabel("Trip end date").fill("2026-07-22");
  await page.getByLabel("Trip end time").fill("18:00");
  await page.getByLabel("Trip start origin").selectOption("HOME");
  await page.getByRole("button", { name: "Create trip" }).click();
  await expect(page.getByText(/Business trip · Wed, 22 Jul/)).toBeVisible();
  await expect(page.getByText("employee-declared", { exact: true })).toBeVisible();
  // Driving segment (actual work in the ledger).
  await page
    .getByRole("button", { name: /Add segment/i })
    .first()
    .click();
  await page.getByLabel("Segment start date").fill("2026-07-22");
  await page.getByLabel("Segment start time").fill("08:00");
  await page.getByLabel("Segment end date").fill("2026-07-22");
  await page.getByLabel("Segment end time").fill("09:30");
  await page.getByLabel("Segment classification").selectOption("DRIVING");
  await page.getByRole("button", { name: "Add segment" }).click();
  await expect(page.getByText("Driving", { exact: true })).toBeVisible();
  await expect(page.getByText("working time in your ledger")).toBeVisible();
  // Passive segment.
  await page
    .getByRole("button", { name: /Add segment/i })
    .first()
    .click();
  await page.getByLabel("Segment start date").fill("2026-07-22");
  await page.getByLabel("Segment start time").fill("12:00");
  await page.getByLabel("Segment end date").fill("2026-07-22");
  await page.getByLabel("Segment end time").fill("14:00");
  await page.getByLabel("Segment classification").selectOption("PASSIVE_TRAVEL");
  await page.getByRole("button", { name: "Add segment" }).click();
  await expect(page.getByText("Passive travel", { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/travel time, not working time/)).toBeVisible();
  // Threshold/credit display.
  await expect(page.getByText(/15:00/i).first()).toBeVisible();

  // --------------------------------------------- SUBMISSIONS (ONE package)
  await page.goto("/month/2026-07/submissions");
  await expect(page.getByRole("heading", { name: /Submissions & Sharing/ })).toBeVisible();
  const payCard = page
    .locator("section.submission-card")
    .filter({ hasText: "Submit Overtime — Pay" });
  await payCard.getByLabel("Overtime month").fill("2026-07");
  await payCard.getByRole("button", { name: "Preview exact package — Overtime — Pay" }).click();
  const payPanel = page.locator("section.preview-panel");
  await expect(payPanel.getByText("What the recipient will see")).toBeVisible();
  // The human component label carries the line; the engine code is gone (7A).
  await expect(payPanel.getByText(/Overtime supplement: /)).toBeVisible();
  // The recipient is chosen explicitly; nothing is pre-selected (INV-05).
  await payCard.getByRole("checkbox", { name: "demo-recipient" }).check();
  await payPanel.getByRole("button", { name: "Submit this Overtime — Pay package" }).click();
  await expect(page.locator(".status-ok, .status-error").last()).toContainText(
    "Recipients can read only this package.",
    {
      timeout: 10_000,
    },
  );
  await expect(page.getByText("Version 1 · Submitted")).toBeVisible();

  // ------------------------------------------------------- PERSISTENCE
  await page.reload();
  await expect(page.getByText("Version 1 · Submitted")).toBeVisible(); // package survives reload
  await page.goto("/travel/2026-07");
  await expect(page.getByText("Driving", { exact: true })).toBeVisible(); // trip survives reload
  await page.goto("/month/2026-07");
  await page.getByRole("button", { name: /^Monday, 6 July 2026/ }).click(); // open the day panel
  await expect(page.getByText("18:00–19:30")).toBeVisible(); // added interval survives reload
});

test("random-check disclosure: HR sees nothing until the employee opens exactly the requested scope", async ({
  page,
}) => {
  principal = "hr-requester";
  await page.goto("/access-requests");
  await page.getByLabel("Employee ID").fill("emp-0101");
  await page.getByLabel("Month").fill("2026-07");
  await page.getByRole("button", { name: "Request random check" }).click();
  await expect(
    page.getByText("Random check · 01.07.2026–31.07.2026 · Waiting for the employee"),
  ).toBeVisible();
  // metadata only: no worked time anywhere on the page
  await expect(page.getByText(/worked_minutes|intervals|elapsed_seconds/i)).toHaveCount(0);

  principal = "mara";
  await page.goto("/access-requests");
  // employee view renders purpose + period (no status suffix)
  await expect(page.getByText("Random check · 01.07.2026–31.07.2026")).toBeVisible();
  await page.getByRole("button", { name: /Open exactly this scope/ }).click();
  await expect(page.locator(".status-ok, .status-error").last()).toContainText("opened", {
    timeout: 10_000,
  });

  principal = "hr-requester";
  await page.goto("/access-requests");
  await expect(page.getByText("Opened by the employee").first()).toBeVisible();
  await page.getByRole("listitem").getByRole("button", { name: "Open the granted view" }).click();
  // human-readable frozen rows with European dates (no raw ISO/offset strings)
  const frozen = page.locator(".frozen-view");
  await expect(frozen.getByText("06.07.2026", { exact: true })).toBeVisible();
  await expect(frozen.getByText(/06 Jul 2026, 08:00 → 06 Jul 2026/)).toBeVisible();
  await expect(frozen.locator(".tech-details")).toHaveCount(0);
  // 7A: the raw JSON is gone; the employee it names is not.
  await expect(frozen.getByText(/Employee reference: emp-0101/)).toBeVisible();
  // exactly the requested month; no ledger browsing, no other months
  await expect(page.getByText(/2026-06|2026-08/)).toHaveCount(0);
});

test("application admin manages the service but cannot browse the employee ledger", async ({
  page,
}) => {
  principal = "admin";
  await page.goto("/admin");
  await expect(page.getByRole("heading", { name: "Administration" })).toBeVisible();
  // identity-only surface: no work-time fields in the user list
  await expect(page.getByText(/worked_minutes|interval_count|closing_balance/i)).toHaveCount(0);
  await page.getByRole("tab", { name: "System" }).click();
  await expect(page.getByText("Application available")).toBeVisible();
  await page.locator(".admin-system-card", { hasText: "Application" }).locator("summary").click();
  await expect(page.getByText(/chronobeam-backend/)).toBeVisible();

  // The server denies ledger routes for ADMIN: the month page shows the
  // forbidden state and no interval data is rendered (mock returns 403).
  await page.route("**/api/v1/me/months/**", (route) =>
    route.fulfill({
      status: 403,
      contentType: "application/json",
      body: JSON.stringify({
        detail: { code: "AUTH_FORBIDDEN", message: "Access to this resource is restricted." },
      }),
    }),
  );
  await page.goto("/month/2026-07");
  await expect(page.getByText("This workspace belongs to another account.")).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.getByText(/08:00–10:00|elapsed_seconds/)).toHaveCount(0);
});
