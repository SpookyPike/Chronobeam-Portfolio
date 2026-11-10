import type { QueryClient } from "@tanstack/react-query";

/**
 * The query-key families whose cached values are DERIVED FROM EMPLOYEE FACTS.
 *
 * Every one of these is recomputed server-side from the recorded facts, so a
 * write to any fact invalidates all of them. Prefix invalidation (the default
 * `exact: false`) is deliberate: one fact can change more than one month — an
 * interval may cross midnight, a month boundary or a year boundary (the
 * campaign's own `31 Dec 22:00 → 1 Jan 03:00` case) — and the month view derives
 * each month by chaining the earlier ones. A month-scoped key therefore cannot be
 * correct here: "the month I am looking at" is not the only affected key.
 *
 * This is already the repository's precedent — `profile.tsx` invalidates
 * `["month"]` with no month argument after a working-time change.
 */
export const FACT_DERIVED_QUERY_KEYS = [
  "month", // the month view: calendar cells, day totals, closing balance
  "findings", // the findings list and the selected day's pin
  "day", // the selected day's panel
  "day-status", // the day's declared statuses
  "shifts", // planned shifts and their deviations
  "ocd", // on-call assignments and their calls
  "travel-summary", // travel windows, credit and findings
  "travel-trips", // the trip list
  "home-month-summary", // Home's balance card — the one cache no writer saw
  "overtime", // overtime entries and their authorizations
  // The overtime compensation preview renders the ELECTION and the classified
  // components, both recomputed server-side, so it is a derived view like any
  // other. It was missing here, which is why choosing PAY/TIME left the visible
  // preview stating the previous choice (review R18).
  "overtime-preview",
  // The submissions scope pickers read the SAME server facts as `shifts` and
  // `ocd` under different keys (`fetchShifts`/`fetchOcd` for the month). They
  // were absent, so a recorded interval, call or shift left the picker offering
  // a stale list of what could be disclosed. The preview itself is server-
  // computed and the fingerprint guards the disclosure, so this was a stale
  // LIST rather than a wrong projection — the same class, one step milder.
  "submission-shifts",
  "submission-ocd",
  // The onboarding read is not only the stored cutover state: the server also
  // derives `has_facts_in_month`, `correctable` and `in_use_reason` from the
  // ledger on every read, and Profile uses them to offer or close carried-
  // balance correction. Recording an interval in the cutover month therefore
  // changed the answer while the cached response still offered a correction the
  // server now refuses (review R8).
  "onboarding",
] as const;

/**
 * Refresh every view derived from employee facts after a fact write.
 *
 * Call this from every mutation that writes a work interval, a break, a day
 * status, a shift actual, an OCD call, a travel fact or an overtime record.
 * Without it the calendar cell, the day panel and the month balance keep showing
 * a state the employee has just changed.
 *
 * ceiling: prefix invalidation refetches every MOUNTED fact-derived query. The
 * mounted set is one page's queries, so the cost is bounded by the current view.
 * If that ever matters, narrow it per writer — but only with a cross-boundary
 * span test in place first, because the breadth is exactly what makes the
 * midnight, month and year crossings correct.
 */
export function invalidateFactDerived(queryClient: QueryClient): void {
  for (const key of FACT_DERIVED_QUERY_KEYS) {
    void queryClient.invalidateQueries({ queryKey: [key] });
  }
}
