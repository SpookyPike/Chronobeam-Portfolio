/* One place that ranks a finding's severity.
 *
 * Acceptance finding C2: a day with several findings showed the LAST one's
 * severity in the day pin (its summary class was hardcoded to "warning") while
 * the calendar cell showed the FIRST one's. The same day could therefore read
 * CRITICAL in the calendar and calm in the panel — two answers to one question.
 *
 * The order is the domain's own: a blocked policy decision outranks a warning,
 * and a critical compliance finding outranks both. The server owns the rule and
 * decides each finding's severity; this only picks the loudest of the severities
 * that have already been decided. */

/** Rank, lowest to highest. An unknown severity ranks as a warning rather than
 *  outranking everything, so a future server value cannot silently raise the
 *  alarm level of an ordinary day. */
const RANK: Record<string, number> = {
  WARNING: 0,
  BLOCKED_POLICY: 1,
  CRITICAL: 2,
};

/** The loudest severity among `findings`, or null when there are none.
 *
 *  Generic over the element type because the calendar receives a structural
 *  subset of `Finding` (`{date, severity, message}`) while the day panel passes
 *  the full type; both must rank identically. */
export function worstSeverity<T extends { severity: string }>(
  findings: readonly T[],
): T["severity"] | null {
  let worst: T["severity"] | null = null;
  for (const finding of findings) {
    if (worst === null || (RANK[finding.severity] ?? 0) > (RANK[worst] ?? 0)) {
      worst = finding.severity;
    }
  }
  return worst;
}
