import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";

import {
  applyTemplate,
  copyPreviousDay,
  createBreak,
  createDayStatus,
  createInterval,
  createShiftPlan,
  deleteBreak,
  deleteDayStatus,
  deleteInterval,
  fetchDay,
  fetchDayStatus,
  fetchFindings,
  fetchIdentity,
  fetchMonth,
  fetchOcd,
  fetchOnboarding,
  fetchProfile,
  fetchShifts,
  fieldIssueFor,
  firstErrorCode,
  isForbiddenError,
  monthRange,
  patchBreak,
  patchDayStatus,
  splitInterval,
  updateInterval,
  type BreakInterval,
  type BreakKind,
  type CalendarClass,
  type DailyResult,
  type DayStatus,
  type DayStatusCategory,
  type DayStatusDayPart,
  type DayStatusPortion,
  type Finding,
  type Interval,
  type OcdAssignment,
  type OcdCall,
  type Shift,
  type WorkType,
} from "../lib/api";
import {
  AUTHORITATIVE_SOURCES,
  breakKindLabel,
  breakKindLabels,
  calendarClassLabel,
  calendarEventLabels,
  calendarStructureMarker,
  coverageBlockedMessage,
  dayStatusCategoryLabel,
  dayStatusCategoryLabels,
  dayStatusDayPartLabels,
  dayStatusRuleLabel,
  shiftKindLabel,
  shiftKindLabels,
  type CalendarEventKind,
} from "../lib/labels";
import { CalendarEventBars, type CalendarEvent } from "../lib/calendar-event";
import { ConfirmAction } from "../lib/confirm";
import { OverflowMenu } from "../lib/menu";
import { ActionNotice, errorNotice, FieldIssue, refusalText } from "../lib/notice";
import { berlinRange, berlinTime, berlinToday } from "../lib/format";
import { invalidateFactDerived } from "../lib/query";
import { worstSeverity } from "../lib/severity";
import { ShiftPlanActualCard } from "./shift-card";

const workTypes: Array<[WorkType, string]> = [
  ["ON_SITE", "On-site"],
  ["REMOTE", "Remote"],
  ["SHIFT", "Shift"],
  ["OCD_CALL", "OCD call"],
  ["OVERTIME_PAID", "Overtime paid"],
  ["OVERTIME_TIME_COMP", "Overtime time compensation"],
  ["BUSINESS_TRIP_WORK", "Business trip work"],
];

const dayStatusCategories = Object.keys(dayStatusCategoryLabels) as DayStatusCategory[];

/** Calendar-event scan order: what was planned for the day first (it is what the
 *  employee acts on), then absences, then compensation days. Retained history is
 *  sorted after ALL of them by the caller, so a cancelled shift can never
 *  outrank a live on-call duty. */
const eventOrder: CalendarEventKind[] = [
  "SHIFT",
  "OCD",
  "VACATION",
  "SICK",
  "FLEX_COMPENSATION",
  "SHIFT_COMPENSATION",
];

const breakKinds = Object.keys(breakKindLabels) as BreakKind[];

/** "full day" / "morning half" / "afternoon half". The stored day part is only
 *  meaningful for a HALF day — the server refuses any other pairing. */
function dayStatusExtent(entry: DayStatus): string {
  if (entry.portion === "FULL") return "full day";
  return `${dayStatusDayPartLabels[entry.day_part].toLowerCase()} half`;
}

/** Spoken description ("Vacation (full day)"): the calendar marker and the
 *  status actions carry it, so no state is visual-only. */
function dayStatusDescription(entry: DayStatus): string {
  return `${dayStatusCategoryLabel(entry.category)} (${dayStatusExtent(entry)})`;
}

/** The extent a calendar bar shows, or NOTHING for the ordinary case.
 *
 *  A FULL day is the default and needs no word: measured against the live grid, a
 *  bar has about 77px of text space, and "Shift comp" plus "full" needs 80 — the
 *  longest category word ellipsized in every cell that had one. Stating the
 *  EXCEPTION rather than the default is the same principle the calendar
 *  classification already uses (it enumerates non-working days, not working ones),
 *  and it removes a word from most bars rather than shrinking the type.
 *
 *  A HALF day is the exception and keeps a two-letter extent: `am` / `pm` beside a
 *  day status are unambiguous, where "morning half" is twelve characters and
 *  ellipsized in every cell. The FULL words ("Vacation (morning half)") stay in
 *  the day panel's Day status section and in the cell's accessible name, so nothing
 *  is lost — only shortened where space is scarce. */
function dayStatusExtentCompact(entry: DayStatus): string | undefined {
  if (entry.portion === "FULL") return undefined;
  return entry.day_part === "MORNING" ? "am" : "pm";
}

function dateFor(month: string, offset: number): string {
  const [year, monthNumber] = month.split("-").map(Number);
  return new Date(Date.UTC(year, monthNumber - 1, offset)).toISOString().slice(0, 10);
}

function daysOfMonth(month: string): string[] {
  const [year, monthNumber] = month.split("-").map(Number);
  const count = new Date(Date.UTC(year, monthNumber, 0)).getUTCDate();
  return Array.from({ length: count }, (_, index) => dateFor(month, index + 1));
}

export function displayMonth(month: string): string {
  return new Intl.DateTimeFormat("en", { month: "long", year: "numeric", timeZone: "UTC" }).format(
    new Date(`${month}-01T00:00:00Z`),
  );
}

/** Deterministic "Tue, 7 July" heading (spec §9): built from parts so the
 *  order never depends on the runtime's locale conventions.
 *
 *  `weekday` is a parameter because the calendar cell's ACCESSIBLE NAME needs the
 *  full word: an abbreviation is read as a syllable by some screen readers, and
 *  the weekday is the one thing that tells a non-visual user which columns are
 *  the weekend. The visible heading keeps the short form. */
function formatDayHeading(day: string, weekday: "short" | "long" = "short"): string {
  const parts = new Intl.DateTimeFormat("en", {
    weekday,
    day: "numeric",
    month: "long",
    timeZone: "UTC",
  }).formatToParts(new Date(`${day}T00:00:00Z`));
  const part = (type: string) => parts.find((item) => item.type === type)?.value ?? "";
  return `${part("weekday")}, ${part("day")} ${part("month")}`;
}

/** Every Europe/Berlin calendar date an instant span OCCUPIES (INV-02).
 *
 *  A cross-midnight shift is an ordinary pair of timestamps here, not an edge
 *  case, so a plan running 19:00 → 03:00 genuinely occupies TWO calendar days and
 *  the month grid has to show it on both. Anchoring the marker to the start date
 *  alone hid the second day completely — the seeded July shift ends at 03:00 on
 *  the 8th and the 8th showed nothing at all.
 *
 *  The end instant is stepped back one millisecond before its date is taken: a
 *  span ending at exactly 00:00 covers none of the following day, and reading its
 *  date directly would claim a day the shift never touches. */
function berlinDatesSpanned(startInstant: string, endInstant: string): string[] {
  const first = berlinDate(startInstant);
  const last = berlinDate(new Date(Date.parse(endInstant) - 1).toISOString());
  if (last <= first) return [first];
  const dates: string[] = [];
  // Bounded by the span itself; the guard only stops a malformed payload from
  // looping forever, since no real assignment runs for months.
  for (let date = first, guard = 0; date <= last && guard < 64; date = nextDate(date), guard += 1) {
    dates.push(date);
  }
  return dates;
}

/** Saturday or Sunday, from the date itself.
 *
 *  The server's `calendar_class` cannot answer this: it enumerates EXCEPTIONS and
 *  emits `WEEKDAY` for Saturdays and Sundays too (lib/labels.ts, EINV-10), so
 *  "is this a weekend" is a property of the date, not of the classification. */
function isWeekend(date: string): boolean {
  const weekday = new Date(`${date}T00:00:00Z`).getUTCDay();
  return weekday === 0 || weekday === 6;
}

/** The non-working structure a calendar cell shows, or null for an ordinary day.
 *
 *  The two INSTITUTIONAL classes win over the weekend: a public holiday that
 *  falls on a Saturday is a holiday, and downgrading it to "weekend" would hide
 *  the classification the day panel reports. */
function nonWorkingKind(
  date: string,
  calendarClass: CalendarClass | undefined,
): "WEEKEND" | "PUBLIC_HOLIDAY" | "EMPLOYER_FREE" | null {
  if (calendarClass === "PUBLIC_HOLIDAY") return "PUBLIC_HOLIDAY";
  if (calendarClass === "EMPLOYER_FREE") return "EMPLOYER_FREE";
  return isWeekend(date) ? "WEEKEND" : null;
}

/** The structure in words, for the cell's accessible name. Spelled out rather
 *  than left to the `H` / `X` marker: the marker is a compact cue for a sighted
 *  user, and a non-visual user must hear which kind of non-working day it is. */
const nonWorkingWords: Record<string, string> = {
  WEEKEND: "weekend",
  PUBLIC_HOLIDAY: "public holiday",
  EMPLOYER_FREE: "employer-free day",
};

/** Signed human-readable duration ("+2:12", "−5:48"); used for deltas and balances. */
export function minutes(value: number): string {
  const absolute = Math.abs(value);
  return `${value < 0 ? "−" : value > 0 ? "+" : ""}${Math.floor(absolute / 60)}:${String(absolute % 60).padStart(2, "0")}`;
}

/** Unsigned human-readable duration ("8:00", "5:48"); used for worked/required. */
export function plainMinutes(value: number): string {
  const absolute = Math.abs(value);
  return `${Math.floor(absolute / 60)}:${String(absolute % 60).padStart(2, "0")}`;
}

function localParts(value: string): { date: string; time: string } {
  return { date: value.slice(0, 10), time: value.slice(11, 16) };
}

function isValidTime(value: string): boolean {
  return /^([01]\d|2[0-3]):[0-5]\d$/.test(value);
}

/** UI-012: `14:30 → 08:00` with "Ends next day" unticked is a negative span,
 *  which the server rejects (`VALIDATION_INTERVAL_ORDER`) — but the form used to
 *  leave its primary button enabled and promise otherwise. The checkbox is
 *  exactly what decides whether a later-looking end time is impossible or the
 *  next morning, so this is the whole rule. Returns the reason to state next to
 *  the button, or null when the two times can be sent. */
function intervalOrderError(
  startTime: string,
  endTime: string,
  endNextDay: boolean,
): string | null {
  // An empty field is not a mistake yet — it is simply not filled in, and the
  // disabled button already says so.
  if (startTime === "" || endTime === "") return null;
  if (!isValidTime(startTime) || !isValidTime(endTime)) return "Enter a time as HH:MM.";
  if (endNextDay) return null;
  if (endTime <= startTime) {
    return "The end time must be after the start time — tick “Ends next day” if it runs past midnight.";
  }
  return null;
}

/** Deterministic 24-hour time fields, shared across the product: native
 *  time inputs render in the browser's UI locale (AM/PM on
 *  en-US Chromium); formatted text fields keep 24-hour entry and display
 *  consistent everywhere (spec §10). */
import { DateTimeField, TimeField, nextDate, toIsoDateTime } from "../lib/inputs";

/** DST fold (0 = first, 1 = second occurrence) implied by a STORED aware
 *  instant for a Berlin wall time. Unambiguous wall times resolve identically
 *  either way, so returning 0 there is harmless; ambiguous ones (fall-back
 *  hour) must carry the fold or the server rejects them (docs/ARCHITECTURE.md). */
function foldFor(instantIso: string, wallLocal: string): 0 | 1 {
  const instant = Date.parse(instantIso);
  const wallAsUtc = Date.parse(`${wallLocal.slice(0, 16)}:00Z`);
  // Europe/Berlin: fold=1 means CET (+01:00), fold=0 means CEST (+02:00).
  return Math.round((instant - wallAsUtc) / 60000) === 60 ? 1 : 0;
}

function duplicateAfter(interval: Interval): {
  start_local: string;
  end_local: string;
  work_type: WorkType;
  comment?: string | null;
} {
  const start = new Date(
    `${localParts(interval.start_local).date}T${localParts(interval.start_local).time}:00Z`,
  );
  const end = new Date(
    `${localParts(interval.end_local).date}T${localParts(interval.end_local).time}:00Z`,
  );
  const duplicateEnd = new Date(end.getTime() + end.getTime() - start.getTime());
  return {
    start_local: end.toISOString().slice(0, 16),
    end_local: duplicateEnd.toISOString().slice(0, 16),
    work_type: interval.work_type,
    comment: interval.comment,
  };
}

function adjacentMonth(month: string, offset: number): string {
  const date = new Date(`${month}-01T00:00:00Z`);
  date.setUTCMonth(date.getUTCMonth() + offset);
  return date.toISOString().slice(0, 7);
}

function monthEndDate(month: string): string {
  return `${adjacentMonth(month, 1)}-01`;
}

/** The Europe/Berlin calendar date an instant falls on (§17).
 *
 *  Extracted because two surfaces now need the same answer: the day panel filters
 *  its shift cards by the day a plan STARTS on, and the calendar marker counts the
 *  plans per day. Two copies of this conversion is exactly how a plan comes to sit
 *  on one day in the panel and a different one in the calendar — a cross-midnight
 *  shift is an ordinary case here, not an edge case. */
function berlinDate(instant: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Europe/Berlin",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(instant));
  const value = (type: string) => parts.find((part) => part.type === type)?.value ?? "";
  return `${value("year")}-${value("month")}-${value("day")}`;
}

// Every write on this page — an interval, a break, a day status, a split, a
// copy — changes facts the calendar cell, the day panel, the month balance and
// the shift surface all derive from, so they refresh together. A break is a
// calculation input for the month AND for any shift it is linked to (SHIFT-003's
// paid-break requirement is resolved from that link). The shared helper owns the
// family list, because a fact can cross midnight, a month or a year and a
// month-scoped key would miss the neighbouring month (§17).

/** Current calendar month in the canonical business timezone (§17). */
export function currentBerlinMonth(): string {
  // One captured instant so a midnight rollover between the two format
  // calls cannot mix year and month.
  const now = new Date();
  const year = new Intl.DateTimeFormat("en", { timeZone: "Europe/Berlin", year: "numeric" }).format(
    now,
  );
  const month = new Intl.DateTimeFormat("en", {
    timeZone: "Europe/Berlin",
    month: "2-digit",
  }).format(now);
  return `${year}-${month}`;
}

/** Positive gap between consecutive intervals, in minutes, when the gap reads
 *  as a break. Negative/zero gaps (overlaps, touching intervals) and large
 *  gaps (split days, overnight pauses) are not shown (spec §4).
 *  ceiling: the 240-minute ceiling is a display heuristic, not a rule;
 *  revisit when compliance logic needs real break semantics. */
function gapMinutes(previous: Interval, next: Interval): number | null {
  const gap = Math.round((Date.parse(next.start_at) - Date.parse(previous.end_at)) / 60000);
  return gap > 0 && gap <= 240 ? gap : null;
}

function breakLabel(previous: Interval, next: Interval): string | null {
  const gap = gapMinutes(previous, next);
  if (gap === null) return null;
  const hours = Math.floor(gap / 60);
  const rest = gap % 60;
  if (hours === 0) return `${gap} min break`;
  return rest === 0 ? `${hours} h break` : `${hours} h ${rest} min break`;
}

/** Compact weekday short label ("Tue 7 Jul") for cross-midnight end dates. */
function shortDayLabel(dateIso: string): string {
  const parts = new Intl.DateTimeFormat("en", {
    weekday: "short",
    day: "numeric",
    month: "short",
    timeZone: "UTC",
  }).formatToParts(new Date(`${dateIso}T00:00:00Z`));
  const part = (type: string) => parts.find((item) => item.type === type)?.value ?? "";
  return `${part("weekday")} ${part("day")} ${part("month")}`;
}

/** Berlin's UTC offset at an instant, read from the zone database — never a
 *  hard-coded +01:00/+02:00, which is precisely what DST changes. */
function berlinOffsetMs(instant: number): number {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/Berlin",
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(new Date(instant));
  const part = (type: string) => parts.find((item) => item.type === type)?.value ?? "00";
  const wall = `${part("year")}-${part("month")}-${part("day")}T${part("hour")}:${part("minute")}:${part("second")}Z`;
  return Date.parse(wall) - instant;
}

/** The instant at which Berlin's wall clock reads 00:00 on `date`. Berlin
 *  shifts at 01:00 UTC and never at midnight, so the offset at 00:00 UTC that
 *  day is also the offset at the boundary. */
function berlinDayStart(date: string): number {
  const naiveMidnight = Date.parse(`${date}T00:00:00Z`);
  return naiveMidnight - berlinOffsetMs(naiveMidnight);
}

/** Minutes of an interval that fall on the given Berlin day, measured in
 *  elapsed time between the stored instants and the day's Berlin boundaries.
 *  Wall-clock arithmetic cannot do this: it puts a cross-midnight interval's
 *  portion on the wrong side of midnight, and it drops the hour the clock
 *  shifts on the two DST transition days (a 23 h or 25 h day). The day panel
 *  sums these rows, so they must add up to the interval's elapsed duration. */
function dayPortionMinutes(interval: Interval, day: string): number {
  const from = Math.max(Date.parse(interval.start_at), berlinDayStart(day));
  const to = Math.min(Date.parse(interval.end_at), berlinDayStart(nextDate(day)));
  return Math.max(0, Math.round((to - from) / 60000));
}

/** Shared month-context navigation: every month-scoped page
 *  keeps the route's $month, so Review/OCD/Overtime/Sharing/Travel never lose
 *  the month the user is working on. Previous/This/Next preserve the current
 *  section; aria-current marks the current item. */
export function MonthContextNav({
  month,
  current,
}: {
  month: string;
  current: "month" | "review" | "ocd" | "overtime" | "submissions" | "travel";
}) {
  const sectionPath = (target: string) =>
    current === "travel"
      ? `/travel/${target}`
      : current === "month"
        ? `/month/${target}`
        : `/month/${target}/${current}`;
  const items: Array<{ id: typeof current; to: string; label: string }> = [
    { id: "month", to: `/month/${month}`, label: "Month" },
    { id: "review", to: `/month/${month}/review`, label: "Review this month" },
    { id: "ocd", to: `/month/${month}/ocd`, label: "On-call duty" },
    { id: "overtime", to: `/month/${month}/overtime`, label: "Overtime" },
    { id: "submissions", to: `/month/${month}/submissions`, label: "Submissions" },
    { id: "travel", to: `/travel/${month}`, label: "Travel" },
  ];
  const sections = useRef<HTMLDivElement>(null);
  // UI-005: on a phone the section tabs are a single-row scroller, so landing
  // on `/month/<m>/overtime` from a link or a reload has to bring the current
  // section into view — otherwise the reader is left looking at `Month` and
  // cannot see where they are. A no-op on desktop, where nothing overflows.
  useEffect(() => {
    sections.current
      ?.querySelector('[aria-current="page"]')
      ?.scrollIntoView({ inline: "center", block: "nearest" });
  }, [current, month]);
  return (
    <nav className="month-context" aria-label="Month navigation">
      {/* The stepper is its own row on a phone and deliberately OUTSIDE the
          scroller below: changing month is the most-used action here and must
          never require a horizontal scroll. */}
      <div className="month-steps">
        <Link
          to={sectionPath(adjacentMonth(month, -1)) as never}
          aria-label="Previous month"
          className="month-step"
        >
          ← Previous month
        </Link>
        <Link
          to={sectionPath(currentBerlinMonth()) as never}
          className="month-step"
          aria-label="This month"
        >
          This month
        </Link>
        <Link
          to={sectionPath(adjacentMonth(month, 1)) as never}
          aria-label="Next month"
          className="month-step"
        >
          Next month →
        </Link>
        <span className="month-label">{displayMonth(month)}</span>
        <span className="month-sep" aria-hidden="true" />
      </div>
      <div className="month-sections" ref={sections}>
        {items.map((item) => (
          <Link
            key={item.id}
            to={item.to as never}
            // UI-001: `Link` marks itself current on a PREFIX match by default,
            // so `/month/2026-07/review` left `Month` current as well as the
            // review item. `aria-current` below is the single source of truth.
            activeOptions={{ exact: true }}
            aria-current={current === item.id ? "page" : undefined}
            // `month-tab` is what the "current section" pill is scoped to. The
            // three step controls above are a separate set of destinations, and
            // `This month` is legitimately current when the current month is the
            // one open — without the scope that drew a SECOND navy pill next to
            // the section pill on `/month/<current month>`.
            className="month-step month-tab"
          >
            {item.label}
          </Link>
        ))}
      </div>
    </nav>
  );
}

/** UI-046: `?day=` is honoured only for a real calendar date inside the month
 *  the route names. A hand-edited URL must not put the day panel — and with it
 *  the day editor's new-interval default — on a foreign date, which would show a
 *  July workspace editing an August day. */
function inMonthDay(month: string, day: string | undefined): string | undefined {
  if (!day || !day.startsWith(`${month}-`)) return undefined;
  const parsed = new Date(`${day}T00:00:00Z`);
  // A well-formed but impossible date (2026-02-30) rolls over, so the round
  // trip catches it; an unparseable value fails the same check.
  return Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== day
    ? undefined
    : day;
}

/** The day a month opens on when nothing else names one: TODAY, when the month
 *  being viewed is the current Berlin month, and the 1st otherwise. Opening on
 *  the 1st meant the employee landed on a day they had no reason to look at and
 *  had to walk to today every single time. A past or future month has no "today"
 *  in it, so it keeps the 1st. */
export function defaultDayFor(month: string): string {
  const today = berlinToday();
  return today.startsWith(month) ? today : `${month}-01`;
}

export function MonthWorkspace({ month, initialDay }: { month: string; initialDay?: string }) {
  const queryClient = useQueryClient();
  const openedDay = inMonthDay(month, initialDay);
  const [selectedDate, setSelectedDate] = useState(openedDay ?? defaultDayFor(month));
  const [previewDates, setPreviewDates] = useState<string[] | null>(null);
  const monthQuery = useQuery({ queryKey: ["month", month], queryFn: () => fetchMonth(month) });
  const findingsQuery = useQuery({
    queryKey: ["findings", month],
    queryFn: () => fetchFindings(month),
  });
  const dayStatusQuery = useQuery({
    queryKey: ["day-status", month],
    queryFn: () => fetchDayStatus(month),
  });
  const profileQuery = useQuery({
    queryKey: ["profile", month],
    // `fetchProfile` takes a DATE: the month workspace wants the arrangement in
    // force at the START of the month it is showing.
    queryFn: () => fetchProfile(`${month}-01`),
  });
  // The employee's own Chronobeam start, for the months BEFORE it. Those months
  // stay in the Excel archive, so presenting a computed balance for them reads as
  // debt the service never tracked. The read is the same one Profile uses (and the
  // same `["onboarding"]` cache entry), so the two surfaces cannot disagree about
  // when the service begins.
  const onboardingQuery = useQuery({ queryKey: ["onboarding"], queryFn: () => fetchOnboarding() });
  const shiftsQuery = useQuery({
    queryKey: ["shifts", month],
    queryFn: () => fetchShifts(`${month}-01`, monthEndDate(month)),
  });
  // The month's OCD availability assignments, for the calendar's On-call marker
  // and for the day panel's Planned assignments section. Deliberately the SAME
  // `["ocd", month]` key and queryFn the on-call page and `DayTimeline` already
  // use, so the three surfaces share one cache entry and cannot disagree about
  // what the month holds — and visiting the month costs no extra request.
  const ocdRange = monthRange(month);
  const ocdQuery = useQuery({
    queryKey: ["ocd", month],
    queryFn: () => fetchOcd(ocdRange.from, ocdRange.to),
  });
  // The SAME `["identity"]` cache entry the shell already reads, so the add
  // control cannot disagree with the navigation about who this session is.
  //
  // Deliberately NOT part of the blocking gate below: the capability only decides
  // whether an OPTIONAL control is offered, so a slow or failed identity read must
  // not hold the month hostage. An absent capability is read as "not granted"
  // (`?? []`), never as "allowed" — the fail-closed direction.
  const identityQuery = useQuery({ queryKey: ["identity"], queryFn: fetchIdentity });
  const canPlanShift = (identityQuery.data?.capabilities ?? []).includes("SHIFT_PLAN_ENTRY_WRITE");
  // The on-call domain's own switch (§6.1). Read here only to decide whether the
  // Planned assignments section OFFERS the link to the on-call page — the page
  // itself gates its form on the same capability, so the two cannot disagree.
  const canPlanOcd = (identityQuery.data?.capabilities ?? []).includes("OCD_PLAN_ENTRY_WRITE");
  const applyMutation = useMutation({
    mutationFn: ({ dates, command }: { dates: string[]; command: string }) =>
      applyTemplate(month, dates, command),
    onSuccess: () => {
      setPreviewDates(null);
      invalidateFactDerived(queryClient);
    },
  });
  useEffect(() => {
    setSelectedDate(defaultDayFor(month));
    setPreviewDates(null);
  }, [month]);
  // UI-046: arriving from a finding link opens that finding's own day. This
  // effect is declared after the month one, so on mount it wins; a later month
  // change leaves `openedDay` untouched and keeps the default-day behaviour
  // above (today for the current month, the 1st otherwise).
  useEffect(() => {
    if (openedDay) setSelectedDate(openedDay);
  }, [openedDay]);
  const dailyByDate = useMemo(
    () => new Map(monthQuery.data?.daily_results.map((result) => [result.date, result])),
    [monthQuery.data],
  );
  const intervalsByDate = useMemo(
    () => new Map(monthQuery.data?.days.map((day) => [day.date, day.interval_count])),
    [monthQuery.data],
  );
  // Keep the private month workspace usable if a stale test fixture or
  // misbehaving intermediary returns a non-list payload for this list route.
  const shifts = useMemo<Shift[]>(
    () => (Array.isArray(shiftsQuery.data) ? shiftsQuery.data : []),
    [shiftsQuery.data],
  );
  const ocdAssignments = useMemo<OcdAssignment[]>(
    () => (Array.isArray(ocdQuery.data) ? ocdQuery.data : []),
    [ocdQuery.data],
  );
  // Current day-status heads per day. Defensive against a non-list payload the
  // same way the shifts query is: this is a supplementary fact layer.
  const dayStatusByDate = useMemo(() => {
    const entries = Array.isArray(dayStatusQuery.data?.entries) ? dayStatusQuery.data.entries : [];
    const map = new Map<string, DayStatus[]>();
    for (const entry of entries) map.set(entry.day, [...(map.get(entry.day) ?? []), entry]);
    return map;
  }, [dayStatusQuery.data]);

  /** Every calendar event per day, in the order a scan should read them: what was
   *  PLANNED for the day first (it is what the employee acts on), then absences
   *  and compensation days, then retained history.
   *
   *  ONE derivation for the whole grid rather than one per cell, because a shift
   *  that crosses midnight contributes to TWO days: building the map in a single
   *  pass is what keeps "which day does this plan sit on" answered in exactly one
   *  place (INV-02 — the previous per-cell lookup anchored every marker to the
   *  plan's start date and so hid the second day entirely). */
  const eventsByDate = useMemo(() => {
    const map = new Map<string, CalendarEvent[]>();
    const push = (date: string, event: CalendarEvent) => {
      // The grid draws only this month's own days; a span reaching into the next
      // month still marks the days that fall inside this one.
      if (!date.startsWith(month)) return;
      map.set(date, [...(map.get(date) ?? []), event]);
    };

    for (const shift of shifts) {
      // ONLY an active plan, or retained history of one, is a fact about the day.
      // A WITHDRAWN assignment was never the real plan and a SUPERSEDED one was
      // replaced, so neither may be presented as something the employee still
      // holds (§4.7) — the same predicate the day panel's card uses.
      //
      // "Aborted by the Company" is a DEVIATION type, not a Shift status
      // (lib/labels.ts), so it has to be read off the deviation. Such a row is
      // still `PLANNED` by status, but the shift did not happen as planned, so it
      // is HISTORY: rendering it as active would claim a plan the employee is
      // still expected to work. The two history kinds keep DIFFERENT words,
      // because an abortion by the Company is not a cancellation and reporting it
      // as one would misstate what the Company did.
      const aborted = shift.deviation?.deviation_type === "ABORTED_BY_COMPANY";
      const cancelled = shift.status === "CANCELLED" || aborted;
      const active = shift.status === "PLANNED" && !cancelled;
      if (!active && !cancelled) continue;
      const start = shift.approved_revision.planned_start_at;
      const end = shift.approved_revision.planned_end_at;
      const kind = shiftKindLabel(shift.shift_kind);
      const window = berlinRange(start, end);
      const historyWord = aborted ? "aborted" : "cancelled";
      berlinDatesSpanned(start, end).forEach((date, index) => {
        push(date, {
          kind: "SHIFT",
          // History states its state in the LABEL, so the bar can never be read as
          // a live plan: "Shift" alone would be a claim the employee still holds
          // this assignment.
          label: active ? undefined : `${calendarEventLabels.SHIFT.label} ${historyWord}`,
          // The SECOND day of a cross-midnight span must not repeat the start
          // time: "Shift 22:00" on the 8th would claim a shift beginning at 22:00
          // on the 8th. "continues" says what is actually true of that day.
          // A history bar carries NO detail. Its label is the longest in the
          // system ("Shift cancelled", 78px of the ~80px a cell can give) and
          // adding the start time on top would ellipsize the label — the one thing
          // a bar must never truncate. The exact window is in the day panel and in
          // the accessible name; the STATE is what matters while scanning.
          //
          // "cont." rather than "continues" for an active plan: the full word needs
          // 50px and would ellipsize in every cross-midnight cell. The full
          // sentence stays in the accessible name.
          detail: !active ? undefined : index === 0 ? berlinTime(start) : "cont.",
          state: active ? "active" : "cancelled",
          spoken: `${
            active ? "planned" : aborted ? "aborted by the Company" : "cancelled"
          } ${kind.toLowerCase()}, ${window}${index > 0 ? ", continues from the previous day" : ""}`,
        });
      });
    }

    for (const assignment of ocdAssignments) {
      // Availability is a PLAN; a call is effective work and belongs to the day
      // panel's actual-work timeline, not to this marker (INV-10 keeps the two
      // apart). Only a scheduled assignment is on the calendar.
      if (assignment.status !== "SCHEDULED") continue;
      const window = berlinRange(assignment.start_at, assignment.end_at);
      berlinDatesSpanned(assignment.start_at, assignment.end_at).forEach((date, index) => {
        push(date, {
          kind: "OCD",
          detail: index === 0 ? berlinTime(assignment.start_at) : "cont.",
          spoken: `on-call duty scheduled, ${window}${
            index > 0 ? ", continues from the previous day" : ""
          }`,
        });
      });
    }

    for (const [date, entries] of dayStatusByDate) {
      for (const entry of entries) {
        // `DayStatusCategory` and the status half of `CalendarEventKind` are the
        // same four codes, so the status maps straight onto the marker.
        push(date, {
          kind: entry.category,
          detail: dayStatusExtentCompact(entry),
          spoken: dayStatusDescription(entry),
        });
      }
    }

    // Deterministic scan order. Retained history sorts last regardless of its
    // category, so a cancelled shift never outranks a live on-call duty.
    const rank = (event: CalendarEvent) =>
      (event.state === "cancelled" ? 100 : 0) + eventOrder.indexOf(event.kind);
    for (const list of map.values()) list.sort((a, b) => rank(a) - rank(b));
    return map;
  }, [shifts, ocdAssignments, dayStatusByDate, month]);
  const selectedDay = new Date(`${selectedDate}T00:00:00Z`);
  const offsetToMonday = (selectedDay.getUTCDay() + 6) % 7;
  selectedDay.setUTCDate(selectedDay.getUTCDate() - offsetToMonday);
  const normalWeekDates = Array.from({ length: 5 }, (_, index) => {
    const date = new Date(selectedDay);
    date.setUTCDate(date.getUTCDate() + index);
    return date.toISOString().slice(0, 10);
  }).filter((date) => date.startsWith(month) && !intervalsByDate.get(date));
  // The normal-week preview is a snapshot of which days are still ELIGIBLE, and
  // eligibility is `!intervalsByDate.get(date)` — a fact. Held as a snapshot it
  // went on claiming that a day the employee had since recorded time on "will
  // receive your saved normal-day pattern", and applying it asked the server for
  // something it refuses (review R17). Deriving the claim from the same data the
  // calendar is drawn from means the preview can never promise more than the
  // month already shows — and every writer, including copy-previous-day and shift
  // confirmation, is covered without being enumerated.
  const previewEligible = previewDates?.filter(
    (date) => date.startsWith(month) && !intervalsByDate.get(date),
  );
  const hasTemplate = Boolean(profileQuery.data?.preferences.normal_day_template?.length);
  const usualWorkType = profileQuery.data?.preferences.usual_work_type ?? null;

  if (
    monthQuery.isPending ||
    findingsQuery.isPending ||
    dayStatusQuery.isPending ||
    profileQuery.isPending ||
    shiftsQuery.isPending ||
    // The on-call read is part of the gate, not a supplementary layer: it is what
    // the calendar's On-call marker and the day panel's Planned assignments are
    // drawn from, so rendering the month while it has failed would present a day
    // list that is quietly missing assignments.
    ocdQuery.isPending
  ) {
    // F27: keep the workspace shape while data loads (accessible status text
    // remains for assistive tech).
    return (
      <section aria-label="Employee month workspace" aria-busy="true">
        <div className="skeleton" aria-hidden="true">
          <div className="sk-row sk-w-18" />
          <div className="sk-row sk-w-30" />
          <div className="sk-row sk-w-22" />
        </div>
        <p role="status">Loading your private month…</p>
      </section>
    );
  }
  const forbidden = [
    monthQuery,
    findingsQuery,
    dayStatusQuery,
    profileQuery,
    shiftsQuery,
    ocdQuery,
  ].some((query) => isForbiddenError(query.error));
  if (forbidden) {
    return (
      <section className="standalone-card access-denied" aria-label="Employee month workspace">
        <h1>{displayMonth(month)}</h1>
        <p role="alert" className="denied-message">
          This workspace belongs to another account.
        </p>
        <p className="private-note">No work-time data was shown.</p>
        <div className="workspace-actions">
          <Link to="/">Back to Home</Link>
        </div>
      </section>
    );
  }
  if (
    monthQuery.isError ||
    findingsQuery.isError ||
    dayStatusQuery.isError ||
    profileQuery.isError ||
    shiftsQuery.isError ||
    ocdQuery.isError
  ) {
    // A month outside policy/calendar coverage is a PERMANENT state of the data,
    // not a transient fault: telling the employee to retry was a lie. The code
    // carries the distinction (ApiError.code), so name the actual boundary and
    // keep the generic sentence for real operational failures.
    const blocked = coverageBlockedMessage(
      firstErrorCode([
        monthQuery,
        findingsQuery,
        dayStatusQuery,
        profileQuery,
        shiftsQuery,
        ocdQuery,
      ]),
      displayMonth(month),
    );
    return (
      <p className="status-error" role="alert">
        {blocked ?? "Could not load your private workspace. Your saved entries are unchanged."}
      </p>
    );
  }
  // Keep the private month workspace usable if a stale test fixture or
  // misbehaving intermediary returns a non-list payload for this list route.
  // (`shifts` is declared with the other derived maps above, because the calendar
  // marker is derived from it and a hook cannot sit behind the guards below.)

  // A month BEFORE the employee's own Chronobeam start is Excel-archive
  // territory: the service did not run then, so there is no balance to report for
  // it. The arithmetic for such a month is correct — it is simply about a period
  // this service never tracked, and showing it as a closing balance reads as debt
  // the employee never owed. ISO months compare as strings, so no date parsing is
  // needed and no timezone can creep in.
  const startMonth =
    onboardingQuery.data?.state === "COMPLETED"
      ? (onboardingQuery.data.opening?.cutover_month ?? null)
      : null;
  const beforeServiceStart = startMonth !== null && month < startMonth;

  return (
    <section className="workspace" aria-label="Employee month workspace">
      <div className="workspace-heading">
        <div>
          <p className="eyebrow">Private — visible only to you</p>
          <div className="title-row">
            <h1>{displayMonth(month)}</h1>
            {/* Deliberately NO balance reminder, counter, badge or call to action
             *  is attached to this figure: the balance still contains unresolved
             *  WT-FLEX-009 components, so such a prompt would claim authority the
             *  number does not have (§8.5 — deferred to F8). The note lives here
             *  because the month page is the balance-bearing surface; the
             *  year-scoped compensation counts it used to sit beside are no longer
             *  rendered, but the deferral is unchanged. */}
            <p className="balance">
              {beforeServiceStart ? (
                <>
                  Before your Chronobeam start ({displayMonth(startMonth)}) — this month stays in
                  your Excel archive.
                </>
              ) : (
                <>
                  Balance at end of {displayMonth(month)}:{" "}
                  {minutes(monthQuery.data.closing_balance_minutes)}
                </>
              )}
            </p>
            <span className="source-badge">
              {monthQuery.data.status === "REVIEWED" ? "Reviewed" : "Not reviewed"}
            </span>
          </div>
        </div>
      </div>
      <MonthContextNav month={month} current="month" />
      {previewEligible && previewEligible.length > 0 && (
        <aside className="preview" aria-label="Normal week preview">
          <strong>Preview before creating actual work</strong>
          <p>
            {previewEligible.map((date) => date.slice(8)).join(", ")} will receive your saved
            normal-day pattern.
          </p>
          <button
            type="button"
            onClick={() =>
              applyMutation.mutate({ dates: previewEligible, command: crypto.randomUUID() })
            }
            disabled={applyMutation.isPending}
          >
            Apply to these days
          </button>
          <button type="button" onClick={() => setPreviewDates(null)}>
            Cancel
          </button>
        </aside>
      )}
      {applyMutation.isError && <ActionNotice notice={errorNotice(applyMutation.error)} />}

      <div className="workspace-grid">
        <MonthCalendar
          month={month}
          selectedDate={selectedDate}
          onSelect={setSelectedDate}
          dailyByDate={dailyByDate}
          intervalsByDate={intervalsByDate}
          eventsByDate={eventsByDate}
          findings={findingsQuery.data.findings}
        />
        <DayPanel
          onFactsChanged={() => setPreviewDates(null)}
          key={selectedDate}
          month={month}
          day={selectedDate}
          dailyResult={dailyByDate.get(selectedDate)}
          dayStatus={dayStatusByDate.get(selectedDate) ?? []}
          findings={findingsQuery.data.findings}
          shifts={shifts.filter((shift) =>
            berlinDatesSpanned(
              shift.approved_revision.planned_start_at,
              shift.approved_revision.planned_end_at,
            ).includes(selectedDate),
          )}
          ocdAssignments={ocdAssignments.filter((assignment) =>
            berlinDatesSpanned(assignment.start_at, assignment.end_at).includes(selectedDate),
          )}
          canPlanShift={canPlanShift}
          canPlanOcd={canPlanOcd}
          hasTemplate={hasTemplate}
          canPreviewWeek={normalWeekDates.length > 0}
          applyBusy={applyMutation.isPending}
          previousDayIntervalCount={intervalsByDate.get(previousDayIso(selectedDate)) ?? 0}
          onApplyNormalDay={() =>
            applyMutation.mutate({ dates: [selectedDate], command: crypto.randomUUID() })
          }
          onPreviewWeek={() => setPreviewDates(normalWeekDates.slice(0, 5))}
          usualWorkType={usualWorkType}
        />
      </div>
    </section>
  );
}

/** Year-scoped counts of recorded compensation facts (F6.4/EINV-25).
 *
 *  REMOVED from the Month page (owner directive, 2026-09-26): the block added
 *  substantial visual weight at the bottom of the primary workspace for
 *  information the employee does not act on there. It is NOT replaced by another
 *  summary widget, and nothing underneath it changed — `compensation_metrics` is
 *  still computed by the server and still sent on the day-status payload, so the
 *  figures can move to Review, Compensation or Overtime without a backend change.
 *
 *  Do not reintroduce it here without a new owner decision. */

function MonthCalendar({
  month,
  selectedDate,
  onSelect,
  dailyByDate,
  intervalsByDate,
  eventsByDate,
  findings,
}: {
  month: string;
  selectedDate: string;
  onSelect: (date: string) => void;
  dailyByDate: Map<string, DailyResult>;
  intervalsByDate: Map<string, number>;
  /** Every calendar event per day, already ordered for scanning. Built once by
   *  the workspace because a cross-midnight span marks two days. */
  eventsByDate: Map<string, CalendarEvent[]>;
  findings: Array<{ date: string | null; severity: string; message: string }>;
}) {
  // Europe/Berlin weeks start on Monday (§17): getUTCDay()=1 (Mon) -> 0.
  const firstWeekday = (new Date(`${month}-01T00:00:00Z`).getUTCDay() + 6) % 7;
  // Month-level findings (date null) are not day-specific, so they surface in
  // the calendar header instead of repeating on every cell (spec §6).
  //
  // Only a CRITICAL one is an alarm. Overtime and corridor deviations are
  // ordinary here, so a permanent month-level WARNING badge would just train
  // people to ignore the header; a non-critical finding keeps the same calm
  // wording as the day pin.
  const monthFindings = findings.filter((item) => item.date === null);
  const monthAlarm = monthFindings.find((item) => item.severity === "CRITICAL");
  return (
    <section className="calendar-panel" aria-label={`${displayMonth(month)} calendar`}>
      <h2>
        Month
        {monthAlarm ? (
          <span className="cell-finding severity-critical" role="note">
            ⚠ {monthAlarm.severity}
          </span>
        ) : monthFindings.length > 0 ? (
          <span className="cell-finding status-muted" role="note">
            {monthFindings.length} month-level finding{monthFindings.length === 1 ? "" : "s"}
          </span>
        ) : null}
      </h2>
      <div className="calendar-weekdays" aria-hidden="true">
        {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((day) => (
          <span key={day}>{day}</span>
        ))}
      </div>
      <div className="calendar-grid">
        {Array.from({ length: firstWeekday }, (_, index) => (
          <span key={`gap-${index}`} aria-hidden="true" />
        ))}
        {daysOfMonth(month).map((date) => {
          const count = intervalsByDate.get(date) ?? 0;
          // ALL of the day's findings, so the cell states the LOUDEST one rather
          // than whichever the server happened to list first (acceptance C2/L4).
          // The cell and the day pin then agree about the same day.
          const dayFindings = findings.filter((item) => item.date === date);
          const daySeverity = worstSeverity(dayFindings);
          const loudest =
            dayFindings.find((item) => item.severity === daySeverity) ?? dayFindings[0];
          const result = dailyByDate.get(date);
          const events = eventsByDate.get(date) ?? [];
          const structure = nonWorkingKind(date, result?.calendar_class);
          const mark = calendarStructureMarker(result?.calendar_class);
          // The accessible name is the ONLY carrier of the day's events: the bars
          // are `aria-hidden` (see lib/calendar-event.tsx), so every event's
          // spoken phrase is composed here — INCLUDING the ones the overflow chip
          // hides, which is what makes the chip an overflow treatment rather than
          // a silent truncation.
          //
          // The weekday is spelled out in full: an abbreviation is read as a
          // syllable by some screen readers, and the weekday is the one thing that
          // tells a non-visual user which columns are the weekend. The structure is
          // spelled out too, so the `H` / `X` marker is never the only statement of
          // which kind of non-working day this is.
          const label = [
            `${formatDayHeading(date, "long")} ${date.slice(0, 4)}`,
            structure ? nonWorkingWords[structure] : "ordinary day",
            count ? `${count} interval${count === 1 ? "" : "s"}` : "no work",
            result ? `delta ${minutes(result.delta_minutes)}` : null,
            ...events.map((event) => event.spoken),
            daySeverity
              ? `${daySeverity} finding: ${loudest.message}${
                  dayFindings.length > 1 ? ` (+${dayFindings.length - 1} more)` : ""
                }`
              : null,
          ]
            .filter(Boolean)
            .join(", ");
          return (
            <button
              className={`day-cell ${selectedDate === date ? "selected" : ""}`}
              key={date}
              type="button"
              aria-pressed={selectedDate === date}
              aria-label={label}
              data-nonworking={structure ?? undefined}
              onClick={() => onSelect(date)}
            >
              <span className="cell-number">
                {date.slice(8)}
                {/* The non-colour carrier for the two institutional classes, and
                 *  the only thing separating a statutory public holiday from an
                 *  employer-free day at phone density, where neither label fits.
                 *  Hidden from assistive technology because the accessible name
                 *  above already spells the classification out. */}
                {mark && (
                  <span className="cell-nonworking-mark" aria-hidden="true">
                    {mark}
                  </span>
                )}
              </span>
              <CalendarEventBars events={events} />
              {/* Shown only when the day HAS work: the delta is 0:00 for a day with
               *  no intervals, and a column of "0:00" is noise. The presence of the
               *  delta is therefore also the "something is recorded here" signal,
               *  which is why the old `.cell-dot` was a duplicate representation and
               *  is gone. */}
              {count > 0 && result && (
                <small className="cell-delta">{minutes(result.delta_minutes)}</small>
              )}
              {daySeverity && (
                <small className={`cell-finding severity-${daySeverity.toLowerCase()}`}>
                  {daySeverity}
                </small>
              )}
            </button>
          );
        })}
      </div>
    </section>
  );
}

/** The day's PLANNED operational assignments, in one section.
 *
 *  This is the structural fix the redesign exists for. The plan used to be
 *  embedded at the very bottom of the day panel — after Breaks, below the actual
 *  timeline — as a bare card plus an add button, so the day's most actionable
 *  content was the last thing on the panel. It is now a first-class section in the
 *  day's information hierarchy and the ONE place a plan is read or created.
 *
 *  What it deliberately does NOT do is imply that recording your own plan is an
 *  institutional approval. The provenance chip is keyed on the same POSITIVE test
 *  the shift card uses: `EMPLOYEE_RECORDED` earns "Entered by employee", a member
 *  of `AUTHORITATIVE_SOURCES` earns "Approved plan", and every other value —
 *  including a fixture's own `DEMO_SEED`, a future adapter's code, or an absent
 *  value — gets NO chip at all. An approval claim nobody made is the one outcome
 *  that must never happen (§6.2). */
function PlannedAssignments({
  month,
  day,
  shifts,
  ocdAssignments,
  canPlanShift,
  canPlanOcd,
  onFactsChanged,
}: {
  month: string;
  day: string;
  shifts: Shift[];
  ocdAssignments: OcdAssignment[];
  canPlanShift?: boolean;
  canPlanOcd?: boolean;
  onFactsChanged: () => void;
}) {
  const empty = shifts.length === 0 && ocdAssignments.length === 0;
  return (
    <section
      className="day-section planned-assignments"
      aria-label={`Planned assignments for ${day}`}
    >
      <h3>Planned assignments</h3>
      {empty && <p className="empty-state">No planned assignments for this day.</p>}
      {shifts.map((shift) => (
        <ShiftPlanActualCard key={shift.id} shift={shift} onFactsChanged={onFactsChanged} />
      ))}
      {ocdAssignments.map((assignment) => (
        <PlannedOcdRow key={assignment.id} assignment={assignment} month={month} />
      ))}
      <div className="planned-actions">
        {/* §6.1: offered only with `SHIFT_PLAN_ENTRY_WRITE`. With the flag off the
         *  server refuses every plan write with POLICY_BLOCKED, so rendering the
         *  control anyway would be a form that can only fail. */}
        {canPlanShift && <AddShiftPlanForm onFactsChanged={onFactsChanged} />}
        {/* A LINK, not a second copy of the form: the on-call page owns assignment
         *  creation, its validation and its capability gate, and duplicating that
         *  form here would put two divergent copies of the same controls on two
         *  surfaces. The link is offered on the same capability the page itself
         *  gates on, so the two cannot disagree about whether it is allowed. */}
        {canPlanOcd && (
          <Link className="planned-add" to="/month/$month/ocd" params={{ month }}>
            + Add on-call duty
          </Link>
        )}
      </div>
    </section>
  );
}

/** One scheduled on-call availability window, as a compact row.
 *
 *  Deliberately NOT a copy of the on-call page's own assignment card: that card
 *  owns the call list, the compensation election and the correct/cancel lifecycle,
 *  and this section shows what is PLANNED. Only a SCHEDULED assignment reaches
 *  here (the workspace filters), so the state is stated by the section itself and
 *  a badge that could only ever read "Scheduled" would be noise; the lifecycle of a
 *  cancelled or superseded window belongs to the page that owns it. */
function PlannedOcdRow({ assignment, month }: { assignment: OcdAssignment; month: string }) {
  // The two surfaces report the same fact under different keys, so read whichever
  // the payload carries (the same defensive read the on-call card makes).
  const source = assignment.plan_source ?? assignment.source;
  return (
    <div className="planned-row">
      <div className="planned-row-head">
        <h4>On-call duty</h4>
        {source === "EMPLOYEE_RECORDED" && (
          <span className="source-badge">Entered by employee</span>
        )}
        {AUTHORITATIVE_SOURCES.has(source ?? "") && (
          <span className="source-badge">Approved plan</span>
        )}
      </div>
      <dl className="planned-row-times">
        <div>
          <dt>Planned window</dt>
          <dd>{berlinRange(assignment.start_at, assignment.end_at)}</dd>
        </div>
        <div>
          <dt>Calls recorded</dt>
          <dd>{assignment.calls.length === 0 ? "None yet" : String(assignment.calls.length)}</dd>
        </div>
      </dl>
      <div className="workspace-actions">
        <Link to="/month/$month/ocd" params={{ month }}>
          Open on-call duty
        </Link>
      </div>
    </div>
  );
}

function DayPanel({
  month,
  day,
  dailyResult,
  dayStatus,
  findings,
  shifts,
  hasTemplate,
  canPreviewWeek,
  applyBusy,
  previousDayIntervalCount,
  onApplyNormalDay,
  onPreviewWeek,
  usualWorkType,
  canPlanShift,
  canPlanOcd,
  ocdAssignments,
  onFactsChanged,
}: {
  month: string;
  day: string;
  dailyResult?: DailyResult;
  dayStatus: DayStatus[];
  findings: Finding[];
  shifts: Shift[];
  /** Scheduled on-call availability for THIS day. A call is effective work and
   *  stays on the Actual work timeline; only availability is a plan (INV-10). */
  ocdAssignments: OcdAssignment[];
  hasTemplate: boolean;
  canPreviewWeek: boolean;
  applyBusy: boolean;
  previousDayIntervalCount: number;
  onApplyNormalDay: () => void;
  onPreviewWeek: () => void;
  /** `SHIFT_PLAN_ENTRY_WRITE` (§6.1). The add control is gated on the CAPABILITY,
   *  not on the page: with the flag off the server refuses every plan write with
   *  `POLICY_BLOCKED`, so rendering the control anyway would be a form that can
   *  only fail. Defaulted, because the day panel is also rendered by callers that
   *  have no identity read (and an absent capability must not mean "allowed"). */
  canPlanShift?: boolean;
  /** `OCD_PLAN_ENTRY_WRITE`. Gates only the LINK to the on-call page, which is
   *  where the real form lives and which gates itself on the same capability. */
  canPlanOcd?: boolean;
  /** A write on this day moves the facts the month view is drawn from, so the
   *  page's normal-week preview (a claim about which days are still eligible)
   *  must be dropped in the same tick rather than after a refetch (review R17). */
  onFactsChanged: () => void;
  usualWorkType: WorkType | null;
}) {
  const matchingFindings = findings.filter(
    (finding) => finding.date === day || finding.date === null,
  );
  // The panel is a landmark region, so it needs an accessible name. It takes the
  // VISIBLE heading's text rather than a written-out label, so the two can never
  // disagree as the panel's contents change.
  const panelHeadingId = useId();
  const queryClient = useQueryClient();
  const [actionNotice, setActionNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(
    null,
  );
  const copyMutation = useMutation({
    mutationFn: (command: string) => copyPreviousDay(month, day, command),
    onSuccess: () => {
      setActionNotice({ kind: "ok", text: "Yesterday’s intervals copied." });
      // Copying writes THIS day's facts, so the page's normal-week preview must
      // go in the same tick — the day is no longer eligible and no refetch is
      // needed to say so (review R6).
      onFactsChanged();
      invalidateFactDerived(queryClient);
    },
    onError: (error) => setActionNotice({ kind: "error", text: refusalText(error) }),
  });
  return (
    <section className="day-panel" aria-labelledby={panelHeadingId}>
      <div className="day-heading">
        <h2 id={panelHeadingId}>{formatDayHeading(day)}</h2>
        <span className="private-badge">Private</span>
      </div>
      <dl className="day-totals" aria-label="Server-calculated day totals">
        <div>
          <dt>Worked</dt>
          <dd>{plainMinutes(dailyResult?.worked_minutes ?? 0)}</dd>
        </div>
        <div>
          <dt>Required</dt>
          <dd>{plainMinutes(dailyResult?.required_minutes ?? 0)}</dd>
        </div>
        <div>
          <dt>Delta</dt>
          <dd>{minutes(dailyResult?.delta_minutes ?? 0)}</dd>
        </div>
        <div>
          <dt>Calendar</dt>
          <dd>{calendarClassLabel(dailyResult?.calendar_class)}</dd>
        </div>
        <div>
          <dt>Credited to your balance</dt>
          <dd>{plainMinutes(dailyResult?.account_credit_minutes ?? 0)}</dd>
        </div>
      </dl>
      {dailyResult && dailyResult.calendar_class !== "WEEKDAY" && (
        <p className="private-note">
          {dailyResult.calendar_class === "PUBLIC_HOLIDAY"
            ? "A public holiday carries no required time; your recorded work still counts as work."
            : "An employer-free day carries no required time; your recorded work still counts as work."}
        </p>
      )}
      <div className="quick-actions">
        <button
          type="button"
          className="quick-action"
          disabled={previousDayIntervalCount === 0 || copyMutation.isPending || applyBusy}
          title={
            previousDayIntervalCount === 0
              ? "The previous day has no intervals to copy."
              : undefined
          }
          onClick={() => copyMutation.mutate(crypto.randomUUID())}
        >
          Copy yesterday
        </button>
        {hasTemplate && (
          <button
            type="button"
            className="quick-action"
            disabled={applyBusy}
            onClick={onApplyNormalDay}
          >
            Apply normal day
          </button>
        )}
        {(hasTemplate && canPreviewWeek) || !hasTemplate ? (
          <OverflowMenu
            className="more-menu"
            popoverClassName="more-menu-popover"
            toggleLabel="More actions"
            toggleText="More…"
            items={(close) => (
              <>
                {hasTemplate ? (
                  <button
                    type="button"
                    disabled={applyBusy}
                    onClick={() => {
                      close();
                      onPreviewWeek();
                    }}
                  >
                    Preview normal work week
                  </button>
                ) : (
                  <Link to="/profile" onClick={close}>
                    Set up a normal-day template
                  </Link>
                )}
              </>
            )}
          />
        ) : null}
      </div>
      {actionNotice && (
        <p role="status" className={actionNotice.kind === "ok" ? "status-ok" : "status-error"}>
          {actionNotice.text}
        </p>
      )}
      {/* The sections below are the day's INFORMATION hierarchy, in the order the
       *  employee reads them: what the day IS, what was PLANNED for it, what was
       *  actually RECORDED, and finally what needs checking. Each is a plain
       *  <section> with its own heading — one panel, no nested cards — so the
       *  hierarchy is carried by the headings and by thin dividers rather than by
       *  a stack of boxes. */}
      <DayStatusBlock day={day} entries={dayStatus} findings={matchingFindings} />
      <PlannedAssignments
        month={month}
        day={day}
        shifts={shifts}
        ocdAssignments={ocdAssignments}
        canPlanShift={canPlanShift}
        canPlanOcd={canPlanOcd}
        onFactsChanged={onFactsChanged}
      />
      <section className="day-section" aria-label={`Actual work on ${day}`}>
        <h3>Actual work</h3>
        <DayTimeline
          month={month}
          day={day}
          usualWorkType={usualWorkType}
          onFactsChanged={onFactsChanged}
        />
      </section>
      <FindingPin findings={matchingFindings} />
    </section>
  );
}

/** Finding identity for list keys. Neither rule+message nor date+rule+message
 *  is unique: WT-CORE-005 fires once per offending interval with the same
 *  message, so one day can carry two byte-identical findings. The payload has
 *  no interval id to key on, and the lists are rebuilt from server data rather
 *  than reordered in place, so the position is the remaining discriminator —
 *  without it React drops or duplicates rows. */
function findingKey(finding: Finding, index: number): string {
  return `${finding.date ?? "month"}-${finding.rule_id}-${finding.message}-${index}`;
}

/** Compact severity pin near the day totals. One finding: its message.
 *  Several: a count. Expandable for full detail (spec §6). A single
 *  finding is shown fully by the summary, so the repeated detail list is
 *  hidden for it (one presentation, not two). */
/** The day's findings, as the panel's LAST section.
 *
 *  Moved from beside the day totals to the end (owner directive, 2026-09-26): the
 *  day now reads in the order summary -> day status -> planned assignments ->
 *  actual work -> things to check, so the panel leads with what the day IS and
 *  ends with what needs attention. Nothing is hidden by the move — the calendar
 *  cell still states the day's worst severity, so an unresolved finding is visible
 *  while scanning the month, and this section is where its message is read.
 *
 *  The wrapper is rendered HERE rather than at the call site so an empty section
 *  heading can never appear: with no findings the component returns null and the
 *  panel simply ends at Actual work. */
function FindingPin({ findings }: { findings: Finding[] }) {
  if (findings.length === 0) return null;
  const single = findings.length === 1;
  // The loudest severity, not the last one's: the calendar cell and the day pin
  // must give the same day the same urgency (acceptance C2).
  const tone = (worstSeverity(findings) ?? "WARNING").toLowerCase();
  return (
    <section className="day-section" aria-label="Things to check">
      <h3>Things to check</h3>
      <details className={`finding-pin${single ? " single" : ""}`}>
        <summary className={`severity-${tone}`}>
          ⚠ {single ? findings[0].message : `${findings.length} things to check`}
        </summary>
        <ul>
          {findings.map((finding, index) => (
            <li
              key={findingKey(finding, index)}
              className={`severity-${finding.severity.toLowerCase()}`}
            >
              <strong>{finding.severity}</strong> {finding.message}
            </li>
          ))}
        </ul>
      </details>
    </section>
  );
}

/** One day's day-status facts (§6.3): the current heads with an add control
 *  and per-entry correct/remove. These are employee-declared facts; the
 *  requirement and the findings they produce are the server's arithmetic
 *  (ARCH-MOD-004 — the frontend never reproduces a calculation rule). */
function DayStatusBlock({
  day,
  entries,
  findings,
}: {
  day: string;
  entries: DayStatus[];
  findings: Finding[];
}) {
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const saveMutation = useMutation({
    mutationFn: createDayStatus,
    onSuccess: () => {
      setAdding(false);
      setNotice({ kind: "ok", text: "Day status saved." });
      invalidateFactDerived(queryClient);
    },
    onError: (error) => setNotice({ kind: "error", text: refusalText(error) }),
  });
  const correctMutation = useMutation({
    mutationFn: ({
      id,
      ...changes
    }: {
      id: string;
      version: number;
      category?: DayStatusCategory;
      portion?: DayStatusPortion;
      day_part?: DayStatusDayPart;
    }) => patchDayStatus(id, changes),
    onSuccess: () => {
      setEditingId(null);
      setNotice({ kind: "ok", text: "Day status updated." });
      invalidateFactDerived(queryClient);
    },
    onError: (error) => setNotice({ kind: "error", text: refusalText(error) }),
  });
  const removeMutation = useMutation({
    mutationFn: ({ id, version }: { id: string; version: number }) => deleteDayStatus(id, version),
    onSuccess: () => {
      setEditingId(null);
      setNotice({ kind: "ok", text: "Day status removed." });
      invalidateFactDerived(queryClient);
    },
    onError: (error) => setNotice({ kind: "error", text: refusalText(error) }),
  });
  const pending = saveMutation.isPending || correctMutation.isPending || removeMutation.isPending;
  // The message stays in the finding pin above; here the rule is named in
  // words. The engine code behind that name stays in the API payload and on
  // /admin — it is never rendered on this surface.
  const statusFindings = findings.filter((finding) => finding.rule_id.startsWith("WT-STATUS-"));
  return (
    <section className="day-section day-status-block" aria-label={`Day status for ${day}`}>
      <h3>Day status</h3>
      {entries.length === 0 ? (
        <p>No day status for this day.</p>
      ) : (
        <ul className="day-status-list">
          {entries.map((entry) => (
            <li key={entry.id}>
              <span className="day-status-label">{dayStatusDescription(entry)}</span>
              <button
                type="button"
                aria-label={`Correct ${dayStatusDescription(entry)}`}
                disabled={pending}
                onClick={() => {
                  setAdding(false);
                  setEditingId(entry.id);
                }}
              >
                Correct
              </button>
              <ConfirmAction
                label="Remove"
                confirmLabel="Remove day status"
                className="menu-danger"
                confirmClassName="menu-danger"
                ariaLabel={`Remove ${dayStatusDescription(entry)}`}
                disabled={pending}
                busy={removeMutation.isPending}
                onConfirm={() => removeMutation.mutate({ id: entry.id, version: entry.version })}
              />
              {editingId === entry.id && (
                <DayStatusForm
                  key={`${entry.id}-${entry.version}`}
                  initial={entry}
                  disabled={pending}
                  onCancel={() => setEditingId(null)}
                  onSave={(values) =>
                    correctMutation.mutate({
                      id: entry.id,
                      version: entry.version,
                      ...dayStatusChanges(entry, values),
                    })
                  }
                />
              )}
            </li>
          ))}
        </ul>
      )}
      {statusFindings.length > 0 && (
        <ul className="day-status-findings" aria-label="Day status findings">
          {statusFindings.map((finding) => (
            <li key={finding.rule_id} className={`severity-${finding.severity.toLowerCase()}`}>
              {dayStatusRuleLabel(finding.rule_id)}
            </li>
          ))}
        </ul>
      )}
      {adding ? (
        <DayStatusForm
          disabled={pending}
          onCancel={() => setAdding(false)}
          onSave={(values) => saveMutation.mutate({ day, ...values })}
        />
      ) : (
        <button
          type="button"
          className="add-day-status"
          disabled={pending}
          onClick={() => {
            setEditingId(null);
            setAdding(true);
          }}
        >
          + Add day status
        </button>
      )}
      {notice && (
        <p role="status" className={notice.kind === "ok" ? "status-ok" : "status-error"}>
          {notice.text}
        </p>
      )}
    </section>
  );
}

type DayStatusValues = {
  category: DayStatusCategory;
  portion: DayStatusPortion;
  day_part: DayStatusDayPart;
};

/** Presence-based correction: only what actually changed is sent, so an
 *  untouched field keeps its value (EINV-12). */
function dayStatusChanges(entry: DayStatus, values: DayStatusValues): Partial<DayStatusValues> {
  const changes: Partial<DayStatusValues> = {};
  if (values.category !== entry.category) changes.category = values.category;
  if (values.portion !== entry.portion) changes.portion = values.portion;
  if (values.day_part !== entry.day_part) changes.day_part = values.day_part;
  return changes;
}

/** Shared create/correct form. `FULL`/`HALF` are the only extents, and
 *  `MORNING`/`AFTERNOON` is only meaningful for a `HALF` day (the server
 *  refuses any other pairing). Half-day sickness is offered **disabled** with
 *  its reason, so the BLOCKED_POLICY refusal (ABSENCE-008) is visible before the
 *  request rather than only as an error afterwards.
 *
 *  7B: the "what your own facts do" sentence used to sit above the entry list,
 *  so it re-rendered on every day selection. It now lives in this form — the
 *  one place it is actually needed, which is when a category is being chosen. */
function DayStatusForm({
  initial,
  disabled,
  onSave,
  onCancel,
}: {
  initial?: DayStatus;
  disabled: boolean;
  onSave: (values: DayStatusValues) => void;
  onCancel: () => void;
}) {
  // UI-031: opening the form used to arrive with `Vacation` already selected and
  // `Add` enabled, so one click recorded a sensitive absence fact the user never
  // chose. The category starts empty; `Full day` stays the extent default.
  const [category, setCategory] = useState<DayStatusCategory | "">(initial?.category ?? "");
  const [portion, setPortion] = useState<DayStatusPortion>(initial?.portion ?? "FULL");
  const [dayPart, setDayPart] = useState<DayStatusDayPart>(initial?.day_part ?? "ALL");
  const halfSickBlocked = category === "SICK";
  const unchanged = Boolean(
    initial &&
    initial.category === category &&
    initial.portion === portion &&
    initial.day_part === dayPart,
  );
  const chooseCategory = (next: DayStatusCategory | "") => {
    setCategory(next);
    // Half-day sickness is blocked policy: keep the form on a combination the
    // server can actually record instead of leaving a disabled choice selected.
    if (next === "SICK" && portion === "HALF") {
      setPortion("FULL");
      setDayPart("ALL");
    }
  };
  const choosePortion = (next: DayStatusPortion) => {
    setPortion(next);
    setDayPart(next === "HALF" ? (dayPart === "ALL" ? "MORNING" : dayPart) : "ALL");
  };
  return (
    <fieldset className="day-status-form">
      <legend>{initial ? "Correct day status" : "Add day status"}</legend>
      <p className="private-note">
        A vacation or sick day reduces the day’s requirement; a compensation day does not.
      </p>
      <label>
        Day status{" "}
        <select
          aria-label="Day status category"
          value={category}
          onChange={(event) => chooseCategory(event.target.value as DayStatusCategory | "")}
          disabled={disabled}
        >
          <option value="" disabled>
            Choose…
          </option>
          {dayStatusCategories.map((value) => (
            <option key={value} value={value}>
              {dayStatusCategoryLabel(value)}
            </option>
          ))}
        </select>
      </label>
      <label>
        Extent{" "}
        <select
          aria-label="Day status extent"
          value={portion}
          onChange={(event) => choosePortion(event.target.value as DayStatusPortion)}
          disabled={disabled}
        >
          <option value="FULL">Full day</option>
          <option value="HALF" disabled={halfSickBlocked}>
            Half day
          </option>
        </select>
      </label>
      {portion === "HALF" && (
        <label>
          Half of the day{" "}
          <select
            aria-label="Half of the day"
            value={dayPart}
            onChange={(event) => setDayPart(event.target.value as DayStatusDayPart)}
            disabled={disabled}
          >
            <option value="MORNING">Morning</option>
            <option value="AFTERNOON">Afternoon</option>
          </select>
        </label>
      )}
      {halfSickBlocked && (
        <p className="private-note" role="note">
          How a half sick day counts is still being confirmed — only a full sick day can be
          recorded.
        </p>
      )}
      <div className="editor-actions">
        <button type="button" onClick={onCancel} disabled={disabled}>
          Cancel
        </button>
        <button
          type="button"
          className="primary"
          onClick={() => {
            // Guarded rather than asserted: the button is disabled until a
            // category is chosen, so this only satisfies the type.
            if (category === "") return;
            onSave({ category, portion, day_part: dayPart });
          }}
          disabled={disabled || unchanged || category === ""}
        >
          {initial ? "Save" : "Add"}
        </button>
      </div>
    </fieldset>
  );
}

export function FindingList({
  findings,
  month,
  headingLevel = 3,
}: {
  findings: Finding[];
  /** UI-046: a finding that names a day links to that day's panel, so the review
   *  surface can be acted on instead of only read. */
  month: string;
  /** UI-053: the month workspace nests this under its own <h2>; the review page
   *  renders it directly under the page <h1>, where an <h3> skipped a level. */
  headingLevel?: 2 | 3;
}) {
  const Heading = `h${headingLevel}` as "h2" | "h3";
  return (
    <section className="finding-list" aria-label="Things to check">
      <Heading>Things to check</Heading>
      <p className="private-note">Only you see these — nothing is sent to HR.</p>
      {findings.length ? (
        <ul>
          {findings.map((finding, index) => (
            <li
              key={findingKey(finding, index)}
              className={`severity-${finding.severity.toLowerCase()}`}
            >
              <strong>{finding.severity}</strong>{" "}
              {finding.date ? (
                <>
                  <Link to="/month/$month" params={{ month }} search={{ day: finding.date }}>
                    {formatDayHeading(finding.date)}
                  </Link>{" "}
                </>
              ) : null}
              {finding.message}
            </li>
          ))}
        </ul>
      ) : (
        <p>Nothing to check.</p>
      )}
    </section>
  );
}

/** UI-012: a day-editor notice states its own kind, so the role and the
 *  ok/error styling never depend on sniffing the message text — and `scope`
 *  is which list produced it, because the message belongs beside that editor
 *  rather than once at the bottom of the panel. */
type DayNotice = { kind: "ok" | "error"; scope: "interval" | "break"; text: string };

function NoticeLine({ notice, scope }: { notice: DayNotice | null; scope: DayNotice["scope"] }) {
  if (!notice || notice.scope !== scope) return null;
  return (
    <p
      // A rejected save is the one message a reader must not miss, so it is
      // announced assertively; a confirmation stays polite.
      role={notice.kind === "error" ? "alert" : "status"}
      className={notice.kind === "ok" ? "status-ok" : "status-error"}
    >
      {notice.text}
    </p>
  );
}

export function DayTimeline({
  month,
  day,
  usualWorkType,
  onFactsChanged,
}: {
  month: string;
  day: string;
  usualWorkType: WorkType | null;
  onFactsChanged: () => void;
}) {
  const queryClient = useQueryClient();
  const dayQuery = useQuery({ queryKey: ["day", month, day], queryFn: () => fetchDay(month, day) });
  // A recorded OCD call and the work interval it creates are the SAME fact, but
  // the interval carries the widened span (call ± required travel, OCD-006) and
  // lost its provenance on screen: the row said "OCD call 02:00–05:00" with no
  // sign of the call it came from (acceptance G3).
  //
  // The link is already persisted AND already exposed by the API
  // (`OcdCall.linked_interval_id`), so this is a presentation join. It reuses the
  // OCD page's own key and queryFn, so the two surfaces share one cache entry and
  // visiting both costs nothing extra.
  //
  // ceiling: a presentation join, not a second truth. If the join ever needs to
  // be authoritative it belongs in the read model, not the client.
  const { from: ocdFrom, to: ocdTo } = monthRange(month);
  const ocdQuery = useQuery({ queryKey: ["ocd", month], queryFn: () => fetchOcd(ocdFrom, ocdTo) });
  const callsByInterval = useMemo(() => {
    const byInterval = new Map<string, OcdCall>();
    const assignments = Array.isArray(ocdQuery.data) ? ocdQuery.data : [];
    for (const assignment of assignments) {
      for (const call of assignment.calls) {
        // Matching is by id, so an employee-edited interval still resolves; a
        // call with no link simply contributes nothing.
        if (call.linked_interval_id) byInterval.set(call.linked_interval_id, call);
      }
    }
    return byInterval;
  }, [ocdQuery.data]);
  // UI-012: the notice carries its own kind. It used to be a bare string whose
  // styling was chosen by `message.includes("saved")`, and every notice rendered
  // once at the bottom of the panel — past the Breaks section, away from the
  // editor that produced it.
  const [notice, setNotice] = useState<DayNotice | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [splitRequestId, setSplitRequestId] = useState<string | null>(null);
  const [editingBreakId, setEditingBreakId] = useState<string | null>(null);
  const saveMutation = useMutation({
    mutationFn: createInterval,
    onSuccess: () => {
      setNotice({ kind: "ok", scope: "interval", text: "Interval saved." });
      onFactsChanged();
      invalidateFactDerived(queryClient);
    },
    onError: (error) => setNotice({ kind: "error", scope: "interval", text: refusalText(error) }),
  });
  const updateMutation = useMutation({
    mutationFn: ({ id, ...payload }: Parameters<typeof updateInterval>[1] & { id: string }) =>
      updateInterval(id, payload),
    onSuccess: () => {
      setNotice({ kind: "ok", scope: "interval", text: "Interval updated." });
      setEditingId(null);
      setSplitRequestId(null);
      onFactsChanged();
      invalidateFactDerived(queryClient);
    },
    onError: (error) => setNotice({ kind: "error", scope: "interval", text: refusalText(error) }),
  });
  const removeMutation = useMutation({
    mutationFn: ({ id, version }: { id: string; version: number }) => deleteInterval(id, version),
    onSuccess: () => {
      setNotice({ kind: "ok", scope: "interval", text: "Interval deleted." });
      setEditingId(null);
      setSplitRequestId(null);
      onFactsChanged();
      invalidateFactDerived(queryClient);
    },
    onError: (error) => setNotice({ kind: "error", scope: "interval", text: refusalText(error) }),
  });
  const splitMutation = useMutation({
    mutationFn: ({
      id,
      version,
      splitAt,
      fold,
    }: {
      id: string;
      version: number;
      splitAt: string;
      fold?: 0 | 1;
    }) => splitInterval(id, version, splitAt, fold),
    onSuccess: () => {
      setNotice({ kind: "ok", scope: "interval", text: "Interval split." });
      setEditingId(null);
      setSplitRequestId(null);
      onFactsChanged();
      invalidateFactDerived(queryClient);
    },
    onError: (error) => setNotice({ kind: "error", scope: "interval", text: refusalText(error) }),
  });
  const breakSaveMutation = useMutation({
    mutationFn: createBreak,
    onSuccess: () => {
      setNotice({ kind: "ok", scope: "break", text: "Break saved." });
      onFactsChanged();
      invalidateFactDerived(queryClient);
    },
    onError: (error) => setNotice({ kind: "error", scope: "break", text: refusalText(error) }),
  });
  const breakRemoveMutation = useMutation({
    mutationFn: ({ id, version }: { id: string; version: number }) => deleteBreak(id, version),
    onSuccess: () => {
      setNotice({ kind: "ok", scope: "break", text: "Break deleted." });
      onFactsChanged();
      invalidateFactDerived(queryClient);
    },
    onError: (error) => setNotice({ kind: "error", scope: "break", text: refusalText(error) }),
  });
  const breakUpdateMutation = useMutation({
    mutationFn: ({ id, ...payload }: Parameters<typeof patchBreak>[1] & { id: string }) =>
      patchBreak(id, payload),
    onSuccess: () => {
      // The PATCH mints a NEW head id + version; the day is refetched so the
      // row reads that head and never reuses the superseded id.
      setNotice({ kind: "ok", scope: "break", text: "Break updated." });
      setEditingBreakId(null);
      onFactsChanged();
      invalidateFactDerived(queryClient);
    },
    onError: (error) => setNotice({ kind: "error", scope: "break", text: refusalText(error) }),
  });
  if (dayQuery.isPending) return <p role="status">Loading timeline…</p>;
  if (dayQuery.isError) return <p className="status-error">Could not load this timeline.</p>;
  // Defensive against a non-list payload, the same guard the month, shifts, day
  // status and on-call reads already apply — and here it is not cosmetic: an
  // unguarded spread of `undefined` threw inside the render, which took the WHOLE
  // page down to the error boundary rather than degrading one section. A stale
  // fixture, a misbehaving intermediary or a changed response shape must cost the
  // employee their interval list, not the entire month workspace.
  const intervals = [
    ...(Array.isArray(dayQuery.data.intervals) ? dayQuery.data.intervals : []),
  ].sort((a, b) => a.start_at.localeCompare(b.start_at));
  const breaks = [...(Array.isArray(dayQuery.data.breaks) ? dayQuery.data.breaks : [])].sort(
    (a, b) => a.start_at.localeCompare(b.start_at),
  );
  const pending =
    saveMutation.isPending ||
    updateMutation.isPending ||
    removeMutation.isPending ||
    splitMutation.isPending ||
    breakSaveMutation.isPending ||
    breakRemoveMutation.isPending ||
    breakUpdateMutation.isPending;
  const latest = intervals.at(-1);
  // Keep the local DATE as well as the clock time.  A latest cross-midnight
  // interval ends on the following date, and an Add default must remain that
  // exact factual boundary rather than silently moving it back to `day`.
  const latestEndLocal = latest ? latest.end_local.slice(0, 16) : null;
  const latestWorkType = latest?.work_type ?? usualWorkType;
  const rows: ReactNode[] = [];
  intervals.forEach((interval, index) => {
    if (index > 0) {
      const label = breakLabel(intervals[index - 1], interval);
      if (label)
        rows.push(
          <li key={`gap-${interval.id}`} className="break-gap" aria-label="Break between intervals">
            {label}
          </li>,
        );
    }
    rows.push(
      <li key={interval.id} className="interval-item">
        <IntervalRow
          interval={interval}
          day={day}
          ocdCall={callsByInterval.get(interval.id) ?? null}
          disabled={pending}
          onEdit={() => setEditingId(interval.id)}
          onDuplicate={() => saveMutation.mutate(duplicateAfter(interval))}
          onDelete={() => removeMutation.mutate({ id: interval.id, version: interval.version })}
          onSplit={() => {
            setEditingId(interval.id);
            setSplitRequestId(interval.id);
          }}
        />
        {editingId === interval.id && (
          <IntervalEditor
            key={`edit-${interval.id}-${interval.version}`}
            interval={interval}
            disabled={pending}
            initialSplit={splitRequestId === interval.id}
            onSave={(payload) =>
              updateMutation.mutate({ id: interval.id, version: interval.version, ...payload })
            }
            onClose={() => {
              setEditingId(null);
              setSplitRequestId(null);
            }}
            onSplit={(splitAt, fold) =>
              splitMutation.mutate({ id: interval.id, version: interval.version, splitAt, fold })
            }
          />
        )}
      </li>,
    );
  });
  rows.push(
    <li key="add" className="add-interval-item">
      <AddIntervalForm
        day={day}
        disabled={pending}
        defaultStartLocal={latestEndLocal}
        defaultWorkType={latestWorkType}
        onSave={(payload, onSuccess) => saveMutation.mutate(payload, { onSuccess })}
      />
    </li>,
  );
  // Explicit factual breaks: their own list, never interleaved with work rows —
  // a break is not work and only an UNPAID one reduces worked time (§5.3).
  const breakRows = breaks.map((item) => (
    <li key={item.id} className="interval-item">
      <BreakRow
        item={item}
        disabled={pending}
        onEdit={() => setEditingBreakId(item.id)}
        onDelete={() => breakRemoveMutation.mutate({ id: item.id, version: item.version })}
      />
      {editingBreakId === item.id && (
        <BreakEditor
          key={`break-edit-${item.id}-${item.version}`}
          item={item}
          disabled={pending}
          onSave={(payload) =>
            breakUpdateMutation.mutate({ id: item.id, version: item.version, ...payload })
          }
          onClose={() => setEditingBreakId(null)}
        />
      )}
    </li>
  ));
  return (
    <div className="timeline">
      {/* h4, not h3: these are subsections of the "Actual work" section that
       *  contains them. As h3s they were siblings of their own parent heading. */}
      <h4>Actual intervals</h4>
      <ol className="interval-list">{rows}</ol>
      <NoticeLine notice={notice} scope="interval" />
      <h4>Breaks</h4>
      {breakRows.length > 0 && <ol className="interval-list">{breakRows}</ol>}
      <AddBreakForm
        day={day}
        disabled={pending}
        onSave={(payload, onSuccess) => breakSaveMutation.mutate(payload, { onSuccess })}
      />
      <NoticeLine notice={notice} scope="break" />
    </div>
  );
}

/** Compact normal-state row: start–end, work type, duration, note indicator,
 *  and a secondary-actions menu. The whole row opens the edit state; full
 *  editing controls are never shown permanently (spec §1–§2).
 *  The actions expand INLINE inside the row (never a floating popover), so
 *  the menu can never occlude the next row's controls. */
function IntervalRow({
  interval,
  day,
  ocdCall,
  disabled,
  onEdit,
  onDuplicate,
  onDelete,
  onSplit,
}: {
  interval: Interval;
  day: string;
  /** The recorded call this interval was created from, when there is one. */
  ocdCall: OcdCall | null;
  disabled: boolean;
  onEdit: () => void;
  onDuplicate: () => void;
  onDelete: () => void;
  onSplit: () => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!menuOpen) return;
    const onPointerDown = (event: MouseEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setMenuOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [menuOpen]);
  const start = localParts(interval.start_local).time;
  const end = localParts(interval.end_local).time;
  const label =
    workTypes.find(([value]) => value === interval.work_type)?.[1] ?? interval.work_type;
  const startDate = interval.start_local.slice(0, 10);
  const endDate = interval.end_local.slice(0, 10);
  const crossesMidnight = endDate !== startDate;
  const totalMinutes = Math.round(interval.elapsed_seconds / 60);
  const dayMinutes = dayPortionMinutes(interval, day);
  // The call's own span and its travel legs, in the interval's own row — the
  // interval's span is the widened one, so without this the row cannot explain
  // where its extra minutes came from. Text only: a link here would nest
  // interactive content inside the summary button, which is invalid and would
  // break the row's own click target.
  const callProvenance = ocdCall
    ? `call ${berlinTime(ocdCall.call_start_at)}–${berlinTime(ocdCall.call_end_at)}${
        ocdCall.travel_out_minutes != null || ocdCall.travel_back_minutes != null
          ? ` · travel ${ocdCall.travel_out_minutes ?? 0} + ${ocdCall.travel_back_minutes ?? 0} min`
          : ""
      }`
    : null;
  return (
    <div className={`interval-row${menuOpen ? " row-actions-open" : ""}`} ref={root}>
      <button
        type="button"
        className="interval-summary"
        // The provenance is part of the control's ACCESSIBLE NAME, not only its
        // visible text: an explicit `aria-label` replaces all descendant text for
        // assistive technology, so a screen-reader user would otherwise never hear
        // where the widened span came from (acceptance G3, review R4).
        aria-label={`Edit interval ${start}–${end}${callProvenance ? ` · ${callProvenance}` : ""}`}
        onClick={onEdit}
      >
        <span className="interval-time">
          {start}–{end}
          {crossesMidnight && <span className="next-day"> · ends {shortDayLabel(endDate)}</span>}
        </span>
        <span className="interval-type">{label}</span>
        <span className="interval-duration">
          {crossesMidnight
            ? `${plainMinutes(dayMinutes)} on this day · ${plainMinutes(totalMinutes)} total`
            : plainMinutes(totalMinutes)}
        </span>
        {callProvenance ? (
          <span className="interval-note interval-provenance">{callProvenance}</span>
        ) : null}
        {interval.comment ? <span className="interval-note">{interval.comment}</span> : null}
      </button>
      <button
        type="button"
        className="menu-toggle row-menu-toggle"
        aria-label={`Interval actions for ${start}–${end}`}
        aria-expanded={menuOpen}
        onClick={() => setMenuOpen((value) => !value)}
      >
        ⋮
      </button>
      {menuOpen && (
        <div className="row-menu-inline">
          <button
            type="button"
            onClick={() => {
              setMenuOpen(false);
              onDuplicate();
            }}
            disabled={disabled}
          >
            Duplicate
          </button>
          <button
            type="button"
            onClick={() => {
              setMenuOpen(false);
              onSplit();
            }}
            disabled={disabled}
          >
            Split
          </button>
          <ConfirmAction
            label="Delete"
            confirmLabel="Delete interval"
            className="menu-danger"
            confirmClassName="menu-danger"
            disabled={disabled}
            onConfirm={() => {
              setMenuOpen(false);
              onDelete();
            }}
          />
        </div>
      )}
    </div>
  );
}

/** Break row: start–end, kind, duration and a secondary-actions menu holding
 *  Delete. The whole row opens the correction editor, mirroring the work
 *  interval rows — a break already covered by a disclosure cannot be deleted,
 *  so correction is the employee's only path to fix it. */
function BreakRow({
  item,
  disabled,
  onEdit,
  onDelete,
}: {
  item: BreakInterval;
  disabled: boolean;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!menuOpen) return;
    const onPointerDown = (event: MouseEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setMenuOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [menuOpen]);
  const start = localParts(item.start_local).time;
  const end = localParts(item.end_local).time;
  const endDate = item.end_local.slice(0, 10);
  const crossesMidnight = endDate !== item.start_local.slice(0, 10);
  return (
    <div className={`interval-row${menuOpen ? " row-actions-open" : ""}`} ref={root}>
      <button
        type="button"
        className="interval-summary"
        aria-label={`Edit break ${start}–${end}`}
        onClick={onEdit}
      >
        <span className="interval-time">
          {start}–{end}
          {crossesMidnight && <span className="next-day"> · ends {shortDayLabel(endDate)}</span>}
        </span>
        <span className="interval-type">{breakKindLabel(item.kind)}</span>
        <span className="interval-duration">
          {plainMinutes(Math.round(item.elapsed_seconds / 60))}
        </span>
      </button>
      <button
        type="button"
        className="menu-toggle row-menu-toggle"
        aria-label={`Break actions for ${start}–${end}`}
        aria-expanded={menuOpen}
        onClick={() => setMenuOpen((value) => !value)}
      >
        ⋮
      </button>
      {menuOpen && (
        <div className="row-menu-inline">
          <ConfirmAction
            label="Delete"
            confirmLabel="Delete break"
            className="menu-danger"
            confirmClassName="menu-danger"
            disabled={disabled}
            onConfirm={() => {
              setMenuOpen(false);
              onDelete();
            }}
          />
        </div>
      )}
    </div>
  );
}

/** True when the given Berlin wall time falls in the ambiguous fall-back
 *  hour (both DST occurrences show the same wall clock). */
function isAmbiguousBerlin(wallLocal: string): boolean {
  const wallAsUtc = Date.parse(`${wallLocal.slice(0, 16)}:00Z`);
  const wall = wallLocal.slice(11, 16);
  const hourAt = (ms: number) =>
    new Intl.DateTimeFormat("en-GB", {
      timeZone: "Europe/Berlin",
      hour: "2-digit",
      hourCycle: "h23",
      minute: "2-digit",
    }).format(new Date(ms));
  // Both the CEST (+02:00) and CET (+01:00) interpretations render the same
  // wall clock exactly when the time is ambiguous.
  return hourAt(wallAsUtc - 7_200_000) === wall && hourAt(wallAsUtc - 3_600_000) === wall;
}

/** First/second-occurrence chooser shown only in the ambiguous fall-back
 *  hour. Without it the server would reject the wall time (docs/ARCHITECTURE.md). */
function OccurrenceChoice({
  wallLocal,
  value,
  onChange,
  disabled,
}: {
  wallLocal: string | null;
  value: 0 | 1;
  onChange: (fold: 0 | 1) => void;
  disabled: boolean;
}) {
  // Need a fully-formed "YYYY-MM-DDTHH:MM" wall time; partially typed or
  // empty inputs are not yet determinable (and must not crash the DST probe).
  const time = wallLocal?.slice(11, 16) ?? "";
  if (
    !wallLocal ||
    !/^\d{2}:\d{2}$/.test(time) ||
    Number.isNaN(Date.parse(`${wallLocal}:00Z`)) ||
    !isAmbiguousBerlin(wallLocal)
  )
    return null;
  return (
    <label className="fold-choice">
      Occurrence{" "}
      <select
        value={value}
        onChange={(event) => onChange(event.target.value === "1" ? 1 : 0)}
        disabled={disabled}
      >
        <option value={0}>First — summer time (CEST)</option>
        <option value={1}>Second — winter time (CET)</option>
      </select>
    </label>
  );
}

/** Focused edit state, opened on demand. Notes are collapsed by default;
 *  Duplicate/Split/Delete stay behind the row's action menu; Cancel both
 *  discards unsaved edits and closes (spec §2). */
function IntervalEditor({
  interval,
  disabled,
  initialSplit,
  onSave,
  onClose,
  onSplit,
}: {
  interval: Interval;
  disabled: boolean;
  initialSplit: boolean;
  onSave: (payload: {
    start_local: string;
    end_local: string;
    work_type: WorkType;
    comment?: string | null;
    start_fold?: 0 | 1;
    end_fold?: 0 | 1;
  }) => void;
  onClose: () => void;
  onSplit: (splitAt: string, fold?: 0 | 1) => void;
}) {
  // Escape closes the editor even when focus is still on the row button that
  // opened it (Enter-to-edit then immediately Escape).
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);
  const start = localParts(interval.start_local);
  const end = localParts(interval.end_local);
  const crossesMidnight = end.date !== start.date;
  const starterFold = foldFor(interval.start_at, interval.start_local);
  const enderFold = foldFor(interval.end_at, interval.end_local);
  const [startTime, setStartTime] = useState(start.time);
  const [endTime, setEndTime] = useState(end.time);
  const [endNextDay, setEndNextDay] = useState(crossesMidnight);
  const [workType, setWorkType] = useState<WorkType>(interval.work_type);
  const [comment, setComment] = useState(interval.comment ?? "");
  const [noteOpen, setNoteOpen] = useState(Boolean(interval.comment));
  const [splitAt, setSplitAt] = useState("");
  const splitOpen = initialSplit;
  const [startFold, setStartFold] = useState<0 | 1>(starterFold);
  const [endFold, setEndFold] = useState<0 | 1>(enderFold);
  const [splitFold, setSplitFold] = useState<0 | 1>(enderFold);
  const startWall = `${start.date}T${startTime}:00`;
  const endWall = `${endNextDay ? nextDate(start.date) : start.date}T${endTime}:00`;
  const orderError = intervalOrderError(startTime, endTime, endNextDay);
  // The stored instants fix the DST occurrences of the CURRENT boundaries;
  // the occurrence choosers let the user disambiguate edited fall-back
  // times (docs/ARCHITECTURE.md — never silently guess an ambiguous input).
  const payload = () => ({
    start_local: startWall,
    end_local: endWall,
    start_fold: startFold,
    end_fold: endFold,
    work_type: workType,
    comment: comment || null,
  });
  // A split time after midnight on a cross-midnight interval belongs to the
  // second day whenever it is earlier than (or at) the start time.
  const splitDate = (time: string) =>
    `${crossesMidnight && time <= start.time ? nextDate(start.date) : start.date}T${time}:00`;
  return (
    <fieldset className="interval-editor">
      <legend>Edit interval</legend>
      <div className="editor-fields">
        <label>
          Start{" "}
          <TimeField
            aria-label="Start time"
            value={startTime}
            onChange={setStartTime}
            disabled={disabled}
          />
        </label>
        <OccurrenceChoice
          wallLocal={startWall}
          value={startFold}
          onChange={setStartFold}
          disabled={disabled}
        />
        <label>
          End{" "}
          <TimeField
            aria-label="End time"
            value={endTime}
            onChange={setEndTime}
            disabled={disabled}
            aria-invalid={orderError ? true : undefined}
            aria-describedby={orderError ? "interval-order-error" : undefined}
          />
        </label>
        {orderError && (
          <p className="field-error" id="interval-order-error">
            {orderError}
          </p>
        )}
        <OccurrenceChoice
          wallLocal={endWall}
          value={endFold}
          onChange={setEndFold}
          disabled={disabled}
        />
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={endNextDay}
            onChange={(event) => setEndNextDay(event.target.checked)}
            disabled={disabled}
          />{" "}
          Ends next day
        </label>
      </div>
      <label>
        Work type{" "}
        <select
          aria-label="Work type"
          value={workType}
          onChange={(event) => setWorkType(event.target.value as WorkType)}
          disabled={disabled}
        >
          {workTypes.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </label>
      {endNextDay && (
        <p className="cross-midnight">
          Starts on {start.date}; ends on {nextDate(start.date)} — elapsed time is calculated by the
          server.
        </p>
      )}
      {noteOpen ? (
        <label className="note-field">
          Note{" "}
          <textarea
            aria-label="Note"
            rows={2}
            value={comment}
            onChange={(event) => setComment(event.target.value)}
            disabled={disabled}
          />
        </label>
      ) : (
        <button
          type="button"
          className="note-toggle"
          onClick={() => setNoteOpen(true)}
          disabled={disabled}
        >
          Add note…
        </button>
      )}
      {splitOpen && (
        <div className="split-controls">
          <label>
            Split at{" "}
            <TimeField
              aria-label="Split interval at"
              value={splitAt}
              onChange={setSplitAt}
              disabled={disabled}
            />
          </label>
          <OccurrenceChoice
            wallLocal={splitAt ? splitDate(splitAt) : null}
            value={splitFold}
            onChange={setSplitFold}
            disabled={disabled}
          />
          <button
            type="button"
            disabled={disabled || !isValidTime(splitAt)}
            onClick={() => onSplit(splitDate(splitAt), splitFold)}
          >
            Split here
          </button>
        </div>
      )}
      <div className="editor-actions">
        <button type="button" onClick={onClose} disabled={disabled}>
          Cancel
        </button>
        <button
          type="button"
          className="primary"
          onClick={() => onSave(payload())}
          disabled={
            disabled || !isValidTime(startTime) || !isValidTime(endTime) || orderError !== null
          }
        >
          Save
        </button>
      </div>
    </fieldset>
  );
}

/** Focused correction state for an explicit break, opened on demand like the
 *  interval editor. `end_local` carries the following date when "Ends next
 *  day" is checked, so a cross-midnight break stays a forward span. */
function BreakEditor({
  item,
  disabled,
  onSave,
  onClose,
}: {
  item: BreakInterval;
  disabled: boolean;
  onSave: (payload: { start_local: string; end_local: string; kind: BreakKind }) => void;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);
  const start = localParts(item.start_local);
  const end = localParts(item.end_local);
  const [startTime, setStartTime] = useState(start.time);
  const [endTime, setEndTime] = useState(end.time);
  const [endNextDay, setEndNextDay] = useState(end.date !== start.date);
  const [kind, setKind] = useState<BreakKind>(item.kind);
  const startWall = `${start.date}T${startTime}:00`;
  const endWall = `${endNextDay ? nextDate(start.date) : start.date}T${endTime}:00`;
  const orderError = intervalOrderError(startTime, endTime, endNextDay);
  return (
    <fieldset className="interval-editor">
      <legend>Edit break</legend>
      <div className="editor-fields">
        <label>
          Start{" "}
          <TimeField
            aria-label="Break start time"
            value={startTime}
            onChange={setStartTime}
            disabled={disabled}
          />
        </label>
        <label>
          End{" "}
          <TimeField
            aria-label="Break end time"
            value={endTime}
            onChange={setEndTime}
            disabled={disabled}
            aria-invalid={orderError ? true : undefined}
            aria-describedby={orderError ? "break-order-error" : undefined}
          />
        </label>
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={endNextDay}
            onChange={(event) => setEndNextDay(event.target.checked)}
            disabled={disabled}
          />{" "}
          Ends next day
        </label>
        {orderError && (
          <p className="field-error" id="break-order-error">
            {orderError}
          </p>
        )}
      </div>
      <label>
        Kind{" "}
        <select
          aria-label="Break kind"
          value={kind}
          onChange={(event) => setKind(event.target.value as BreakKind)}
          disabled={disabled}
        >
          {breakKinds.map((value) => (
            <option key={value} value={value}>
              {breakKindLabels[value]}
            </option>
          ))}
        </select>
      </label>
      <div className="editor-actions">
        <button type="button" onClick={onClose} disabled={disabled}>
          Cancel
        </button>
        <button
          type="button"
          className="primary"
          onClick={() => onSave({ start_local: startWall, end_local: endWall, kind })}
          disabled={
            disabled || !isValidTime(startTime) || !isValidTime(endTime) || orderError !== null
          }
        >
          Save
        </button>
      </div>
    </fieldset>
  );
}

/** Toggle + compact creation form with safe deterministic defaults: the new
 *  start defaults to the end of the latest interval, work type follows the
 *  day's usual type — but no interval is created without explicit
 *  confirmation, and the end time is never invented (spec §3). */
function AddIntervalForm({
  day,
  disabled,
  defaultStartLocal,
  defaultWorkType,
  onSave,
}: {
  day: string;
  disabled: boolean;
  defaultStartLocal: string | null;
  defaultWorkType: WorkType | null;
  onSave: (
    payload: {
      start_local: string;
      end_local: string;
      work_type: WorkType;
      comment?: string | null;
      fold?: 0 | 1;
    },
    onSuccess: () => void,
  ) => void;
}) {
  const defaultStart = localParts(defaultStartLocal ?? `${day}T09:00`);
  const [open, setOpen] = useState(false);
  const [startDate, setStartDate] = useState(defaultStart.date);
  const [start, setStart] = useState(defaultStart.time);
  const [end, setEnd] = useState("");
  const [workType, setWorkType] = useState<WorkType>(defaultWorkType ?? "ON_SITE");
  const [comment, setComment] = useState("");
  const [noteOpen, setNoteOpen] = useState(false);
  const [nextDay, setNextDay] = useState(false);
  const [fold, setFold] = useState<0 | 1 | null>(null);
  if (!open) {
    return (
      <button
        type="button"
        className="add-interval"
        onClick={() => {
          setStartDate(defaultStart.date);
          setStart(defaultStart.time);
          setWorkType(defaultWorkType ?? "ON_SITE");
          setEnd("");
          setComment("");
          setNoteOpen(false);
          setNextDay(false);
          setFold(null);
          setOpen(true);
        }}
      >
        + Add interval
      </button>
    );
  }
  const startWall = `${startDate}T${start}:00`;
  const endWall = `${nextDay ? nextDate(startDate) : startDate}T${end}:00`;
  const orderError = intervalOrderError(start, end, nextDay);
  const ambiguousStart = isValidTime(start) && isAmbiguousBerlin(startWall);
  const ambiguousEnd = isValidTime(end) && isAmbiguousBerlin(endWall);
  // A disabled primary must say what it is waiting for. The DST fold was the
  // worst case: `Add` was disabled and only a placeholder option inside an
  // unrelated `<select>` hinted at why, so the employee saw a dead button and no
  // requirement (acceptance C1). docs/ARCHITECTURE.md: an ambiguous wall time must be
  // detected and SURFACED, never silently guessed.
  const foldRequired = (ambiguousStart || ambiguousEnd) && fold === null;
  const incompleteTime = (start !== "" && !isValidTime(start)) || (end !== "" && !isValidTime(end));
  const addRequirement = foldRequired
    ? "That start or end falls in the hour that happens twice tonight — choose which occurrence you mean."
    : incompleteTime
      ? "Enter the start and end as HH:MM — for example 08:00."
      : null;
  return (
    <fieldset className="interval-editor new-interval">
      <legend>Add interval</legend>
      <div className="editor-fields">
        <label>
          Start{" "}
          <TimeField
            aria-label="New start time"
            value={start}
            onChange={setStart}
            disabled={disabled}
          />
        </label>
        <label>
          End{" "}
          <TimeField
            aria-label="New end time"
            value={end}
            onChange={setEnd}
            disabled={disabled}
            aria-invalid={orderError ? true : undefined}
            aria-describedby={orderError ? "new-interval-order-error" : undefined}
          />
        </label>
        {orderError && (
          <p className="field-error" id="new-interval-order-error">
            {orderError}
          </p>
        )}
        {(ambiguousStart || ambiguousEnd) && (
          <label className="fold-choice">
            Occurrence for{" "}
            {ambiguousStart && ambiguousEnd
              ? "start and end"
              : ambiguousStart
                ? "start time"
                : "end time"}
            <select
              aria-label={`Occurrence for ${ambiguousStart && ambiguousEnd ? "start and end" : ambiguousStart ? "start time" : "end time"}`}
              value={fold ?? ""}
              onChange={(event) =>
                setFold(event.target.value === "" ? null : event.target.value === "1" ? 1 : 0)
              }
              disabled={disabled}
            >
              <option value="" disabled>
                Choose occurrence…
              </option>
              <option value={0}>First — summer time (CEST)</option>
              <option value={1}>Second — winter time (CET)</option>
            </select>
          </label>
        )}
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={nextDay}
            onChange={(event) => setNextDay(event.target.checked)}
            disabled={disabled}
          />{" "}
          Ends next day
        </label>
      </div>
      {startDate !== day && (
        <p className="cross-midnight">
          The latest interval ends on {shortDayLabel(startDate)}; this entry starts then.
        </p>
      )}
      <label>
        Work type{" "}
        <select
          aria-label="New work type"
          value={workType}
          onChange={(event) => setWorkType(event.target.value as WorkType)}
          disabled={disabled}
        >
          {workTypes.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </label>
      {noteOpen ? (
        <label className="note-field">
          Note{" "}
          <textarea
            aria-label="New note"
            rows={2}
            value={comment}
            onChange={(event) => setComment(event.target.value)}
            disabled={disabled}
          />
        </label>
      ) : (
        <button
          type="button"
          className="note-toggle"
          onClick={() => setNoteOpen(true)}
          disabled={disabled}
        >
          Add note…
        </button>
      )}
      <div className="editor-actions">
        <button type="button" onClick={() => setOpen(false)} disabled={disabled}>
          Cancel
        </button>
        <button
          type="button"
          className="primary"
          onClick={() =>
            onSave(
              {
                start_local: startWall,
                end_local: endWall,
                work_type: workType,
                comment: comment || null,
                ...(fold === null ? {} : { fold }),
              },
              () => setOpen(false),
            )
          }
          disabled={
            disabled ||
            !isValidTime(start) ||
            !isValidTime(end) ||
            orderError !== null ||
            foldRequired
          }
          aria-describedby={addRequirement ? "new-interval-requirement" : undefined}
        >
          Add
        </button>
      </div>
      {addRequirement && (
        <p className="field-requirement" id="new-interval-requirement" role="status">
          {addRequirement}
        </p>
      )}
    </fieldset>
  );
}

/** The calendar day before `day`, as an ISO date. */
function previousDayIso(day: string): string {
  const date = new Date(`${day}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() - 1);
  return date.toISOString().slice(0, 10);
}

/** Toggle + compact creation form for an explicit factual break (§5.3). Same
 *  deterministic-defaults discipline as the interval form: no break is created
 *  without explicit start, end and kind, and no time is invented. */
function AddBreakForm({
  day,
  disabled,
  onSave,
}: {
  day: string;
  disabled: boolean;
  onSave: (
    payload: {
      start_local: string;
      end_local: string;
      kind: BreakKind;
    },
    onSuccess: () => void,
  ) => void;
}) {
  const [open, setOpen] = useState(false);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [kind, setKind] = useState<BreakKind>("UNPAID");
  const [nextDay, setNextDay] = useState(false);
  const orderError = intervalOrderError(start, end, nextDay);
  if (!open) {
    return (
      <button
        type="button"
        className="add-interval"
        onClick={() => {
          setStart("");
          setEnd("");
          setKind("UNPAID");
          setNextDay(false);
          setOpen(true);
        }}
      >
        + Add break
      </button>
    );
  }
  return (
    <fieldset className="interval-editor new-interval">
      <legend>Add break</legend>
      <div className="editor-fields">
        <label>
          Start{" "}
          <TimeField
            aria-label="New break start time"
            value={start}
            onChange={setStart}
            disabled={disabled}
          />
        </label>
        <label>
          End{" "}
          <TimeField
            aria-label="New break end time"
            value={end}
            onChange={setEnd}
            disabled={disabled}
            aria-invalid={orderError ? true : undefined}
            aria-describedby={orderError ? "new-break-order-error" : undefined}
          />
        </label>
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={nextDay}
            onChange={(event) => setNextDay(event.target.checked)}
            disabled={disabled}
          />{" "}
          Ends next day
        </label>
        {orderError && (
          <p className="field-error" id="new-break-order-error">
            {orderError}
          </p>
        )}
      </div>
      <label>
        Kind{" "}
        <select
          aria-label="New break kind"
          value={kind}
          onChange={(event) => setKind(event.target.value as BreakKind)}
          disabled={disabled}
        >
          {breakKinds.map((value) => (
            <option key={value} value={value}>
              {breakKindLabels[value]}
            </option>
          ))}
        </select>
      </label>
      {/* "Ends next day" carries the following date in end_local so a
          cross-midnight break (23:45–00:15) stays a forward span; the server
          accepts it and only requires end > start. */}
      <div className="editor-actions">
        <button type="button" onClick={() => setOpen(false)} disabled={disabled}>
          Cancel
        </button>
        <button
          type="button"
          className="primary"
          onClick={() =>
            onSave(
              {
                start_local: `${day}T${start}:00`,
                end_local: `${nextDay ? nextDate(day) : day}T${end}:00`,
                kind,
              },
              () => setOpen(false),
            )
          }
          disabled={disabled || !isValidTime(start) || !isValidTime(end) || orderError !== null}
        >
          Add
        </button>
      </div>
    </fieldset>
  );
}

/** Toggle + compact creation form for a PLANNED shift the employee records for
 *  themselves (§6.1, D20).
 *
 *  A planned shift is a PLAN object, never actual work (INV-09), so it is created
 *  in its own form rather than as another interval — and the server forces
 *  `source = EMPLOYEE_RECORDED`, which is why the card that renders it reads
 *  "Planned shift", never "Approved plan" (§6.2).
 *
 *  Two `DateTimeField`s rather than a day plus two times: a shift may cross
 *  midnight (a night shift is the normal case here), and the payload is two
 *  instants. Both fields start EMPTY — the planned times are facts the employee
 *  states, and a default such as 09:00 would be a time nobody planned.
 *
 *  Accessibility: a real `<fieldset>`/`<legend>` group, a real `<label>` bound to
 *  every control (the label text IS the accessible name — no `aria-label`
 *  standing in for a visible label), native `<select>`/`<button>`, and the
 *  disabled primary states its own requirement through `aria-describedby`
 *  (WCAG 2.2 SC 1.3.1, 2.1.1, 3.3.2, 4.1.2). Every rejection keeps the existing
 *  `role="alert"` field-message contract. */
function AddShiftPlanForm({ onFactsChanged }: { onFactsChanged: () => void }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [plannedStart, setPlannedStart] = useState("");
  const [plannedEnd, setPlannedEnd] = useState("");
  // "Shift" is the neutral kind (the label fallback for an unknown kind is the
  // same word), so the default claims nothing about the pattern worked.
  const [shiftKind, setShiftKind] = useState("SHIFT");
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const createMutation = useMutation({
    mutationFn: () =>
      createShiftPlan({
        planned_start_local: startIso as string,
        planned_end_local: endIso as string,
        shift_kind: shiftKind,
      }),
    onSuccess: () => {
      // The plan moves the calendar marker, the day panel and the month, so the
      // whole fact-derived family refreshes — not just this day's shift list.
      invalidateFactDerived(queryClient);
      onFactsChanged();
      setNotice({ kind: "ok", text: "Planned shift recorded." });
      setOpen(false);
      setPlannedStart("");
      setPlannedEnd("");
    },
    onError: (error) => setNotice(errorNotice(error)),
  });
  const startIso = toIsoDateTime(plannedStart);
  const endIso = toIsoDateTime(plannedEnd);
  // Both values are `YYYY-MM-DDTHH:MM` in the canonical zone, so they compare as
  // strings; parsing them into `Date`s would reintroduce a timezone question for
  // an answer that is already exact.
  const orderError =
    startIso !== null && endIso !== null && startIso >= endIso
      ? "The planned end must be after the planned start."
      : null;
  const valid = startIso !== null && endIso !== null && orderError === null;
  const issueProps = (field: string, id: string) =>
    fieldIssueFor(createMutation.error, field)
      ? { "aria-invalid": true as const, "aria-describedby": id }
      : {};
  if (!open) {
    return (
      <>
        <button
          type="button"
          className="add-interval"
          onClick={() => {
            setPlannedStart("");
            setPlannedEnd("");
            setShiftKind("SHIFT");
            setNotice(null);
            setOpen(true);
          }}
        >
          + Add planned shift
        </button>
        {/* The notice lives OUTSIDE the form on purpose: a successful save
         *  collapses the form, and a message rendered inside it would be
         *  unmounted in the same commit that produced it — the employee would
         *  never see it (the `DayTimeline`/`NoticeLine` precedent). */}
        <ActionNotice notice={notice} />
      </>
    );
  }
  return (
    <fieldset className="interval-editor new-interval">
      <legend>Add planned shift</legend>
      <div className="editor-fields">
        <label>
          Planned start
          <DateTimeField
            value={plannedStart}
            onChange={setPlannedStart}
            required
            {...issueProps("planned_start_local", "new-plan-start-issue")}
          />
        </label>
        <label>
          Planned end
          <DateTimeField
            value={plannedEnd}
            onChange={setPlannedEnd}
            required
            {...issueProps("planned_end_local", "new-plan-end-issue")}
          />
        </label>
        {orderError && (
          <p className="field-error" id="new-plan-order-error">
            {orderError}
          </p>
        )}
      </div>
      <label>
        Shift kind
        <select
          value={shiftKind}
          onChange={(event) => setShiftKind(event.target.value)}
          {...issueProps("shift_kind", "new-plan-kind-issue")}
        >
          {Object.entries(shiftKindLabels).map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <div className="editor-actions">
        <button type="button" onClick={() => setOpen(false)}>
          Cancel
        </button>
        <button
          type="button"
          className="primary"
          onClick={() => createMutation.mutate()}
          disabled={createMutation.isPending || !valid}
          aria-describedby={valid ? undefined : "new-plan-requirement"}
        >
          Add
        </button>
      </div>
      {!valid && (
        <p className="field-requirement" id="new-plan-requirement" role="status">
          Enter the planned start and end as DD.MM.YYYY HH:MM — for example 31.07.2026 22:00. A wall
          time that falls in the hour that happens twice is refused rather than guessed.
        </p>
      )}
      <ActionNotice notice={notice} />
      <FieldIssue
        error={createMutation.error}
        field="planned_start_local"
        id="new-plan-start-issue"
      />
      <FieldIssue error={createMutation.error} field="planned_end_local" id="new-plan-end-issue" />
      <FieldIssue error={createMutation.error} field="shift_kind" id="new-plan-kind-issue" />
    </fieldset>
  );
}
