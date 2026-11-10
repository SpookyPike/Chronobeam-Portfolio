/* One shared Berlin date-time vocabulary (product-wide directive:
 * European day-first dates everywhere — "05.12.2026" or "06 Dec 2026" are
 * both acceptable; US mm/dd/yyyy and AM/PM never appear.
 *
 * Displays use the readable day-first named form ("06 Dec 2026, 19:00",
 * 24-hour); inputs use compact German numeric (see lib/inputs). */

const BERLIN = "Europe/Berlin";

/** "2026-10-09" — TODAY in the canonical business timezone.
 *
 *  Deliberately not `new Date().toISOString().slice(0, 10)`: that is UTC, so
 *  between 00:00 and 02:00 Berlin time it names the PREVIOUS day, and every
 *  month-boundary decision (which day a month opens on, which month "today" is
 *  in) would be a day behind for two hours every night. `en-CA` is the locale
 *  that formats as YYYY-MM-DD. */
export function berlinToday(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: BERLIN,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

/** "06 Dec 2026, 19:00" — Berlin wall time, day-first + 24-hour. The year is
 *  always present (a month-scoped view must not be ambiguous; en-GB would
 *  otherwise drop it when hour/minute are requested). */
export function berlinInstant(instant: string | null, missing = "Not recorded"): string {
  if (!instant) return missing;
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: BERLIN,
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(new Date(instant));
}

/** "06 Dec 2026, 19:00" with the year always present. */
export function berlinInstantWithYear(instant: string, missing = "Not recorded"): string {
  if (!instant) return missing;
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: BERLIN,
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(new Date(instant));
}

/** "19:00" — Berlin wall time only, 24-hour. For pairing with berlinInstant
 *  when a range must be readable without repeating the date. An absent or
 *  unparseable instant renders the placeholder instead of throwing, so a
 *  partial payload degrades to "—" rather than breaking the page. */
export function berlinTime(instant: string | null, missing = "—"): string {
  if (!instant) return missing;
  const at = new Date(instant);
  if (Number.isNaN(at.getTime())) return missing;
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: BERLIN,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(at);
}

/** "July 2026" from "2026-07" (deterministic, TZ-safe). */
export function monthTitle(month: string): string {
  return new Intl.DateTimeFormat("en", { month: "long", year: "numeric", timeZone: "UTC" }).format(
    new Date(`${month}-01T00:00:00Z`),
  );
}

/** "06 Dec 2026" from an ISO calendar DATE ("2026-12-06").
 *
 *  A calendar date is not an instant: formatting it in the business timezone
 *  would shift it by a day for anyone whose browser is behind Berlin, so this
 *  reads it in UTC — the same reason `monthTitle` does. Ranges of institutional
 *  dates (a calendar edition's effective range) are what this is for. */
export function isoDate(value: string): string {
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: "UTC",
    day: "2-digit",
    month: "short",
    year: "numeric",
  }).format(new Date(`${value}T00:00:00Z`));
}

/** "2026-07-19T19:00" — local wall-clock value for datetime inputs (ISO
 *  transport form; the visible European format is handled by lib/inputs). */
export function berlinLocalDateTime(instant: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: BERLIN,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(instant));
  const value = (type: string) => parts.find((part) => part.type === type)?.value ?? "";
  return `${value("year")}-${value("month")}-${value("day")}T${value("hour")}:${value("minute")}`;
}

/** Berlin wall-clock calendar day ("2026-07-19") — for asking whether two
 *  instants fall on the same local day without re-parsing a display string.
 *  An unparseable instant yields "" so a partial payload cannot throw here. */
function berlinDayKey(instant: string): string {
  const at = new Date(instant);
  if (Number.isNaN(at.getTime())) return "";
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: BERLIN,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(at);
}

/** "19 Jul 2026, 19:00 → 07:00" — the short range form. An interval that
 *  crosses midnight repeats the end date instead (INV-02: "19:00 → 07:00"
 *  does not say whether 07:00 is that morning or the next one). Like
 *  berlinTime, a missing or unparseable instant degrades to the placeholder
 *  rather than throwing. */
export function berlinRange(start: string, end: string): string {
  const head = berlinInstant(start);
  const endDay = berlinDayKey(end);
  // An absent or unparseable end keeps the short form, where berlinTime
  // already degrades to its placeholder instead of throwing.
  if (!endDay || berlinDayKey(start) === endDay) return `${head} → ${berlinTime(end)}`;
  return `${head} → ${berlinInstant(end)}`;
}
