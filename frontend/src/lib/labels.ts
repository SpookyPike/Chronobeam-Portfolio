/* Human label maps for engine vocabulary.
 *
 * On an employee or recipient surface the human label is the ONLY copy: the
 * engine code behind it stays in the API payload and on /admin, never as a
 * secondary chip beside the label. (The earlier "codes as secondary detail
 * chips" convention is retired — owner directive, 2026-09-22: the frontend is
 * written for the people who use it, not for the team that built it.) */

export const workTypeLabels: Record<string, string> = {
  ON_SITE: "On-site",
  REMOTE: "Remote",
  SHIFT: "Shift",
  OCD_CALL: "On-call call",
  OVERTIME_PAID: "Overtime paid",
  OVERTIME_TIME_COMP: "Overtime time compensation",
  BUSINESS_TRIP_WORK: "Business trip work",
};

export function workTypeLabel(code: string): string {
  return workTypeLabels[code] ?? code;
}

/* Planned shift kinds. Shared by the shift card and the submission scope
 * picker so an assignment reads the same in both places. An unknown kind
 * falls back to "Shift" — an engine code is never primary copy. */
export const shiftKindLabels: Record<string, string> = {
  NIGHT: "Night shift",
  DAY: "Day shift",
  SHIFT: "Shift",
};

export function shiftKindLabel(code: string | undefined): string {
  return shiftKindLabels[code ?? ""] ?? "Shift";
}

export const componentLabels: Record<string, string> = {
  MEHRARBEIT: "Extra hours (part-time)",
  OVERTIME_BASE: "Base overtime",
  OVERTIME_SUPPLEMENT: "Overtime supplement",
  OCD_CALL_BASE: "Call work",
  // The allowance components (docs/RULE_CATALOGUE.md, ALLOW-003..006).
  // Unmapped, the code itself became the primary copy — "NIGHT_20: 0:36" —
  // which is the one thing this map exists to prevent.
  NIGHT_20: "Night allowance",
  SATURDAY_20: "Saturday allowance",
  SUNDAY_25: "Sunday allowance",
  HOLIDAY_35: "Public-holiday allowance",
};

export function componentLabel(code: string): string {
  return componentLabels[code] ?? code;
}

export const electionLabels: Record<string, string> = {
  PAY: "Paid out",
  TIME: "Time compensation",
};

export const dayStatusCategoryLabels: Record<string, string> = {
  VACATION: "Vacation",
  SICK: "Sick leave",
  FLEX_COMPENSATION: "Flex-time compensation",
  SHIFT_COMPENSATION: "Shift compensation",
};

export function dayStatusCategoryLabel(code: string): string {
  return dayStatusCategoryLabels[code] ?? code;
}

/* Compact forms for the month-calendar cells, where a column is only ~60px
 * wide: the full name above does not fit and would push the chip past the
 * cell's border. The full name stays the one used in the day panel and in the
 * day button's accessible name. */
export const dayStatusCategoryCellLabels: Record<string, string> = {
  VACATION: "Vacation",
  SICK: "Sick",
  FLEX_COMPENSATION: "Flex-time",
  SHIFT_COMPENSATION: "Shift comp",
};

export function dayStatusCategoryCellLabel(code: string): string {
  return dayStatusCategoryCellLabels[code] ?? code;
}

export const dayStatusPortionLabels: Record<string, string> = {
  FULL: "Full day",
  HALF: "Half day",
};

/* Explicit factual breaks (WT-BACKEND §5.3). Only an unpaid break reduces
 * worked time; a paid shift break is compensated presence (SHIFT-003). */
export const breakKindLabels: Record<string, string> = {
  UNPAID: "Unpaid break",
  PAID_SHIFT_BREAK: "Paid shift break (compensated)",
  OTHER: "Other break",
};

export function breakKindLabel(code: string): string {
  return breakKindLabels[code] ?? code;
}

/* Overtime authorization lifecycle states, in employee-facing words. Shared by
 * the overtime page and the disclosed package preview, so an employee and a
 * recipient never read a raw `APPROVED`/`CANCELLED` enum. */
export const authorizationStatusLabels: Record<string, string> = {
  REQUESTED: "Requested",
  APPROVED: "Approved",
  REJECTED: "Rejected",
  CANCELLED: "Cancelled",
};

export function authorizationStatusLabel(code: string): string {
  return authorizationStatusLabels[code] ?? code;
}

/* SHIFT-002 shift deviations. Rendered both in the shift card's own form and in
 * the disclosed package preview, so the employee and the recipient read the
 * same human phrase instead of a raw `OVERRUN_GT_10H`. */
export const deviationTypeLabels: Record<string, string> = {
  CANCELLED: "Cancelled",
  RESCHEDULED: "Moved to another time",
  ADDITIONAL: "Worked in addition to the plan",
  SWAPPED: "Swapped with another shift",
  SHORTENED: "Ended earlier than planned",
  OVERRUN: "Ran past the planned end",
  OVERRUN_GT_10H: "Ran more than ten hours past the planned end",
  MINOR_CHANGE: "Small change to the plan",
  CANCELLED_SICKNESS: "Cancelled because of sickness",
  // POL-02: the Company cutting a shift short after it started — a SEPARATE
  // type from CANCELLED, because CANCELLED deletes the linked actual intervals
  // and would erase the partial work the entitlement is computed from.
  ABORTED_BY_COMPANY: "Aborted by the Company",
};

export function deviationTypeLabel(code: string): string {
  return deviationTypeLabels[code] ?? code;
}

/* SHIFT-003 paid shift-break requirement. Shared by the shift card and the
 * disclosed package preview so both read the same. */
export const paidBreakLabels: Record<string, string> = {
  EXPLICIT_FACT_REQUIRED: "Not recorded yet",
  RECORDED: "Recorded",
};

export function paidBreakLabel(code: string): string {
  return paidBreakLabels[code] ?? code;
}

export const dayStatusDayPartLabels: Record<string, string> = {
  ALL: "All day",
  MORNING: "Morning",
  AFTERNOON: "Afternoon",
};

/* The day-status rules the server can raise (docs/RULE_CATALOGUE.md).
 * The message stays server-authored; this is only the human name of the rule
 * for the secondary detail chip. */
export const dayStatusRuleLabels: Record<string, string> = {
  "WT-STATUS-001": "Work recorded on a full vacation day",
  "WT-STATUS-002": "Work recorded on a sick day",
  "WT-STATUS-003": "Work recorded on a flex-time compensation day",
  "WT-STATUS-004": "More than a full day of status coverage",
  "WT-STATUS-005": "Day status on a day with no scheduled requirement",
};

export function dayStatusRuleLabel(ruleId: string): string {
  return dayStatusRuleLabels[ruleId] ?? ruleId;
}

/* The resolved calendar classification of a day (server-derived, F6/E8). An
 * institutional fact, not an employee status: the employee sees WHY the
 * requirement is zero instead of inferring it from the number.
 *
 * `WEEKDAY` is NOT "a day you work": the edition enumerates *exceptions*
 * (`docs/INVARIANTS.md` EINV-10), so the server emits `WEEKDAY` for every day
 * that is neither a public holiday nor an employer-free day — Saturdays and
 * Sundays included. Whether a day carries a requirement comes from the
 * employee's own weekly schedule, which the panel shows separately, so this
 * label must not assert that the day is a working one. */
export const calendarClassLabels: Record<string, string> = {
  WEEKDAY: "Ordinary day",
  PUBLIC_HOLIDAY: "Public holiday",
  EMPLOYER_FREE: "Employer-free day",
};

export function calendarClassLabel(code: string | undefined): string {
  return code ? (calendarClassLabels[code] ?? code) : "—";
}

/* Authorization findings on the overtime preview. One rule id carries three
 * server-authored messages (no reference / not an institutional approval /
 * outside the window), so the label names the RULE and never the mode — the
 * server's message stays the explanation. */
export const accessRuleLabels: Record<string, string> = {
  "ACCESS-006": "Overtime not covered by an institutional approval",
};

export function accessRuleLabel(ruleId: string): string {
  return accessRuleLabels[ruleId] ?? ruleId;
}

/* OCD duty short-notice COMPENSATION CLASSIFICATION.
 *
 * TWO DIFFERENT RULES share the words "short notice", and presenting both with
 * the same words made one screen contradict itself (acceptance G4/H1):
 *
 *   - the notice EXPECTATION — `docs/RULE_CATALOGUE.md` OCD-002: "OCD is
 *     normally agreed at least 4 weeks in advance". It is sourced, so its
 *     finding keeps the four-week wording.
 *   - the short-notice COMPENSATION CLASSIFICATION — the same document's
 *     OCD-007 (POL-03 sample policy: the allowance trigger is not quantified).
 *     The shipped rule set does NOT quantify the allowance trigger. What is live is a
 *     comparison of the communicated time against the duty start at 72 hours
 *     (`backend/app/ocd_overtime/policy.py`, pinned by
 *     `backend/tests/test_ocd_overtime_policy.py`).
 *
 * Ledger G4/H1 stays an OPEN POLICY QUESTION: which rule should govern "short
 * notice" is a product/policy decision. This function only names WHICH rule it
 * reports. Do not "fix" it by picking a number — neither threshold is changed
 * here, and a missing policy is never invented (docs/RULE_CATALOGUE.md).
 *
 * `null` means the server could not classify it at all (no communicated time to
 * compare against). That is a DIFFERENT fact from a classification of "no", so
 * all three states keep their own words. */
export function shortNoticeLabel(shortNotice: boolean | null | undefined): string {
  if (shortNotice === true)
    return "short-notice compensation: yes (communicated under 72 h before the duty)";
  if (shortNotice === false)
    return "short-notice compensation: no (communicated 72 h or more before the duty)";
  return "short-notice compensation: not classified (no communicated time to compare)";
}

/* The one line that keeps the two notice rules apart wherever both are on
 * screen. Rule ids stay in the code: an employee or recipient surface carries
 * the human words only (see this file's header). */
export const shortNoticeRuleNote =
  "Two different rules use the word notice here: the four-week figure is the notice expectation for agreeing an assignment, while the short-notice figure above is the compensation classification — a different rule, whose allowance trigger is still being confirmed.";

/* Submission package lifecycle states, in employee- and recipient-facing words.
 * Shared by /share's package list and the recipient queue. */
export const submissionStatusLabels: Record<string, string> = {
  SUBMITTED: "Submitted",
  WITHDRAWN: "Withdrawn",
};

/* -------------------------------------------------------------------------
 * Calendar event markers
 * ---------------------------------------------------------------------- */

/** The categories a month-grid event bar can carry. Deliberately the same set
 *  the day panel's "Planned assignments" section renders, so the grid and the
 *  panel cannot disagree about what a day holds. */
export type CalendarEventKind =
  | "SHIFT"
  | "OCD"
  | "VACATION"
  | "SICK"
  | "FLEX_COMPENSATION"
  | "SHIFT_COMPENSATION";

/** One source for each category's desktop label, phone glyph and spoken phrase.
 *
 *  The month grid's bars, the day button's accessible name and the day panel all
 *  read from here, so the three surfaces cannot drift.
 *
 *  The glyph is TWO letters taken from the human label — never an engine code
 *  (this file's header). `Si` / `Sc` / `Sh` share an initial, which is
 *  deliberate and is the one place the glyph alone is not self-explanatory: the
 *  glyph is always accompanied by the category's hue AND by the full spoken
 *  phrase in the day button's accessible name, so neither colour nor glyph is
 *  ever the sole carrier of meaning. Two letters rather than one because at the
 *  phone density a single letter cannot distinguish the three S-categories at
 *  all, and because `Sick` and `Shift` genuinely collide on their initial.
 *
 *  Labels reuse `dayStatusCategoryCellLabels` where the category is a day status,
 *  so a vacation reads "Vacation" in the grid exactly as it does in the cell chip
 *  it replaces. */
export const calendarEventLabels: Record<CalendarEventKind, { label: string; glyph: string }> = {
  SHIFT: { label: "Shift", glyph: "Sh" },
  // "On-call", not "OCD": the navigation, the on-call page and the work-type map
  // all say "on-call" to the employee, and `labels.ts` never shows an engine
  // acronym as primary copy.
  OCD: { label: "On-call", glyph: "On" },
  VACATION: { label: dayStatusCategoryCellLabels.VACATION, glyph: "Va" },
  SICK: { label: dayStatusCategoryCellLabels.SICK, glyph: "Si" },
  FLEX_COMPENSATION: { label: dayStatusCategoryCellLabels.FLEX_COMPENSATION, glyph: "Fx" },
  SHIFT_COMPENSATION: { label: dayStatusCategoryCellLabels.SHIFT_COMPENSATION, glyph: "Sc" },
};

/* The `H` / `X` markers that sit beside a date number on an institutionally
 * non-working day. They are the non-colour carrier for the two structural
 * classes and the ONLY thing that separates a statutory public holiday from an
 * employer-free day at phone density, where neither label fits. The full words
 * still reach assistive technology through the day button's accessible name and
 * through the day panel's own Calendar field. */
export const calendarStructureMarkers: Record<string, string> = {
  PUBLIC_HOLIDAY: "H",
  EMPLOYER_FREE: "X",
};

export function calendarStructureMarker(code: string | undefined): string {
  return code ? (calendarStructureMarkers[code] ?? "") : "";
}

/* The two coverage boundaries the API reports for a month.
 *
 * Both are PERMANENT states of the data, not transient faults: no amount of
 * retrying installs a working-time policy edition or publishes a holiday
 * calendar. Presenting them as a generic load failure told the employee to try
 * again for something that can never succeed. Returns null for every other
 * code, so the caller keeps its operational-error copy.
 *
 * `monthLabel` is the human month ("May 2026") — the callers already have it. */
export function coverageBlockedMessage(
  code: string | undefined,
  monthLabel: string,
): string | null {
  if (code === "POLICY_BLOCKED")
    return `No working-time rules are installed for ${monthLabel}. Earlier months stay in the Excel archive.`;
  if (code === "CALENDAR_BLOCKED")
    return `No public-holiday calendar edition covers ${monthLabel} yet.`;
  return null;
}

/* -------------------------------------------------------------------------
 * Plan provenance and cancellation context (plan §6.2, §6.3)
 * ---------------------------------------------------------------------- */

/* The plan sources the repository genuinely treats as AUTHORITATIVE.
 *
 * Mirrors `AUTHORITATIVE_SOURCES` in `backend/app/enums.py` exactly, and must
 * stay a mirror: adding a value here is the one act that upgrades a plan's
 * label to "Approved plan". Today the backend set holds `INSTITUTIONAL_APPROVAL`
 * alone — `EMPLOYEE_RECORDED` is deliberately NOT in it, because recording your
 * own plan is not self-authorisation (ACCESS-017). */
export const AUTHORITATIVE_SOURCES: ReadonlySet<string> = new Set(["INSTITUTIONAL_APPROVAL"]);

/* Who authored a plan, in employee- and recipient-facing words.
 *
 * FAIL-CLOSED three-way, and the middle branch is a POSITIVE test. "Not
 * `EMPLOYEE_RECORDED`" is *not* the test for "approved": `shift_plan.source` /
 * `shift_plan_revision.source` are free-text `Text` columns, so the fixture's
 * `TEST_SEED`, a future adapter's own code, or a typo would all fall into a
 * naive `else` and be presented to the employee as institutionally approved —
 * an approval claim nobody made. Only membership in `AUTHORITATIVE_SOURCES`
 * earns "Approved plan"; every other value, including an absent one, takes the
 * neutral branch. */
export function planSourceLabel(source: string | null | undefined, noun = "shift"): string {
  if (source === "EMPLOYEE_RECORDED") return `Planned ${noun}`;
  if (AUTHORITATIVE_SOURCES.has(source ?? "")) return "Approved plan";
  return `Planned ${noun}`;
}

/* `noun` names WHAT was planned, because one label serves two different
 * assignments and the wrong noun is a false statement about the employee's record:
 * before this parameter existed, an on-call availability on the on-call page and in
 * the disclosed OCD package both read "Planned shift". Callers pass
 * "on-call duty" for anything in the OCD domain; the default keeps every shift
 * caller, including the disclosed Shift Report, byte-identical. */

/* The recipient-facing compensation-review state on a Shift Report (D19).
 *
 * A cancelled or aborted shift's FACTUAL report is submittable; what the
 * recipient gets instead of an entitlement is this one report-level status, so
 * a human decides. "NOT_APPLICABLE" is the ordinary case.
 *
 * An unrecognised status does NOT fall back to the engine code (this file's
 * header) and does NOT claim that no review is needed — the neutral sentence
 * says only that the state was not reported. */
export const compensationReviewLabels: Record<string, string> = {
  NOT_APPLICABLE: "No compensation review needed",
  MANUAL_REVIEW_REQUIRED: "Compensation needs manual review",
};

export function compensationReviewLabel(status: string): string {
  return compensationReviewLabels[status] ?? "Compensation review not classified";
}

/* The escape hatch in the cancellation dropdown: choosing it reveals a
 * free-text input, and the TEXT is what is sent — this sentinel is never stored
 * (it is a UI-only value, so it must never be written to `decision_maker_type`). */
export const decisionMakerRoleOther = "Other / not stated";

/* The cancellation decision roles, in D22 order, exactly as POL-02 names them.
 * The form renders this list and nothing else.
 *
 * "Local Contact" and "Works Council" are deliberately ABSENT: neither is a
 * cancellation decision-maker in POL-02, and offering them here would imply an
 * institutional authority the sample policy does not grant. */
export const decisionMakerRoles: readonly string[] = [
  "Director",
  "Department Head",
  "Supervisor / Group Leader",
  "Person nominated by the Management Board",
  decisionMakerRoleOther,
];

/* The FIELD LABEL for that dropdown. Never "Cancelled by" (D22): the employee is
 * reporting what they were told, not asserting the Company's own decision, and
 * `decision_maker_type` is stored unvalidated (SHIFT-013) — so this wording is
 * the only thing keeping the record honest. */
export const decisionMakerRoleFieldLabel = "Who decided to cancel (as reported to you)";

/* A stored `decision_maker_type` back into words. The roles above are stored
 * VERBATIM (the field is free text, unvalidated by design — SHIFT-013), so a
 * known role reads as itself and an arbitrary string is shown unchanged rather
 * than rejected. Only an absent value gets words of its own: an empty field on a
 * recipient surface must not read as a blank. */
export function decisionMakerRoleLabel(role: string | null | undefined): string {
  return role && role.trim() !== "" ? role : "Not stated";
}
