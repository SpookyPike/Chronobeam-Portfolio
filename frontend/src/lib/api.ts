export type WorkType =
  | "ON_SITE"
  | "REMOTE"
  | "SHIFT"
  | "OCD_CALL"
  | "OVERTIME_PAID"
  | "OVERTIME_TIME_COMP"
  | "BUSINESS_TRIP_WORK";

export interface HealthResponse {
  status: string;
  service: string;
}

export interface Identity {
  subject: string;
  display_name: string;
  capabilities: Capability[];
}

export type Capability =
  | "OWN_LEDGER_READ"
  | "OWN_LEDGER_WRITE"
  | "OWN_SUBMISSION_CREATE"
  | "OWN_DISCLOSURE_GRANT"
  | "SUBMISSION_READ_ASSIGNED"
  | "ACCESS_REQUEST_CREATE"
  | "DISCLOSURE_READ_GRANTED"
  | "ACCESS_ADMIN"
  | "SYSTEM_STATUS_READ"
  // Pilot plan entry — two independent per-domain switches. Each gates only its
  // own domain's controls; never treat one as implying the other.
  | "SHIFT_PLAN_ENTRY_WRITE"
  | "OCD_PLAN_ENTRY_WRITE";

export interface Interval {
  id: string;
  work_type: WorkType;
  start_at: string;
  end_at: string;
  start_local: string;
  end_local: string;
  elapsed_seconds: number;
  comment: string | null;
  version: number;
}

/** Explicit factual break (WT-BACKEND §5.3). Separate from `Interval`: a break
 *  is not work. Only `UNPAID` reduces worked time; `PAID_SHIFT_BREAK` is
 *  compensated presence (SHIFT-003) and `OTHER` must not tighten the result. */
export type BreakKind = "UNPAID" | "PAID_SHIFT_BREAK" | "OTHER";

export interface BreakInterval {
  id: string;
  kind: BreakKind;
  start_at: string;
  end_at: string;
  start_local: string;
  end_local: string;
  elapsed_seconds: number;
  linked_shift_assignment_id: string | null;
  version: number;
  supersedes_id: string | null;
  client_request_id: string | null;
}

export type CalendarClass = "WEEKDAY" | "PUBLIC_HOLIDAY" | "EMPLOYER_FREE";

export interface DailyResult {
  date: string;
  worked_minutes: number;
  required_minutes: number;
  delta_minutes: number;
  /** The resolved calendar classification of the day (institutional fact). */
  calendar_class: CalendarClass;
  /** Minutes this day CONTRIBUTES to the time account. `delta_minutes` is
   *  derived from it (`account_credit - required`), so the two must be read
   *  together; the frontend never re-derives either. */
  account_credit_minutes: number;
}

export interface MonthSummary {
  period_month: string;
  status: "OPEN" | "REVIEWED" | "CLOSED";
  interval_count: number;
  days: Array<{ date: string; interval_count: number }>;
  daily_results: DailyResult[];
  closing_balance_minutes: number;
}

export interface Finding {
  rule_id: string;
  severity: "WARNING" | "CRITICAL" | "BLOCKED_POLICY";
  date: string | null;
  message: string;
}

export interface FindingsResponse {
  closing_balance_minutes: number;
  findings: Finding[];
}

export interface TemplateInterval {
  start: string;
  end: string;
  ends_next_day?: boolean;
  work_type: WorkType;
  comment?: string | null;
}

export interface ShiftFinding {
  rule_id: string;
  status: "INFO" | "WARNING" | "CRITICAL" | "BLOCKED_POLICY";
  message: string;
}

export interface ShiftDeviation {
  id: string;
  deviation_type: string;
  actual_start_at: string | null;
  actual_end_at: string | null;
  noticed_or_decided_at: string | null;
  decision_maker_type: string | null;
  reason: string | null;
  version: number;
}

export interface Shift {
  id: string;
  shift_kind: string;
  /** `WITHDRAWN` is the employee retracting their OWN plan record (it was never
   *  the real plan) — NOT a cancellation, and it carries no entitlement. Only
   *  `PLANNED` is an active plan; every other status is retained lineage. */
  status: "PLANNED" | "WITHDRAWN" | "CANCELLED" | "SUPERSEDED";
  /** Plan provenance (D2): `EMPLOYEE_RECORDED` for anything the employee
   *  entered, a member of `AUTHORITATIVE_SOURCES` when an institutional source
   *  approved it. Read it through `planSourceLabel` — never compare it to
   *  "not EMPLOYEE_RECORDED" to mean "approved". Optional because the label is
   *  fail-closed: an absent value reads as a planned shift, never an approval. */
  plan_source?: string;
  approved_revision: {
    id: string;
    revision_no: number;
    planned_start_at: string;
    planned_end_at: string;
    communicated_at: string | null;
  };
  actual_start_at: string | null;
  actual_end_at: string | null;
  deviation: ShiftDeviation | null;
  findings: ShiftFinding[];
  // SHIFT-003: "RECORDED" once a paid shift break fact is linked to this
  // assignment, so the requirement is met rather than outstanding.
  paid_break_status: "EXPLICIT_FACT_REQUIRED" | "RECORDED";
  /** The paid-break facts that status is derived from — a long shift can hold
   *  more than one. Resolved by the assignment link, so a break after midnight
   *  is listed here even though it is not on this day's day-panel list. */
  paid_breaks: BreakInterval[];
}

export interface ShiftReportPreview {
  report_type: "SHIFT_REPORT";
  policy_version: string;
  policy_status: "PROVISIONAL" | "BLOCKED";
  employee_identifier: string;
  shift_assignment_id: string;
  planned_start_at: string;
  planned_end_at: string;
  actual_start_at: string | null;
  actual_end_at: string | null;
  deviation_type: string | null;
  /** The employee's own note on the deviation, and the decision context the
   *  recipient is entitled to (the server sends both in this payload). */
  reason: string | null;
  noticed_or_decided_at: string | null;
  decision_maker_type: string | null;
  findings: ShiftFinding[];
  /** Plan provenance, carried into the disclosed package (D2). */
  plan_source: string;
  /** Report-level entitlement state (D19, §8.8). A cancelled or aborted shift's
   *  factual report is submittable, and the recipient is told a human must
   *  decide the compensation — never an amount, credit or compensation day. */
  compensation_review_status: "NOT_APPLICABLE" | "MANUAL_REVIEW_REQUIRED";
  /** SHIFT-003 requirement state, mirroring `Shift.paid_break_status`: the
   *  recipient package can already say RECORDED, so the employee's own
   *  preview must be able to show it. */
  paid_break_status: "EXPLICIT_FACT_REQUIRED" | "RECORDED";
  preview_fingerprint: string;
}

export interface WorkingTimeSchedule {
  valid_from: string;
  valid_to: string | null;
  weekly_required_minutes: number;
  mon_required_minutes: number;
  tue_required_minutes: number;
  wed_required_minutes: number;
  thu_required_minutes: number;
  fri_required_minutes: number;
  sat_required_minutes: number;
  sun_required_minutes: number;
  source: "MASTER" | "ADMIN_SEED" | "EMPLOYEE";
  version: number;
}

export interface Profile {
  effective_at: string;
  employee: { external_employee_id: string | null; legal_name: string; email: string | null };
  contract: {
    fte: number | null;
    weekly_required_minutes: number;
    pay_group_bucket: string;
  } | null;
  schedule: WorkingTimeSchedule | null;
  preferences: {
    usual_work_type: WorkType | null;
    preferred_calendar_view: string | null;
    normal_day_template: TemplateInterval[] | null;
  };
}

interface ApiErrorBody {
  detail?: {
    code?: string;
    message?: string;
    /** A 422's per-field detail (`app/main.py`). `loc` is the server's own path
     *  to the field and `msg` is Pydantic's English sentence — NEITHER is ever
     *  rendered. See `fieldIssuesFrom`. */
    details?: Array<{ loc?: unknown[]; msg?: string }>;
  };
}

/** One rejected field, already in the product's own words. */
export interface FieldIssue {
  /** The API's field name (e.g. `start_local`), for lookup by the form. */
  field: string;
  /** The form's word for that field (e.g. "Start"). */
  label: string;
  /** A product sentence telling the employee what to do. */
  message: string;
}

/** The API field names in the form's own words, with an actionable sentence.
 *
 *  This is the ONLY place a raw field name is translated, and it is why no `loc`
 *  array and no Pydantic `msg` can reach the DOM (the backend deliberately
 *  strips `input`/`ctx` from its 422 so values are never reflected back —
 *  DP-LOG-001/DP-DATA-002 — and the frontend must not undo that by rendering
 *  the English remainder).
 *
 *  An unrecognised field gets the generic sentence: naming a field the form does
 *  not have would be worse than saying "check the form". */
const FIELD_COPY: Record<string, { label: string; message: string }> = {
  start_local: { label: "Start", message: "Enter the start as DD.MM.YYYY HH:MM." },
  end_local: { label: "End", message: "Enter the end as DD.MM.YYYY HH:MM." },
  start_at: { label: "Start", message: "Enter the start as DD.MM.YYYY HH:MM." },
  end_at: { label: "End", message: "Enter the end as DD.MM.YYYY HH:MM." },
  actual_start_local: {
    label: "Actual start",
    message: "Enter the actual start as DD.MM.YYYY HH:MM.",
  },
  actual_end_local: { label: "Actual end", message: "Enter the actual end as DD.MM.YYYY HH:MM." },
  planned_start_local: {
    label: "Planned start",
    message: "Enter the planned start as DD.MM.YYYY HH:MM.",
  },
  planned_end_local: {
    label: "Planned end",
    message: "Enter the planned end as DD.MM.YYYY HH:MM.",
  },
  shift_kind: { label: "Shift kind", message: "Choose a shift kind." },
  noticed_or_decided_local: {
    label: "Decision time",
    message: "Use the format DD.MM.YYYY HH:MM — for example 05.12.2026 18:00.",
  },
  decision_maker_type: {
    label: "Decided or reported by",
    message: "Say who decided or reported it.",
  },
  deviation_type: { label: "Deviation", message: "Choose the kind of difference." },
  reason: { label: "Reason given", message: "Add a short reason." },
  day: { label: "Day", message: "Choose a day inside the month." },
  category: { label: "Category", message: "Choose a category." },
  portion: { label: "How much of the day", message: "Choose how much of the day this covers." },
  day_part: { label: "Half of the day", message: "Choose which half of the day." },
  call_start_local: { label: "Call start", message: "Enter the call start as DD.MM.YYYY HH:MM." },
  call_end_local: { label: "Call end", message: "Enter the call end as DD.MM.YYYY HH:MM." },
  travel_out_minutes: { label: "Travel there (min)", message: "Enter the travel time in minutes." },
  travel_back_minutes: { label: "Travel back (min)", message: "Enter the travel time in minutes." },
  valid_from: { label: "Effective from", message: "Choose a valid date." },
  valid_to: { label: "Effective to", message: "Choose a valid date." },
  start_month: { label: "Start month", message: "Choose a valid month." },
  weekly_minutes: { label: "Weekly working time", message: "Enter the weekly working time." },
  effective_from: { label: "Effective from", message: "Choose a valid date." },
  month: { label: "Month", message: "Use the format MM.YYYY — for example 10.2026." },
  message: { label: "Message", message: "Add a short message." },
  recipients: { label: "Recipients", message: "Select at least one recipient." },
};

const GENERIC_FIELD_ISSUE = "Some details could not be accepted — check the form.";

/** Turn a 422's `details` into product words. The LAST element of `loc` is the
 *  field name; the envelope's own `body`/`query`/`path` markers are skipped. */
function fieldIssuesFrom(details: ApiErrorBody["detail"]): FieldIssue[] {
  // The envelope is external input: a body whose `details` is not a list (an
  // empty object, a string) must not turn the error path itself into a crash —
  // that would replace a useful refusal with "object is not iterable".
  const raw = details?.details;
  if (!Array.isArray(raw)) return [];
  const issues: FieldIssue[] = [];
  for (const detail of raw) {
    const parts = (detail.loc ?? [])
      .map(String)
      .filter((part) => !["body", "query", "path"].includes(part));
    const field = parts.at(-1) ?? "";
    const copy = FIELD_COPY[field];
    issues.push(
      copy
        ? { field, label: copy.label, message: copy.message }
        : { field, label: "", message: GENERIC_FIELD_ISSUE },
    );
  }
  return issues;
}

/** The product sentence for one field of a rejected request, or null when the
 *  server did not name it. Lets a form put the message beside its own control. */
export function fieldIssueFor(error: unknown, field: string): string | null {
  if (!(error instanceof ApiError)) return null;
  return error.fieldIssues.find((issue) => issue.field === field)?.message ?? null;
}

export interface AuthSessionStatus {
  authenticated: boolean;
  csrf_token: string | null;
  oidc_mode: boolean;
}

let csrfToken: string | null = null;
let sessionExpiredHandler: (() => void) | undefined;

export function setSessionExpiredHandler(handler: (() => void) | undefined): void {
  sessionExpiredHandler = handler;
}

/** API failure carrying the machine `code` (e.g. AUTH_FORBIDDEN) so pages can
 *  render boundary states ("no access") differently from operational errors
 *  `message` stays the human-readable server text. */
export class ApiError extends Error {
  code?: string;
  status: number;
  /** Per-field rejections in product words, empty for anything but a 422. */
  fieldIssues: FieldIssue[];
  constructor(message: string, status: number, code?: string, fieldIssues: FieldIssue[] = []) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.fieldIssues = fieldIssues;
  }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? "GET").toUpperCase();
  const headers = new Headers({ "Content-Type": "application/json", ...init?.headers });
  if (["POST", "PUT", "PATCH", "DELETE"].includes(method) && csrfToken) {
    headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(path, {
    ...init,
    headers,
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ApiErrorBody;
    const error = new ApiError(
      body.detail?.message ?? `Request failed: ${response.status}`,
      response.status,
      body.detail?.code,
      fieldIssuesFrom(body.detail),
    );
    if (response.status === 401) sessionExpiredHandler?.();
    throw error;
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** True when the failure is an authorization boundary denial (403-family)
 *  rather than an operational error — the UI shows an access state (F11). */
export function isForbiddenError(error: unknown): boolean {
  return error instanceof ApiError && (error.code === "AUTH_FORBIDDEN" || error.status === 403);
}

/** The machine `code` of the first FAILED query in a page's query group.
 *
 *  A page fires several queries at once and any one of them failing shows its
 *  error state, but the code that explains WHY is on whichever request failed.
 *  Returning the first one lets a page classify the boundary (a permanent
 *  coverage gap vs an operational fault) without every query being inspected at
 *  the call site. Queries that succeeded or are still pending are skipped. */
export function firstErrorCode(queries: readonly { error: unknown; isError?: boolean }[]) {
  for (const query of queries) {
    if (query.error instanceof ApiError && query.error.code) return query.error.code;
  }
  return undefined;
}

/** Retry policy for TanStack Query: transient faults only.
 *
 *  TanStack's default is 3 retries with exponential backoff for EVERY query, so
 *  a deterministic 400/401/403/404/409/422 was retried three times — delaying
 *  the boundary state the user needs to see and tripling the load on a refusal.
 *  Retrying a refusal is also a privacy-adjacent smell: it makes an
 *  authorization decision look like a transient glitch.
 *
 *  Anything below 500 is a client-side refusal whose answer cannot change by
 *  asking again; only 5xx (and a thrown network error, which is not an
 *  ``ApiError``) is retried, and only a bounded number of times. */
export const MAX_TRANSIENT_RETRIES = 2;

export function shouldRetryQuery(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status < 500) return false;
  return failureCount < MAX_TRANSIENT_RETRIES;
}

/** Public backend health probe. No personal data is involved. */
export function fetchHealth(): Promise<HealthResponse> {
  return request<HealthResponse>("/health");
}

export function fetchIdentity(): Promise<Identity> {
  return request<Identity>("/api/v1/me");
}

export async function fetchAuthSession(): Promise<AuthSessionStatus> {
  const status = await request<AuthSessionStatus>("/api/v1/auth/session");
  csrfToken = status.csrf_token;
  return status;
}

export async function logout(): Promise<void> {
  await request<void>("/api/v1/auth/logout", { method: "POST" });
  csrfToken = null;
}

export function fetchMonth(month: string): Promise<MonthSummary> {
  return request<MonthSummary>(`/api/v1/me/months/${month}`);
}

export function fetchDay(
  month: string,
  day: string,
): Promise<{ date: string; intervals: Interval[]; breaks: BreakInterval[] }> {
  return request(`/api/v1/me/months/${month}/days/${day}`);
}

export function fetchFindings(month: string): Promise<FindingsResponse> {
  return request(`/api/v1/me/findings?period=${month}`);
}

/** The effective-dated profile **at one DATE** (`YYYY-MM-DD`). The date is the
 *  maintenance key: `?at=<an arrangement's valid_from>` resolves that
 *  arrangement with its own values and its own `valid_to`, so the arrangements
 *  of one employee are enumerable without a history endpoint. Omit it for
 *  today. */
export function fetchProfile(at?: string): Promise<Profile> {
  return request(`/api/v1/me/profile${at ? `?at=${at}` : ""}`);
}

export function savePreferences(
  preferences: Partial<Profile["preferences"]>,
): Promise<Profile["preferences"]> {
  return request("/api/v1/me/preferences", { method: "PATCH", body: JSON.stringify(preferences) });
}

export function saveWorkingTime(payload: {
  valid_from: string;
  weekly_required_minutes: number;
  mon_required_minutes: number;
  tue_required_minutes: number;
  wed_required_minutes: number;
  thu_required_minutes: number;
  fri_required_minutes: number;
  sat_required_minutes: number;
  sun_required_minutes: number;
}): Promise<WorkingTimeSchedule> {
  return request("/api/v1/me/working-time", { method: "PUT", body: JSON.stringify(payload) });
}

/** The one-time starting state: when Chronobeam starts for the employee, their
 *  weekly working time, and the balance carried from the previous system.
 *
 *  `state` describes the month whose readiness was requested (`month`), which is
 *  a DIFFERENT thing from `opening.cutover_month` — the employee's actual start
 *  month, fixed once established. */
export interface OnboardingState {
  state: "READY" | "BLOCKED" | "COMPLETED";
  month: string;
  default_start_month: string | null;
  opening: {
    cutover_month: string;
    minutes: number;
    seed_minutes: number;
    occurred_on: string;
    previous_period: string;
    confirmed_by: string;
    revision: number;
    corrected_at: string | null;
  } | null;
  blocked_reason: "POLICY_EDITION_MISSING" | "CALENDAR_MISSING" | "ROLLOUT_BOUNDARY" | null;
  blocked_month: string | null;
  has_facts_in_month: boolean;
  correctable: boolean;
  in_use_reason: "IN_USE_BY_FACTS" | "IN_USE_BY_REVIEW" | "IN_USE_BY_DISCLOSURE" | null;
  schedule: WorkingTimeSchedule | null;
}

export interface OnboardingPayload {
  start_month: string;
  weekly_required_minutes: number;
  mon_required_minutes: number;
  tue_required_minutes: number;
  wed_required_minutes: number;
  thu_required_minutes: number;
  fri_required_minutes: number;
  sat_required_minutes: number;
  sun_required_minutes: number;
  opening_balance_minutes: number;
}

export function fetchOnboarding(startMonth?: string): Promise<OnboardingState> {
  return request(`/api/v1/me/onboarding${startMonth ? `?start_month=${startMonth}` : ""}`);
}

export function establishOnboarding(payload: OnboardingPayload): Promise<OnboardingState> {
  return request("/api/v1/me/onboarding", { method: "POST", body: JSON.stringify(payload) });
}

export function correctOpeningBalance(payload: {
  expected_revision: number;
  opening_balance_minutes: number;
  reason: string;
}): Promise<OnboardingState> {
  return request("/api/v1/me/onboarding", { method: "PATCH", body: JSON.stringify(payload) });
}

export function createInterval(payload: {
  start_local: string;
  end_local: string;
  work_type: WorkType;
  comment?: string | null;
  client_request_id?: string;
  fold?: 0 | 1;
}): Promise<Interval> {
  // Stash the key on the payload object itself: react-query retries reuse the
  // same object, so a retry replays instead of double-creating (review L2).
  payload.client_request_id ??= crypto.randomUUID();
  return request("/api/v1/me/work-intervals", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateInterval(
  id: string,
  payload: {
    version: number;
    start_local?: string;
    end_local?: string;
    work_type?: WorkType;
    comment?: string | null;
    fold?: 0 | 1;
    start_fold?: 0 | 1;
    end_fold?: 0 | 1;
    /** Attach/clear an overtime authorization reference (OVERTIME_* only). */
    overtime_authorization_id?: string | null;
  },
): Promise<Interval> {
  return request(`/api/v1/me/work-intervals/${id}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function deleteInterval(id: string, version: number): Promise<void> {
  await request<void>(`/api/v1/me/work-intervals/${id}?version=${version}`, { method: "DELETE" });
}

export function createBreak(payload: {
  start_local: string;
  end_local: string;
  kind: BreakKind;
  linked_shift_assignment_id?: string | null;
  client_request_id?: string;
  fold?: 0 | 1;
}): Promise<BreakInterval> {
  // Same retry-replay rationale as createInterval: a react-query retry reuses
  // the payload object, so it replays instead of double-creating.
  payload.client_request_id ??= crypto.randomUUID();
  return request("/api/v1/me/break-intervals", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function patchBreak(
  id: string,
  payload: {
    version: number;
    start_local?: string;
    end_local?: string;
    kind?: BreakKind;
    fold?: 0 | 1;
    reason?: string;
  },
): Promise<BreakInterval> {
  return request(`/api/v1/me/break-intervals/${id}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function deleteBreak(id: string, version: number): Promise<void> {
  await request<void>(`/api/v1/me/break-intervals/${id}?version=${version}`, { method: "DELETE" });
}

// ---------------------------------------------------------------------------
// Day status: employee-declared vacation / sickness / compensation days.
// The server derives the day's requirement and the WT-STATUS-* findings from
// these facts; the frontend never reproduces that arithmetic (ARCH-MOD-004).
// ---------------------------------------------------------------------------

export type DayStatusCategory = "VACATION" | "SICK" | "FLEX_COMPENSATION" | "SHIFT_COMPENSATION";
export type DayStatusPortion = "FULL" | "HALF";
export type DayStatusDayPart = "ALL" | "MORNING" | "AFTERNOON";

export interface DayStatus {
  id: string;
  day: string;
  category: DayStatusCategory;
  portion: DayStatusPortion;
  day_part: DayStatusDayPart;
  version: number;
  supersedes_id: string | null;
  created_at: string;
}

export interface DayStatusMonth {
  period: string;
  entries: DayStatus[];
  /** Year-scoped counts of recorded compensation facts. Read-only metrics:
   *  they are NOT calculation inputs and carry no finding. */
  compensation_metrics: CompensationMetrics;
}

export interface CompensationLimit {
  max_days_per_year: number;
  max_consecutive_days: number;
  source: string;
  enforced: boolean;
}

export interface CompensationMetrics {
  year: number;
  flex_compensation_full_days: number;
  flex_compensation_half_days: number;
  flex_compensation_zero_requirement_days: number;
  flex_compensation_longest_run_days: number;
  shift_compensation_days: number;
  limits: CompensationLimit;
  counting_note: string;
}

export interface DayStatusInput {
  day: string;
  category: DayStatusCategory;
  portion: DayStatusPortion;
  day_part: DayStatusDayPart;
  client_request_id?: string;
}

export function fetchDayStatus(month: string): Promise<DayStatusMonth> {
  return request(`/api/v1/me/day-status?period=${month}`);
}

export function createDayStatus(input: DayStatusInput): Promise<DayStatus> {
  // Same retry-replay rationale as createInterval: a react-query retry reuses
  // the payload object, so it replays instead of double-creating.
  input.client_request_id ??= crypto.randomUUID();
  return request("/api/v1/me/day-status", { method: "POST", body: JSON.stringify(input) });
}

export function patchDayStatus(
  id: string,
  changes: {
    version: number;
    category?: DayStatusCategory;
    portion?: DayStatusPortion;
    day_part?: DayStatusDayPart;
  },
): Promise<DayStatus> {
  // Presence-based on the server: omitted fields stay unchanged.
  return request(`/api/v1/me/day-status/${id}`, {
    method: "PATCH",
    body: JSON.stringify(changes),
  });
}

export async function deleteDayStatus(id: string, version: number): Promise<void> {
  await request<void>(`/api/v1/me/day-status/${id}?version=${version}`, { method: "DELETE" });
}

export function applyTemplate(
  month: string,
  dates: string[],
  commandId: string,
): Promise<Interval[]> {
  return request(`/api/v1/me/months/${month}/apply-template`, {
    method: "POST",
    body: JSON.stringify({ dates, client_request_id: commandId }),
  });
}

export function reviewMonth(month: string, acknowledged: boolean): Promise<MonthSummary> {
  return request(`/api/v1/me/months/${month}/review`, {
    method: "POST",
    body: JSON.stringify({ acknowledged }),
  });
}

export function splitInterval(
  id: string,
  version: number,
  split_local: string,
  fold?: 0 | 1,
): Promise<[Interval, Interval]> {
  return request(`/api/v1/me/work-intervals/${id}/split`, {
    method: "POST",
    body: JSON.stringify({ version, split_local, ...(fold !== undefined ? { fold } : {}) }),
  });
}

export function copyPreviousDay(
  month: string,
  day: string,
  commandId: string,
): Promise<Interval[]> {
  return request(`/api/v1/me/months/${month}/days/${day}/copy-previous`, {
    method: "POST",
    body: JSON.stringify({ client_request_id: commandId }),
  });
}

export function fetchShifts(from: string, to: string): Promise<Shift[]> {
  return request(`/api/v1/me/shifts?from=${from}&to=${to}`);
}

export function confirmShift(id: string, approvedRevisionId: string): Promise<Shift> {
  return request(`/api/v1/me/shifts/${id}/confirm`, {
    method: "POST",
    body: JSON.stringify({
      approved_revision_id: approvedRevisionId,
      client_request_id: crypto.randomUUID(),
    }),
  });
}

export function saveShiftDeviation(
  id: string,
  payload: {
    deviation_type: string;
    actual_start_local?: string;
    actual_end_local?: string;
    noticed_or_decided_local?: string;
    decision_maker_type?: string;
    reason?: string;
    version?: number;
  },
): Promise<Shift> {
  return request(`/api/v1/me/shifts/${id}/deviation`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function fetchShiftReportPreview(id: string): Promise<ShiftReportPreview> {
  return request(`/api/v1/me/shifts/${id}/report-preview`);
}

// ---------------------------------------------------------------------------
// Employee-entered PLAN facts: planned shifts and OCD availabilities.
//
// These are PLAN objects, never actual work (INV-09/INV-10). Two different
// concurrency contracts apply and every command uses exactly one (§8.6):
//
//   CREATE  — carries `client_request_id` (BE-CMD-001). It is stashed on the
//             payload object so a react-query retry replays the same command
//             instead of double-creating (the `createInterval` rationale).
//   EDIT / WITHDRAW / CANCEL — carries the EXPECTED current token instead:
//             `approved_revision_id` for shifts, `version` for OCD. A stale
//             token is a 409. These NEVER carry a `client_request_id`: a
//             correction is not a create, and accepting both would give two
//             answers to "is this the same command".
// ---------------------------------------------------------------------------

/** Create a planned shift the employee records for themselves.
 *
 *  The server forces `source = EMPLOYEE_RECORDED` — the employee write path
 *  cannot claim institutional authority (ACCESS-017), which is why the label for
 *  it is "Planned shift" and not "Approved plan". */
export function createShiftPlan(payload: {
  planned_start_local: string;
  planned_end_local: string;
  shift_kind?: string | null;
  timezone?: "Europe/Berlin";
  start_fold?: 0 | 1;
  end_fold?: 0 | 1;
  client_request_id?: string;
}): Promise<Shift> {
  payload.client_request_id ??= crypto.randomUUID();
  return request("/api/v1/me/shifts", { method: "POST", body: JSON.stringify(payload) });
}

/** Correct a planned shift that has not been withdrawn.
 *
 *  `approved_revision_id` is the existing shift token (`confirmShift` uses the
 *  same one), so the client re-reads and re-issues after a 409. */
export function editShiftPlan(
  id: string,
  payload: {
    approved_revision_id: string;
    planned_start_local?: string;
    planned_end_local?: string;
    shift_kind?: string | null;
    timezone?: "Europe/Berlin";
    start_fold?: 0 | 1;
    end_fold?: 0 | 1;
  },
): Promise<Shift> {
  return request(`/api/v1/me/shifts/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
}

/** Retract a plan that was entered by mistake or was never the real plan (D12).
 *
 *  Sets `status = WITHDRAWN`; it is NOT a cancellation and produces no deviation
 *  and no entitlement (§4.6). A real Company cancellation goes through
 *  `saveShiftCancellation` instead. */
export function withdrawShiftPlan(id: string, approvedRevisionId: string): Promise<Shift> {
  return request(`/api/v1/me/shifts/${id}/withdraw`, {
    method: "POST",
    body: JSON.stringify({ approved_revision_id: approvedRevisionId }),
  });
}

/** Record that the Company cancelled a real planned shift (D11, §6.3).
 *
 *  Deliberately NOT a withdrawal: the plan and its approved revision are left
 *  byte-identical and `status` stays `PLANNED`, because the scheduled shift is
 *  the basis for any allowance. The reported context lands on the existing
 *  `shift_deviation`, which is why this posts to the EXISTING deviation endpoint
 *  with `deviation_type: "CANCELLED"` — no new route, no new table.
 *
 *  `decisionMakerRole` is what the employee was told (the D22 dropdown), stored
 *  in the free-text `decision_maker_type` and deliberately unvalidated
 *  (SHIFT-013). `reason` is where "who informed you" belongs — the messenger is
 *  not modelled as its own field. */
export function saveShiftCancellation(
  id: string,
  payload: {
    /** When the employee was told. OPTIONAL, and it must stay optional: the
     *  backend schema defaults it to None (``shift/schemas.py:165``) and making
     *  it mandatory in the UI would be a process tightening under INV-15. An
     *  absent instant leaves SHIFT-009 ``BLOCKED_POLICY`` — the honest answer —
     *  rather than blocking the employee from recording what happened. */
    noticed_or_decided_local?: string | null;
    decisionMakerRole?: string | null;
    reason?: string | null;
    version?: number;
  },
): Promise<Shift> {
  return saveShiftDeviation(id, {
    deviation_type: "CANCELLED",
    noticed_or_decided_local: payload.noticed_or_decided_local ?? undefined,
    decision_maker_type: payload.decisionMakerRole ?? undefined,
    reason: payload.reason ?? undefined,
    version: payload.version,
  });
}

// ---------------------------------------------------------------------------
// Special work: OCD assignments/calls and overtime (all employee-private)
// ---------------------------------------------------------------------------

export type OcdCallMode = "REMOTE" | "ON_SITE";

export interface OcdCall {
  id: string;
  mode: OcdCallMode;
  call_start_at: string;
  call_end_at: string;
  travel_out_minutes: number | null;
  travel_back_minutes: number | null;
  comment: string | null;
  status: "RECORDED" | "VOIDED";
  version: number;
  /** Linked private work-interval fact (span = call ± travel, OCD-006). */
  linked_interval_id: string | null;
  span_start_at: string;
  span_end_at: string;
  elapsed_seconds: number;
}

export interface OcdAssignment {
  id: string;
  start_at: string;
  end_at: string;
  status: "SCHEDULED" | "CANCELLED" | "SUPERSEDED";
  /** The INSTITUTIONAL notice instant — when the Company scheduled it. It is the
   *  sole input to the OCD-002 notice finding and the OCD-007 short-notice
   *  classification, so it is `null` on every employee-authored row. */
  scheduled_at: string | null;
  /** The instant the EMPLOYEE reports they were told. A different fact from
   *  `scheduled_at`: preserved as reported and NEVER read by the classifier. */
  reported_notice_at: string | null;
  /** Plan provenance. `OcdOut` reports the same fact as `source`; read the
   *  label through `planSourceLabel(assignment.plan_source ?? assignment.source)`
   *  so the card is right whichever of the two the payload carries. */
  plan_source?: string;
  source: string;
  /** The optimistic-concurrency token. `PATCH` and `POST …/cancel` are
   *  version-guarded (409 `CONFLICT_VERSION` on a stale token), so the card reads
   *  the token from the row it is showing. Optional in the TYPE only so an older
   *  or partial payload cannot crash a render — a card that cannot read it must
   *  withhold Correct/Cancel rather than send a guessed token. */
  version?: number;
  calls: OcdCall[];
  findings: ShiftFinding[];
}

export interface ClassifiedComponent {
  rule_id: string;
  component: string;
  minutes: number;
  time_equivalent_minutes: number | null;
}

export interface DutyClassification {
  duty_minutes: number;
  duty_kind: "DUTY_GE_12H" | "DUTY_LT_12H";
  units: number | null;
  /** 12.5% basis is serialized as 1250 basis points. */
  basis_points: number | null;
  short_notice: boolean | null;
}

export interface ElectionSummary {
  id: string | null;
  mode: "PAY" | "TIME" | null;
  version: number | null;
}

export interface OcdCompensationPreview {
  report_type: "OCD_COMPENSATION";
  policy_version: string;
  policy_status: string;
  employee_identifier: string;
  ocd_assignment_id: string;
  assignment_start_at: string;
  assignment_end_at: string;
  calls: Array<{
    call_id: string;
    mode: OcdCallMode;
    call_start_at: string;
    call_end_at: string;
    travel_out_minutes: number | null;
    travel_back_minutes: number | null;
    span_start_at: string;
    span_end_at: string;
    elapsed_seconds: number;
  }>;
  classified_components: ClassifiedComponent[];
  duty: DutyClassification;
  election: ElectionSummary;
  findings: ShiftFinding[];
  /** Plan provenance, carried into the disclosed package (D2). */
  plan_source: string;
  /** The employee's own report of when they were told, disclosed AS REPORTED —
   *  it is not an institutional notice instant and drives no classification. */
  reported_notice_at: string | null;
  preview_fingerprint: string;
}

export type OvertimeAuthorizationStatus = "REQUESTED" | "APPROVED" | "REJECTED" | "CANCELLED";

/** The only statuses the EMPLOYEE write path may produce (ACCESS-017). */
export type EmployeeOvertimeAuthorizationStatus = "REQUESTED" | "CANCELLED";

export interface OvertimeAuthorization {
  id: string;
  valid_from: string;
  valid_to: string | null;
  purpose: string | null;
  /** Read-only on the employee view: an approval/rejection is an institutional
   *  decision, never something the employee records (ACCESS-017). */
  status: OvertimeAuthorizationStatus;
  /** Read-only: the institutional actor that decided the row, when there is one. */
  authorized_by: string | null;
  source_ref: string | null;
  /** Read-only provenance: `EMPLOYEE_RECORDED` or `INSTITUTIONAL_APPROVAL`. */
  source: string;
  version: number;
}

export interface CompensationElection {
  id: string;
  context_type: "OVERTIME" | "SHIFT" | "OCD";
  context_id: string;
  mode: "PAY" | "TIME";
  version: number;
  selected_at: string;
}

export interface OvertimePreview {
  report_type: "OVERTIME";
  policy_version: string;
  policy_status: string;
  employee_identifier: string;
  period_start: string;
  period_end: string;
  authorizations: Array<{
    id: string;
    valid_from: string;
    valid_to: string | null;
    purpose: string | null;
    status: string;
    source_ref: string | null;
  }>;
  overtime_intervals: Array<{
    interval_id: string;
    start_at: string;
    end_at: string;
    work_type: WorkType;
    elapsed_seconds: number;
    linked_authorization_id: string | null;
    version: number;
  }>;
  classified_components: ClassifiedComponent[];
  election: ElectionSummary;
  findings: ShiftFinding[];
  preview_fingerprint: string;
}

export function monthRange(month: string): { from: string; to: string } {
  const [year, monthNumber] = month.split("-").map(Number);
  const next =
    monthNumber === 12 ? `${year + 1}-01` : `${year}-${String(monthNumber + 1).padStart(2, "0")}`;
  return { from: `${month}-01`, to: `${next}-01` };
}

export function fetchOcd(from: string, to: string): Promise<OcdAssignment[]> {
  return request(`/api/v1/me/ocd?from=${from}&to=${to}`);
}

export function createOcdCall(payload: {
  ocd_assignment_id: string;
  call_start_local: string;
  call_end_local: string;
  mode: OcdCallMode;
  travel_out_minutes?: number | null;
  travel_back_minutes?: number | null;
  comment?: string | null;
  client_request_id?: string;
}): Promise<OcdCall> {
  payload.client_request_id ??= crypto.randomUUID();
  return request("/api/v1/me/ocd-calls", { method: "POST", body: JSON.stringify(payload) });
}

export function correctOcdCall(
  id: string,
  payload: {
    version: number;
    call_start_local?: string;
    call_end_local?: string;
    mode?: OcdCallMode;
    travel_out_minutes?: number | null;
    travel_back_minutes?: number | null;
    comment?: string | null;
  },
): Promise<OcdCall> {
  return request(`/api/v1/me/ocd-calls/${id}`, { method: "PATCH", body: JSON.stringify(payload) });
}

export function voidOcdCall(id: string, version: number, reason?: string): Promise<void> {
  return request(`/api/v1/me/ocd-calls/${id}/void`, {
    method: "POST",
    body: JSON.stringify({ version, ...(reason ? { reason } : {}) }),
  });
}

export function fetchOcdCompensationPreview(assignmentId: string): Promise<OcdCompensationPreview> {
  return request(`/api/v1/me/ocd-assignments/${assignmentId}/compensation-preview`);
}

/** Create an OCD AVAILABILITY window the employee plans to hold (INV-10).
 *
 *  `reported_notice_local` is the employee's OWN report of when they were told
 *  ("When were you told?"). It is preserved and disclosed as a reported fact but
 *  is never authoritative — the classifier reads only the institutional
 *  `scheduled_at`, which is why there is deliberately NO `scheduled_at` field
 *  here: asserting when the Company gave notice is not the employee's to do
 *  (§4.3). */
export function createOcdAssignment(payload: {
  start_local: string;
  end_local: string;
  reported_notice_local?: string | null;
  timezone?: "Europe/Berlin";
  start_fold?: 0 | 1;
  end_fold?: 0 | 1;
  client_request_id?: string;
}): Promise<OcdAssignment> {
  payload.client_request_id ??= crypto.randomUUID();
  return request("/api/v1/me/ocd-assignments", { method: "POST", body: JSON.stringify(payload) });
}

/** Correct a planned OCD availability.
 *
 *  `reported_notice_local` follows the repository's PRESENCE convention for a
 *  nullable field on a versioned model: omitting the key preserves the
 *  predecessor's instant on the new head, an explicit `null` CLEARS it. So pass
 *  the key only when you mean to change it — this function does not fill it in. */
export function correctOcdAssignment(
  id: string,
  payload: {
    version: number;
    start_local?: string;
    end_local?: string;
    reported_notice_local?: string | null;
    timezone?: "Europe/Berlin";
    start_fold?: 0 | 1;
    end_fold?: 0 | 1;
  },
): Promise<OcdAssignment> {
  return request(`/api/v1/me/ocd-assignments/${id}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

/** Cancel a planned OCD availability — the availability lifecycle, distinct from
 *  an actual call (`INV-10`). The expected token is `version`. */
export function cancelOcdAssignment(id: string, version: number): Promise<OcdAssignment> {
  return request(`/api/v1/me/ocd-assignments/${id}/cancel`, {
    method: "POST",
    body: JSON.stringify({ version }),
  });
}

export function fetchCompensationElection(
  contextType: "OVERTIME" | "SHIFT" | "OCD",
  contextId: string,
): Promise<CompensationElection | null> {
  return request(
    `/api/v1/me/compensation-elections?context_type=${contextType}&context_id=${contextId}`,
  );
}

export function saveCompensationElection(payload: {
  context_type: "OVERTIME" | "SHIFT" | "OCD";
  context_id: string;
  mode: "PAY" | "TIME";
}): Promise<CompensationElection> {
  return request("/api/v1/me/compensation-elections", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function fetchOvertimeAuthorizations(
  from: string,
  to: string,
): Promise<OvertimeAuthorization[]> {
  return request(`/api/v1/me/overtime-authorizations?from=${from}&to=${to}`);
}

/** Create an overtime REQUEST. `status`, `authorized_by` and `source` are not
 *  part of the payload: the server refuses them (ACCESS-017). */
export function createOvertimeAuthorization(payload: {
  valid_from: string;
  valid_to?: string | null;
  purpose?: string | null;
  source_ref?: string | null;
}): Promise<OvertimeAuthorization> {
  return request("/api/v1/me/overtime-authorizations", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** Correct an overtime request. Presence-based: only send what changed.
 *  The employee may move their OWN request between `REQUESTED` and
 *  `CANCELLED`; an institutional decision is refused by the server (422). */
export function correctOvertimeAuthorization(
  id: string,
  payload: {
    version: number;
    valid_from?: string;
    valid_to?: string | null;
    purpose?: string | null;
    status?: EmployeeOvertimeAuthorizationStatus;
    source_ref?: string | null;
  },
): Promise<OvertimeAuthorization> {
  return request(`/api/v1/me/overtime-authorizations/${id}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function fetchOvertimePreview(from: string, to: string): Promise<OvertimePreview> {
  return request(`/api/v1/me/overtime/preview?from=${from}&to=${to}`);
}

// ---------------------------------------------------------------------------
// Employee-controlled disclosure packages and access requests
// ---------------------------------------------------------------------------

export type SubmissionPurpose =
  | "SHIFT_REPORT"
  | "OCD_COMPENSATION"
  | "OVERTIME_PAID"
  | "OVERTIME_TIME_COMP";
export type SubmissionStatus = "SUBMITTED" | "SUPERSEDED" | "WITHDRAWN";

export interface SubmissionRecipient {
  recipient_principal: string;
  recipient_type: string;
  read_at: string | null;
}

export interface SubmissionPackage {
  id: string;
  purpose: SubmissionPurpose;
  period_start: string;
  period_end: string;
  version: number;
  status: SubmissionStatus;
  supersedes_id: string | null;
  created_at: string;
  employee_confirmed_at: string | null;
  recipients: SubmissionRecipient[];
  payload: Record<string, unknown>;
}

export interface RecipientSubmissionPackage {
  id: string;
  purpose: SubmissionPurpose;
  period_start: string;
  period_end: string;
  version: number;
  status: SubmissionStatus;
  employee_identifier: string;
  created_at: string;
  read_at: string | null;
  supersedes_id: string | null;
  payload?: Record<string, unknown>;
}

export function fetchSubmissions(): Promise<SubmissionPackage[]> {
  return request("/api/v1/me/submissions");
}

/** Server-controlled recipient registry: the subjects a package may be sent
 *  to, and whether each currently holds the reading capability. */
export interface RecipientOption {
  subject: string;
  readable: boolean;
}

export function fetchSubmissionRecipients(): Promise<RecipientOption[]> {
  return request("/api/v1/me/submission-recipients");
}

export function createSubmission(
  purpose: SubmissionPurpose,
  payload: {
    scope: { assignment_id: string } | { month: string };
    preview_fingerprint: string;
    client_request_id: string;
    recipients: string[];
  },
): Promise<SubmissionPackage> {
  return request(`/api/v1/me/submissions/${purpose}`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function withdrawSubmission(id: string): Promise<SubmissionPackage> {
  return request(`/api/v1/me/submissions/${id}/withdraw`, { method: "POST" });
}

export function fetchRecipientSubmissions(): Promise<RecipientSubmissionPackage[]> {
  return request("/api/v1/recipient/submissions");
}

export function fetchRecipientSubmission(id: string): Promise<RecipientSubmissionPackage> {
  return request(`/api/v1/recipient/submissions/${id}`);
}

export type AccessRequestPurpose = "RANDOM_CHECK" | "WORK_TIME_RECORD";
export type AccessRequestStatus = "PENDING" | "GRANTED" | "DECLINED" | "CLOSED" | "EXPIRED";

export interface AccessRequest {
  id: string;
  requester_principal: string;
  purpose: AccessRequestPurpose;
  period_start: string;
  period_end: string;
  view_type: string;
  message: string | null;
  status: AccessRequestStatus;
  created_at: string;
  closed_at: string | null;
  /** Metadata pulse for the requester: the grant currently serving this request, if any. */
  grant_id: string | null;
}

export interface DisclosureGrant {
  id: string;
  access_request_id: string | null;
  employee_identifier: string;
  grantee_principal: string;
  purpose: AccessRequestPurpose;
  period_start: string;
  period_end: string;
  view_type: string;
  granted_at: string;
  expires_at: string | null;
  supersedes_id: string | null;
  active: boolean;
}

export interface DisclosureView {
  grant_id: string;
  purpose: AccessRequestPurpose;
  period_start: string;
  period_end: string;
  view_type: string;
  granted_at: string;
  snapshot: Record<string, unknown>;
}

export function fetchMyAccessRequests(): Promise<AccessRequest[]> {
  return request("/api/v1/me/access-requests");
}

export function fetchHrAccessRequests(): Promise<AccessRequest[]> {
  return request("/api/v1/hr/access-requests");
}

export function createHrAccessRequest(payload: {
  employee_external_id: string;
  purpose: AccessRequestPurpose;
  month: string;
  view_type?: string;
  message?: string;
}): Promise<AccessRequest> {
  return request("/api/v1/hr/access-requests", { method: "POST", body: JSON.stringify(payload) });
}

export function grantAccessRequest(id: string): Promise<DisclosureGrant> {
  return request(`/api/v1/me/access-requests/${id}/grant`, { method: "POST" });
}

export function declineAccessRequest(id: string): Promise<AccessRequest> {
  return request(`/api/v1/me/access-requests/${id}/decline`, { method: "POST" });
}

export function fetchDisclosure(grantId: string): Promise<DisclosureView> {
  return request(`/api/v1/disclosures/${grantId}`);
}

/* ---------------------------------------------------------------------------
 * Travel facts (TRAVEL-001..004) — employee-declared, private.
 * ------------------------------------------------------------------------ */

export type TripStartOrigin = "HOME" | "WORKPLACE";
export type TravelClassification = "PASSIVE_TRAVEL" | "ACTIVE_WORK" | "DRIVING";
export type TravelBasis = "ACTUAL" | "FICTITIOUS_BUSINESS_ROUTE";

export interface TravelSegment {
  id: string;
  trip_id: string;
  start_at: string;
  end_at: string;
  start_local: string;
  end_local: string;
  classification: TravelClassification;
  basis: TravelBasis;
  work_interval_id: string | null;
  version: number;
}

export interface BusinessTrip {
  id: string;
  start_at: string;
  end_at: string;
  start_local: string;
  end_local: string;
  start_origin: TripStartOrigin;
  source: string;
  reference_label: string | null;
  version: number;
  segments: TravelSegment[];
}

export interface TravelTopup {
  day: string;
  minutes: number;
}

export interface TravelCredit {
  occurred_on: string;
  minutes: number;
  rule_id: string;
  source_entity_type: string;
  source_entity_id: string;
  window_start: string;
  window_end: string;
  eligible_minutes: number;
  threshold_minutes: number;
  excess_minutes: number;
}

export interface TravelTripSummary {
  trip_id: string;
  window_start: string;
  window_end: string;
  eligible_minutes: number;
  threshold_minutes: number;
  excess_minutes: number;
  credit_minutes: number;
  credit_effective_on: string;
}

export interface TravelSummary {
  period_month: string;
  topups: TravelTopup[];
  credits: TravelCredit[];
  trips: TravelTripSummary[];
  findings: Finding[];
  input_fingerprint: string;
}

export function fetchTravelTrips(): Promise<BusinessTrip[]> {
  return request<{ trips: BusinessTrip[] }>("/api/v1/me/travel-trips").then((data) => data.trips);
}

export function createTrip(payload: {
  start_local: string;
  end_local: string;
  start_origin: TripStartOrigin;
  reference_label?: string | null;
  fold?: 0 | 1;
  start_fold?: 0 | 1;
  end_fold?: 0 | 1;
  client_request_id?: string;
}): Promise<BusinessTrip> {
  payload.client_request_id ??= crypto.randomUUID();
  return request("/api/v1/me/travel-trips", { method: "POST", body: JSON.stringify(payload) });
}

export function updateTrip(
  id: string,
  payload: {
    version: number;
    reason: string;
    start_local?: string;
    end_local?: string;
    start_origin?: TripStartOrigin;
    reference_label?: string | null;
    fold?: 0 | 1;
    start_fold?: 0 | 1;
    end_fold?: 0 | 1;
  },
): Promise<BusinessTrip> {
  return request(`/api/v1/me/travel-trips/${id}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function deleteTrip(id: string, version: number): Promise<void> {
  await request<void>(`/api/v1/me/travel-trips/${id}`, {
    method: "DELETE",
    body: JSON.stringify({ version }),
  });
}

export function createSegment(
  tripId: string,
  payload: {
    start_local: string;
    end_local: string;
    classification: TravelClassification;
    basis?: TravelBasis;
    fold?: 0 | 1;
    start_fold?: 0 | 1;
    end_fold?: 0 | 1;
    client_request_id?: string;
  },
): Promise<TravelSegment> {
  payload.client_request_id ??= crypto.randomUUID();
  return request(`/api/v1/me/travel-trips/${tripId}/segments`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateSegment(
  id: string,
  payload: {
    version: number;
    reason: string;
    start_local?: string;
    end_local?: string;
    classification?: TravelClassification;
    basis?: TravelBasis;
    fold?: 0 | 1;
    start_fold?: 0 | 1;
    end_fold?: 0 | 1;
  },
): Promise<TravelSegment> {
  return request(`/api/v1/me/travel-segments/${id}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function deleteSegment(id: string, version: number): Promise<void> {
  await request<void>(`/api/v1/me/travel-segments/${id}`, {
    method: "DELETE",
    body: JSON.stringify({ version }),
  });
}

export function fetchTravelSummary(period: string): Promise<TravelSummary> {
  return request(`/api/v1/me/travel-summary?period=${period}`);
}

// ---------------------------------------------------------------------------
// Administration: users & access, system status
// ---------------------------------------------------------------------------

export type AdminRoleName = "APPLICATION_ADMIN" | "HR_REQUESTER" | "SUBMISSION_RECIPIENT";

export interface AdminRoleAssignment {
  id: string;
  role: AdminRoleName;
  granted_by_principal: string;
  granted_at: string;
  revoked_at: string | null;
}

export interface AdminUserSummary {
  id: string;
  external_employee_id: string | null;
  legal_name: string;
  email: string | null;
  active: boolean;
  /** ACTIVE role names only; the grant/revoke history is on the detail view. */
  roles: AdminRoleName[];
  created_at: string;
  /** Readiness: presence facts about PROVISIONING, never work records. The binding
   *  is a boolean rather than the subject itself — the list needs to say "not
   *  mapped", not to republish the credential identifier. */
  has_idp_subject: boolean;
  has_effective_contract: boolean;
  has_effective_schedule: boolean;
}

export interface AdminUserDetail extends AdminUserSummary {
  idp_subject: string | null;
  role_assignments: AdminRoleAssignment[];
  effective_capabilities: Capability[];
}

/** One published calendar edition: institutional data, no employee data.
 *  `valid_to` is INCLUSIVE; `null` means open-ended. */
export interface AdminCalendarEdition {
  version: string;
  valid_from: string;
  valid_to: string | null;
  source: string;
}

export interface AdminSystem {
  application: { name: string; version: string; build_sha: string };
  database: {
    reachable: boolean;
    schema_revision: string | null;
    expected_revision: string | null;
    schema_current: boolean;
  };
  auth: { mode: string; oidc_configured: boolean };
  integrations: Record<string, string>;
  /** `first_uncovered_month` is `"YYYY-MM"`: the first month at or after the
   *  current one whose month view answers 409 CALENDAR_BLOCKED. `null` = no gap. */
  calendar: {
    editions: AdminCalendarEdition[];
    first_uncovered_month: string | null;
  };
}

export const ADMIN_ROLE_LABELS: Record<AdminRoleName, string> = {
  APPLICATION_ADMIN: "Application administrator",
  HR_REQUESTER: "HR requester",
  SUBMISSION_RECIPIENT: "Submission recipient",
};

export const ALL_ADMIN_ROLES: AdminRoleName[] = [
  "APPLICATION_ADMIN",
  "HR_REQUESTER",
  "SUBMISSION_RECIPIENT",
];

export type AdminUserSortKey = "name" | "employee_id" | "email" | "status" | "role" | "provisioned";

export type SortDirection = "asc" | "desc";

export function fetchAdminUsers(params?: {
  search?: string;
  active?: boolean;
  sort?: AdminUserSortKey;
  direction?: SortDirection;
  /** 0 means every matching user — the "All" page size. */
  limit?: number;
}): Promise<AdminUserSummary[]> {
  const query = new URLSearchParams();
  if (params?.search) query.set("search", params.search);
  if (params?.active !== undefined) query.set("active", String(params.active));
  if (params?.sort) query.set("sort", params.sort);
  if (params?.direction) query.set("direction", params.direction);
  // `!== undefined`, not truthiness: 0 is the meaningful "all" value.
  if (params?.limit !== undefined) query.set("limit", String(params.limit));
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  return request(`/api/v1/admin/users${suffix}`);
}

export function fetchAdminUser(id: string): Promise<AdminUserDetail> {
  return request(`/api/v1/admin/users/${id}`);
}

export function provisionAdminUser(payload: {
  legal_name: string;
  external_employee_id?: string | null;
  idp_subject?: string | null;
  email?: string | null;
}): Promise<AdminUserDetail> {
  return request("/api/v1/admin/users", {
    method: "POST",
    body: JSON.stringify({
      legal_name: payload.legal_name,
      external_employee_id: payload.external_employee_id ?? null,
      idp_subject: payload.idp_subject ?? null,
      email: payload.email ?? null,
    }),
  });
}

export function updateAdminUser(
  id: string,
  payload: {
    legal_name?: string;
    external_employee_id?: string | null;
    email?: string | null;
    active?: boolean;
  },
): Promise<AdminUserDetail> {
  return request(`/api/v1/admin/users/${id}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function assignAdminRole(id: string, role: AdminRoleName): Promise<AdminRoleAssignment> {
  return request(`/api/v1/admin/users/${id}/roles/${role}`, { method: "PUT" });
}

export function revokeAdminRole(id: string, role: AdminRoleName): Promise<void> {
  return request(`/api/v1/admin/users/${id}/roles/${role}`, { method: "DELETE" });
}

export function fetchAdminSystem(): Promise<AdminSystem> {
  return request("/api/v1/admin/system");
}
