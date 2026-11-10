import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useRef, useState } from "react";

import {
  correctOpeningBalance,
  establishOnboarding,
  fetchOnboarding,
  fetchProfile,
  savePreferences,
  saveWorkingTime,
  type OnboardingState,
  type TemplateInterval,
  type WorkType,
  type WorkingTimeSchedule,
} from "../lib/api";
import { berlinToday, monthTitle } from "../lib/format";
import { DateField, isoToGermanDate, MonthField, TimeField, toIsoDate } from "../lib/inputs";
import { invalidateFactDerived } from "../lib/query";
import { refusalText } from "../lib/notice";

type DayKey = "mon" | "tue" | "wed" | "thu" | "fri" | "sat" | "sun";

const WEEKDAYS: Array<{ key: DayKey; label: string }> = [
  { key: "mon", label: "Monday" },
  { key: "tue", label: "Tuesday" },
  { key: "wed", label: "Wednesday" },
  { key: "thu", label: "Thursday" },
  { key: "fri", label: "Friday" },
  { key: "sat", label: "Saturday" },
  { key: "sun", label: "Sunday" },
];

const DAY_FIELDS: Array<`${DayKey}_required_minutes`> = [
  "mon_required_minutes",
  "tue_required_minutes",
  "wed_required_minutes",
  "thu_required_minutes",
  "fri_required_minutes",
  "sat_required_minutes",
  "sun_required_minutes",
];

const MAX_DAY_MINUTES = 24 * 60;

/** Human labels for pay-group buckets; raw bucket codes are never shown. */
const payGroupLabels: Record<string, string> = {
  LE_9B: "Pay group up to 9B",
  GE_9C: "Pay group 9C or above",
  UNKNOWN: "Not classified",
};

/** Parse a human-readable duration like `6:00`, `7:48`, `29` or `29:15`.
 *  Blank is 0:00. Returns null for anything malformed. Never floats. */
export function parseDuration(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === "") return 0;
  const match = /^(\d{1,3})(?::([0-5]\d))?$/.exec(trimmed);
  if (!match) return null;
  return parseInt(match[1], 10) * 60 + (match[2] ? parseInt(match[2], 10) : 0);
}

/** Format minutes as `29:00` / `−5:00` without a plus sign (editor context). */
export function formatDuration(value: number): string {
  const sign = value < 0 ? "−" : "";
  const absolute = Math.abs(value);
  return `${sign}${Math.floor(absolute / 60)}:${String(absolute % 60).padStart(2, "0")}`;
}

/** Default working days: existing arrangement's positive days, else Mon-Fri. */
function initialWorkingDays(schedule: WorkingTimeSchedule | null): Record<DayKey, boolean> {
  const fromSchedule = Object.fromEntries(
    WEEKDAYS.map(({ key }) => [key, (schedule?.[`${key}_required_minutes`] ?? 0) > 0]),
  ) as Record<DayKey, boolean>;
  if (Object.values(fromSchedule).some(Boolean)) return fromSchedule;
  return { mon: true, tue: true, wed: true, thu: true, fri: true, sat: false, sun: false };
}

export function ProfilePage() {
  const queryClient = useQueryClient();
  const profile = useQuery({ queryKey: ["profile"], queryFn: () => fetchProfile() });
  const onboarding = useQuery({ queryKey: ["onboarding"], queryFn: () => fetchOnboarding() });
  const save = useMutation({
    mutationFn: savePreferences,
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["profile"] }),
  });
  const [usualWorkType, setUsualWorkType] = useState<WorkType | null>(null);
  const [view, setView] = useState<string | null>(null);
  const [template, setTemplate] = useState<TemplateInterval[] | null>(null);

  if (profile.isPending) return <p role="status">Loading your profile…</p>;
  if (profile.isError) return <p className="status-error">Could not load your profile.</p>;
  const data = profile.data;
  const currentType = usualWorkType ?? data.preferences.usual_work_type ?? "ON_SITE";
  const currentView = view ?? data.preferences.preferred_calendar_view ?? "month";
  const currentTemplate = template ?? data.preferences.normal_day_template ?? [];
  const refreshOnboarding = () => {
    void queryClient.invalidateQueries({ queryKey: ["onboarding"] });
    void queryClient.invalidateQueries({ queryKey: ["profile"] });
    // The maintenance editor reads its own target-keyed key; establishing a start
    // state creates the arrangement that governs the cutover month.
    void queryClient.invalidateQueries({ queryKey: ["profile-at"] });
    // Establishing the cutover state writes a weekly schedule AND the opening
    // balance — the same kind of fact `saveWorkingTime` writes, and the reason
    // that mutation uses the full breadth. Invalidating `["month"]` alone left
    // the findings list, the day panel and Home's balance card showing the
    // pre-establishment state (review R21, found while checking R18's class).
    invalidateFactDerived(queryClient);
  };
  return (
    <section className="profile-page">
      <p className="eyebrow">Private — visible only to you</p>
      <h1>Profile</h1>
      <div className="profile-grid">
        <section>
          <h2>Authoritative profile</h2>
          {data.contract ? (
            <p>
              Name, employee ID, FTE and contractual hours come from the authoritative master-data
              source and cannot be edited here.
            </p>
          ) : (
            // D1: this MVP does not synchronise contractual hours from HR. Saying
            // "Not available" for hours the employee sets themselves, two cards
            // below, would contradict the page.
            <p>
              Name and employee ID come from the authoritative master-data source. Your working time
              is not synchronised from HR — you maintain it yourself in <em>My working time</em>.
            </p>
          )}
          <dl className="profile-list">
            <div>
              <dt>Name</dt>
              <dd>{data.employee.legal_name}</dd>
            </div>
            <div>
              <dt>Employee ID</dt>
              <dd>{data.employee.external_employee_id ?? "Not available"}</dd>
            </div>
            {data.contract && (
              <>
                <div>
                  <dt>FTE</dt>
                  <dd>{data.contract.fte ?? "Not available"}</dd>
                </div>
                <div>
                  <dt>Contractual weekly hours (HR)</dt>
                  <dd>{formatDuration(data.contract.weekly_required_minutes)}</dd>
                </div>
                <div>
                  <dt>Pay group bucket</dt>
                  <dd>
                    {payGroupLabels[data.contract.pay_group_bucket] ??
                      data.contract.pay_group_bucket}
                  </dd>
                </div>
              </>
            )}
          </dl>
          <p>
            <strong>Report profile data issue:</strong> use the approved HR/master-data support
            channel. This service cannot change authoritative data.
          </p>
        </section>
        <OnboardingSection onboarding={onboarding} onChanged={refreshOnboarding} />
        {/* §5.2: onboarding must NEVER remove a working-time capability. The
            standalone editor is hidden only while the onboarding form is
            actually present to take its place — never because a cutover
            prerequisite is unavailable. */}
        {onboarding.data?.state !== "READY" && (
          <WorkingTimeEditor
            schedule={data.schedule}
            contractWeekly={data.contract?.weekly_required_minutes ?? null}
            cutoverMonth={onboarding.data?.opening?.cutover_month ?? null}
          />
        )}
      </div>
      <section className="card preferences">
        <h2>Preferences</h2>
        <p>These do not change your contract or schedule.</p>
        <label>
          Usual work type{" "}
          <select
            value={currentType}
            onChange={(event) => setUsualWorkType(event.target.value as WorkType)}
          >
            <option value="ON_SITE">On-site</option>
            <option value="REMOTE">Remote</option>
          </select>
        </label>
        <label>
          Preferred calendar view{" "}
          <select value={currentView} onChange={(event) => setView(event.target.value)}>
            <option value="month">Month</option>
            <option value="week">Week</option>
          </select>
        </label>
        <h3>Normal-day template</h3>
        <p>
          Applying this pattern creates intervals only after you choose a day and confirm the
          preview.
        </p>
        <TemplateEditor template={currentTemplate} onChange={setTemplate} />
        <button
          type="button"
          onClick={() =>
            save.mutate({
              usual_work_type: currentType,
              preferred_calendar_view: currentView,
              normal_day_template: currentTemplate,
            })
          }
        >
          Save preferences
        </button>
        {save.isPending && <p role="status">Saving…</p>}
        {save.isError && (
          <p className="status-error" role="alert">
            Could not save preferences.
          </p>
        )}
        {save.isSuccess && <p className="status-ok">Preferences saved. No data was shared.</p>}
      </section>
    </section>
  );
}

/** The employee's one-time Chronobeam start (§5.3–§5.5).
 *
 *  Three concepts that must not be disentangled by prose: WHEN Chronobeam starts
 *  (the cutover month), the NORMAL weekly working time, and the balance CARRIED
 *  from the previous system. They become three `<fieldset>` groups, which is the
 *  native grouping pattern and gives each group its accessible name.
 *
 *  One form, no wizard and no modal: the incumbent vocabulary for a multi-part
 *  task is an inline card (`.card`), exactly like the working-time editor below.
 */
function OnboardingSection({
  onboarding,
  onChanged,
}: {
  onboarding: {
    isPending: boolean;
    isError: boolean;
    data: OnboardingState | undefined;
  };
  onChanged: () => void;
}) {
  // The card heading is the focus target after a successful establish: the form
  // is replaced by the summary, so focus would otherwise fall to <body>.
  const headingRef = useRef<HTMLHeadingElement>(null);
  // The announcement must survive that swap, so the live region lives HERE and
  // not inside the form it describes.
  const [announcement, setAnnouncement] = useState("");
  return (
    <section className="card onboarding" aria-labelledby="onboarding-heading">
      <h2 id="onboarding-heading" ref={headingRef} tabIndex={-1}>
        Your Chronobeam start
      </h2>
      {announcement && (
        <p className="status-ok" role="status">
          {announcement}
        </p>
      )}
      {onboarding.isPending && <p role="status">Loading your start state…</p>}
      {onboarding.isError && (
        <p className="status-error" role="alert">
          Could not load your start state. Your saved entries are unchanged.
        </p>
      )}
      {onboarding.data && (
        <OnboardingBody
          state={onboarding.data}
          onChanged={onChanged}
          headingRef={headingRef}
          announce={setAnnouncement}
        />
      )}
    </section>
  );
}

function OnboardingBody({
  state,
  onChanged,
  headingRef,
  announce,
}: {
  state: OnboardingState;
  onChanged: () => void;
  headingRef: React.RefObject<HTMLHeadingElement | null>;
  announce: (message: string) => void;
}) {
  // Explicit switch, NOT a fallthrough. An absent or unrecognised `state` (a
  // stale fixture, a malformed payload) must never conjure the onboarding form:
  // it would duplicate the working-time setup and invite the employee to
  // establish a start state the server never asked for. Fail safe — the card
  // stays quiet and the standalone editor keeps the capability.
  switch (state.state) {
    case "BLOCKED":
      return <OnboardingBlocked state={state} />;
    case "COMPLETED":
      return <OnboardingCompleted state={state} onChanged={onChanged} headingRef={headingRef} />;
    case "READY":
      return (
        <OnboardingForm
          state={state}
          onChanged={onChanged}
          headingRef={headingRef}
          announce={announce}
        />
      );
    default:
      return null;
  }
}

// The ONE place a blocked reason becomes words. The body-level BLOCKED card and
// the in-form refusal for a month the employee picks must never drift apart.
function blockedReasonSentence(state: OnboardingState): string {
  const month = state.blocked_month ? monthTitle(state.blocked_month) : "that month";
  return {
    POLICY_EDITION_MISSING: `The working-time rules for ${month} are not configured yet, so Chronobeam cannot start in it.`,
    CALENDAR_MISSING: `The public-holiday calendar does not cover ${month} yet, so the required time for it cannot be worked out.`,
    ROLLOUT_BOUNDARY: `Chronobeam cannot start in ${month} — that is earlier than the first month this service covers.`,
  }[state.blocked_reason ?? "ROLLOUT_BOUNDARY"];
}

function OnboardingBlocked({ state }: { state: OnboardingState }) {
  const reason = blockedReasonSentence(state);
  return (
    <>
      <p className="private-note">{reason}</p>
      {state.default_start_month && (
        <p className="working-time-note">
          The earliest month you can start in is{" "}
          <strong>{monthTitle(state.default_start_month)}</strong>.
        </p>
      )}
      {/* Never a "try again": this is a permanent property of the month, not a
          transient fault. The working-time editor below stays available. */}
      <p className="working-time-note">Your working time below can still be set and changed.</p>
    </>
  );
}

function OnboardingCompleted({
  state,
  onChanged,
  headingRef,
}: {
  state: OnboardingState;
  onChanged: () => void;
  headingRef: React.RefObject<HTMLHeadingElement | null>;
}) {
  const opening = state.opening;
  if (!opening) return null;
  return (
    <>
      <dl className="profile-list">
        <div>
          <dt>Started</dt>
          <dd>{monthTitle(opening.cutover_month)}</dd>
        </div>
        <div>
          <dt>Weekly working time</dt>
          <dd>
            {state.schedule
              ? `${formatDuration(state.schedule.weekly_required_minutes)} (${workingDaySummary(state.schedule)})`
              : "Not recorded"}
          </dd>
        </div>
        <div>
          <dt>Carried balance</dt>
          <dd>
            {carriedSummary(opening.minutes)} from {monthTitle(opening.previous_period)}
            {opening.corrected_at && ", corrected by you"}
          </dd>
        </div>
      </dl>
      {state.correctable ? (
        <CorrectionDisclosure opening={opening} onChanged={onChanged} headingRef={headingRef} />
      ) : (
        <p className="working-time-note">
          {inUseSentence(state.in_use_reason, opening.cutover_month)} Self-service correction is no
          longer available. Contact the administrator if the carried balance needs to be corrected.
        </p>
      )}
    </>
  );
}

/** Employee words for why the window closed — never a code, never a reason
 *  that implies a workflow we do not have. */
function inUseSentence(reason: OnboardingState["in_use_reason"], cutoverMonth: string): string {
  const month = monthTitle(cutoverMonth);
  if (reason === "IN_USE_BY_REVIEW") return `You have already reviewed ${month}.`;
  if (reason === "IN_USE_BY_DISCLOSURE") return `${month} has been shared.`;
  return `You have already recorded work in ${month}.`;
}

function CorrectionDisclosure({
  opening,
  onChanged,
  headingRef,
}: {
  opening: NonNullable<OnboardingState["opening"]>;
  onChanged: () => void;
  headingRef: React.RefObject<HTMLHeadingElement | null>;
}) {
  const detailsRef = useRef<HTMLDetailsElement>(null);
  const firstFieldRef = useRef<HTMLInputElement>(null);
  const reasonHintId = useId();
  const [direction, setDirection] = useState<"deficit" | "credit">(
    opening.minutes < 0 ? "deficit" : "credit",
  );
  const [amount, setAmount] = useState(formatDuration(Math.abs(opening.minutes)));
  const [reason, setReason] = useState("");
  const correction = useMutation({
    mutationFn: correctOpeningBalance,
    onSuccess: () => {
      setReason("");
      // (a) focus management: the disclosure closes, so focus must not be left
      // on a removed control.
      headingRef.current?.focus();
      onChanged();
    },
  });

  const parsed = parseDuration(amount);
  const total = parsed === null ? null : direction === "deficit" ? -parsed : parsed;
  const unchanged = total === opening.minutes;
  const canSave = total !== null && reason.trim().length > 0 && !unchanged;

  return (
    // The incumbent disclosure vocabulary (`.finding-pin` is a <details>): native
    // expand/collapse, keyboard operable, no ARIA needed.
    <details
      className="finding-pin correction"
      ref={detailsRef}
      onToggle={(event) => {
        // (c) opening the disclosure moves focus into its first field.
        if (event.currentTarget.open) firstFieldRef.current?.focus();
      }}
    >
      <summary>Correct the carried balance</summary>
      <p className="working-time-note">
        Adds a correction — earlier entries are not deleted or rewritten.
      </p>
      <fieldset className="balance-direction">
        <legend>Which way does it go?</legend>
        <label className="checkbox-label">
          <input
            type="radio"
            name="correction-direction"
            checked={direction === "deficit"}
            onChange={() => setDirection("deficit")}
          />
          I owe time (deficit)
        </label>
        <label className="checkbox-label">
          <input
            type="radio"
            name="correction-direction"
            checked={direction === "credit"}
            onChange={() => setDirection("credit")}
          />
          I am owed time (credit)
        </label>
      </fieldset>
      <label>
        Corrected carried balance{" "}
        <input
          ref={firstFieldRef}
          aria-label="Corrected carried balance"
          inputMode="numeric"
          value={amount}
          onChange={(event) => setAmount(event.target.value)}
          placeholder="0:00"
        />
      </label>
      <label>
        Why are you correcting it?{" "}
        <input
          aria-label="Reason for the correction"
          aria-required="true"
          aria-describedby={reasonHintId}
          value={reason}
          maxLength={500}
          onChange={(event) => setReason(event.target.value)}
          placeholder="The carried figure was mistyped"
        />
      </label>
      {/* SIBLING of the label, not inside it: text inside a wrapping label
          becomes part of the control's accessible name. The reason is required,
          so the requirement is stated where it is asked for — and the disabled
          primary points at it. */}
      <p className="working-time-note" id={reasonHintId}>
        Required: say why you are correcting it. Your reason is recorded with the correction.
      </p>
      <p className="balance-summary" aria-live="polite">
        <span>
          Corrected balance <strong>{carriedSummary(total ?? 0)}</strong>
        </span>
      </p>
      <div className="editor-actions">
        <button
          type="button"
          className="primary"
          disabled={!canSave || correction.isPending}
          aria-describedby={reasonHintId}
          onClick={() =>
            total !== null &&
            correction.mutate({
              expected_revision: opening.revision,
              opening_balance_minutes: total,
              reason: reason.trim(),
            })
          }
        >
          Save correction
        </button>
      </div>
      {correction.isPending && <p role="status">Saving…</p>}
      {correction.isError && (
        <p className="status-error" role="alert">
          Could not save the correction: {refusalText(correction.error) || "try again"}
        </p>
      )}
    </details>
  );
}

/** "Mon–Fri" / "Mon, Wed, Fri" — the working days in words. */
function workingDaySummary(schedule: WorkingTimeSchedule | null): string {
  if (!schedule) return "no days set";
  const active = WEEKDAYS.filter(({ key }) => schedule[`${key}_required_minutes`] > 0);
  if (active.length === 0) return "no days set";
  if (
    active.length === 5 &&
    active.every(({ key }) => ["mon", "tue", "wed", "thu", "fri"].includes(key))
  )
    return "Mon–Fri";
  return active.map(({ label }) => label.slice(0, 3)).join(", ");
}

/** Signed minutes in words, so the meaning is never carried by a sign alone. */
function carriedSummary(minutes: number): string {
  if (minutes === 0) return "Nothing carried over";
  return minutes > 0
    ? `Carried credit ${formatDuration(minutes)}`
    : `Carried deficit ${formatDuration(Math.abs(minutes))}`;
}

function OnboardingForm({
  state,
  onChanged,
  headingRef,
  announce,
}: {
  state: OnboardingState;
  onChanged: () => void;
  headingRef: React.RefObject<HTMLHeadingElement | null>;
  announce: (message: string) => void;
}) {
  const errorRef = useRef<HTMLParagraphElement>(null);
  const formId = useId();
  // `null` until the employee picks a month, so the server's suggestion is used
  // without an effect — and when there is no covered month NOTHING is
  // preselected rather than a guess.
  const [monthChoice, setMonthChoice] = useState<string | null>(null);
  const startMonth = monthChoice ?? state.default_start_month ?? "";
  // The earliest month this service covers is known WITHOUT asking: the server
  // derives `default_start_month` from installed policy/calendar coverage, so
  // anything before it is uncovered by construction. Refusing it here means a
  // past month is answered in words and no request is sent at all.
  const monthFloor = state.default_start_month;
  const monthShapeOk = /^\d{4}-\d{2}$/.test(startMonth);
  const monthTooEarly = monthFloor !== null && monthShapeOk && startMonth < monthFloor;
  // The advisory verdict is per MONTH, and the card loads exactly one. Once the
  // employee picks a different month, re-read so the readiness and the
  // already-recorded-facts warning describe the month they actually chose
  // rather than the one the card happened to open with.
  const selectedMonth =
    monthChoice !== null && monthChoice !== state.month && monthShapeOk && !monthTooEarly
      ? monthChoice
      : null;
  const monthAdvisory = useQuery({
    queryKey: ["onboarding", selectedMonth],
    queryFn: () => fetchOnboarding(selectedMonth ?? undefined),
    enabled: selectedMonth !== null,
  });
  // A failed advisory read is not a refusal: fall back to the loaded month and
  // let the authoritative POST answer, exactly as before.
  const monthState = monthAdvisory.data ?? state;
  const [weeklyText, setWeeklyText] = useState(
    state.schedule ? formatDuration(state.schedule.weekly_required_minutes) : "",
  );
  const [dayTexts, setDayTexts] = useState<Record<DayKey, string>>(
    () =>
      Object.fromEntries(
        WEEKDAYS.map(({ key }) => [
          key,
          state.schedule ? formatDuration(state.schedule[`${key}_required_minutes`]) : "",
        ]),
      ) as Record<DayKey, string>,
  );
  const [working, setWorking] = useState<Record<DayKey, boolean>>(() =>
    initialWorkingDays(state.schedule),
  );
  const [direction, setDirection] = useState<"deficit" | "credit">("deficit");
  const [balanceText, setBalanceText] = useState("");

  const establish = useMutation({
    mutationFn: establishOnboarding,
    onSuccess: () => {
      announce("Your Chronobeam start is set up.");
      onChanged();
    },
  });

  // (a) After a successful establish the form is replaced by the summary, so
  // focus would fall to <body>. Move it to the card heading instead.
  useEffect(() => {
    if (establish.isSuccess) headingRef.current?.focus();
  }, [establish.isSuccess]);

  // (b) A server refusal must not leave the employee hunting for the reason:
  // move focus to the error summary, which is announced by role="alert".
  useEffect(() => {
    if (establish.isError) errorRef.current?.focus();
  }, [establish.isError]);

  const weekly = parseDuration(weeklyText);
  const days = Object.fromEntries(
    WEEKDAYS.map(({ key }) => [key, parseDuration(dayTexts[key])]),
  ) as Record<DayKey, number | null>;
  const allocated = Object.values(days).reduce<number>((sum, value) => sum + (value ?? 0), 0);
  const remaining = (weekly ?? 0) - allocated;

  const dayError = (key: DayKey): string | null => {
    const value = days[key];
    if (value === null) return "Use H:MM";
    if (value > MAX_DAY_MINUTES) return "Max 24:00 per day";
    return null;
  };
  const weeklyError = weekly === null ? "Use H:MM (e.g. 29:00)" : null;
  const sumError =
    remaining !== 0
      ? `${formatDuration(Math.abs(remaining))} ${remaining > 0 ? "remaining" : "over-allocated"} — allocate exactly your weekly total`
      : null;
  const balance = parseDuration(balanceText);
  const balanceError = balance === null ? "Use H:MM (e.g. 18:24)" : null;
  const signedBalance = balance === null ? null : direction === "deficit" ? -balance : balance;
  const monthBlocked = monthState.state === "BLOCKED" ? blockedReasonSentence(monthState) : null;
  const monthOk = monthShapeOk && !monthTooEarly && monthBlocked === null;
  const monthError = monthTooEarly
    ? `Chronobeam cannot start before ${monthTitle(monthFloor ?? "")}.`
    : monthBlocked;
  const canSave =
    monthOk &&
    weekly !== null &&
    balance !== null &&
    Object.values(days).every((value) => value !== null && value <= MAX_DAY_MINUTES) &&
    remaining === 0;

  const setDay = (key: DayKey, text: string) =>
    setDayTexts((current) => ({ ...current, [key]: text }));
  const toggleWorking = (key: DayKey) => {
    setWorking((current) => {
      const next = { ...current, [key]: !current[key] };
      if (!next[key]) setDay(key, "0:00");
      return next;
    });
  };
  const useRemaining = (key: DayKey) => {
    if (remaining > 0) setDay(key, formatDuration(remaining));
  };
  const distributeEvenly = () => {
    const selected = WEEKDAYS.filter(({ key }) => working[key]).map(({ key }) => key);
    if (weekly === null || selected.length === 0) return;
    const base = Math.floor(weekly / selected.length);
    const remainder = weekly % selected.length;
    selected.forEach((key, index) =>
      setDay(key, formatDuration(base + (index < remainder ? 1 : 0))),
    );
  };

  const submit = () => {
    if (!canSave || weekly === null || signedBalance === null) return;
    establish.mutate({
      start_month: startMonth,
      weekly_required_minutes: weekly,
      mon_required_minutes: days.mon ?? 0,
      tue_required_minutes: days.tue ?? 0,
      wed_required_minutes: days.wed ?? 0,
      thu_required_minutes: days.thu ?? 0,
      fri_required_minutes: days.fri ?? 0,
      sat_required_minutes: days.sat ?? 0,
      sun_required_minutes: days.sun ?? 0,
      opening_balance_minutes: signedBalance,
    });
  };

  // One statement of the whole start, so the three groups are confirmed together.
  const confirmLine = [
    monthOk ? `From ${monthTitle(startMonth)}` : "Choose a start month",
    weekly !== null
      ? `${formatDuration(weekly)}/week, ${workingDaySummary(scheduleFrom(days))}`
      : null,
    signedBalance !== null ? carriedSummary(signedBalance).toLowerCase() : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <>
      <p className="private-note">
        Starts at a month boundary. Earlier months stay in the Excel archive; only the carried
        balance comes across.
      </p>
      {monthState.has_facts_in_month && (
        <p className="suggestion" role="status">
          You have already recorded facts in {monthTitle(monthState.month)}. Setting your start
          state recalculates that month, and after it is in use you can no longer correct the
          carried balance yourself.
        </p>
      )}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <fieldset>
          <legend>When Chronobeam starts for you</legend>
          <label>
            Start month{" "}
            <MonthField
              aria-label="Start month"
              aria-describedby={
                monthError ? `${formId}-start-note ${formId}-month-error` : `${formId}-start-note`
              }
              aria-invalid={monthError ? true : undefined}
              value={startMonth}
              onChange={setMonthChoice}
            />
          </label>
          {monthError && (
            // WCAG 2.2 SC 3.3.1/1.3.1: the refusal is associated with the field
            // it is about, like every other error on this card.
            <p className="status-error" id={`${formId}-month-error`} role="alert">
              {monthError}
            </p>
          )}
          <p className="working-time-note" id={`${formId}-start-note`}>
            Your first month in Chronobeam.
          </p>
        </fieldset>
        <fieldset>
          <legend>Your normal weekly working time</legend>
          <label>
            Weekly working time{" "}
            <input
              aria-label="Weekly working time"
              aria-describedby={weeklyError ? `${formId}-weekly-error` : undefined}
              aria-invalid={weeklyError ? true : undefined}
              inputMode="numeric"
              value={weeklyText}
              onChange={(event) => setWeeklyText(event.target.value)}
              placeholder="29:00"
            />
          </label>
          {weeklyError && (
            // WCAG 2.2 SC 3.3.1/1.3.1: the message is associated with the input it
            // describes, exactly like the per-weekday errors below — a detached
            // form-level alert left the employee unable to tell WHICH control was
            // wrong.
            <p className="status-error weekday-error" id={`${formId}-weekly-error`}>
              {weeklyError}
            </p>
          )}
          <fieldset className="working-day-picker">
            <legend>Working days — used by Distribute evenly</legend>
            {WEEKDAYS.map(({ key, label }) => (
              <label key={key} className="checkbox-label">
                <input
                  type="checkbox"
                  checked={working[key]}
                  onChange={() => toggleWorking(key)}
                  aria-label={`${label} working day`}
                />
                {label}
              </label>
            ))}
          </fieldset>
          <div className="weekday-grid">
            {WEEKDAYS.map(({ key, label }) => {
              const message = dayError(key);
              return (
                <div className="weekday-cell" key={key}>
                  <label>
                    {label}{" "}
                    <input
                      aria-label={`${label} required time`}
                      aria-describedby={message ? `${formId}-${key}-error` : undefined}
                      aria-invalid={message ? true : undefined}
                      inputMode="numeric"
                      value={dayTexts[key]}
                      disabled={!working[key]}
                      onChange={(event) => setDay(key, event.target.value)}
                      placeholder="0:00"
                    />
                  </label>
                  <div className="weekday-actions">
                    {working[key] && (
                      <button
                        type="button"
                        aria-label={`Use remaining for ${label}`}
                        disabled={remaining <= 0}
                        onClick={() => useRemaining(key)}
                      >
                        Use remaining
                      </button>
                    )}
                  </div>
                  {message && (
                    // WCAG 2.2 SC 3.3.1/1.3.1: the message is associated with the
                    // input it describes (id + aria-describedby + aria-invalid),
                    // not merely adjacent to it.
                    <p className="status-error weekday-error" id={`${formId}-${key}-error`}>
                      {message}
                    </p>
                  )}
                </div>
              );
            })}
          </div>
          <div className="working-time-actions">
            <button type="button" onClick={distributeEvenly}>
              Distribute evenly
            </button>
            <span aria-live="polite" className="working-time-totals">
              <span>
                Weekly total <strong>{formatDuration(weekly ?? 0)}</strong>
              </span>
              <span>
                Allocated <strong>{formatDuration(allocated)}</strong>
              </span>
              <span className={remaining === 0 ? "remaining-ok" : "remaining-excess"}>
                Remaining <strong>{formatDuration(remaining)}</strong>
              </span>
            </span>
          </div>
        </fieldset>
        <fieldset>
          <legend>Balance carried from the previous system</legend>
          <fieldset className="balance-direction">
            <legend>Which way does it go?</legend>
            <label className="checkbox-label">
              <input
                type="radio"
                name="balance-direction"
                checked={direction === "deficit"}
                onChange={() => setDirection("deficit")}
              />
              I owe time (deficit)
            </label>
            <label className="checkbox-label">
              <input
                type="radio"
                name="balance-direction"
                checked={direction === "credit"}
                onChange={() => setDirection("credit")}
              />
              I am owed time (credit)
            </label>
          </fieldset>
          <label>
            Carried balance{" "}
            <input
              aria-label="Carried balance"
              aria-describedby={`${formId}-balance-note`}
              aria-invalid={balanceError ? true : undefined}
              inputMode="numeric"
              value={balanceText}
              onChange={(event) => setBalanceText(event.target.value)}
              placeholder="0:00"
            />
          </label>
          <p className="working-time-note" id={`${formId}-balance-note`}>
            Enter 0:00 if nothing carries over. {balanceError ?? ""}
          </p>
          <p className="balance-summary" aria-live="polite">
            <span>
              <strong>
                {signedBalance === null ? "Enter a balance" : carriedSummary(signedBalance)}
              </strong>
            </span>
          </p>
        </fieldset>
        <p className="working-time-note">{confirmLine}</p>
        {/* The allocation mismatch is a FORM-level fact (two controls disagree), so it
            keeps the form-level alert; the weekly field's own format error is
            associated with that field above. */}
        {sumError ? (
          <p className="status-error" role="alert" ref={errorRef} tabIndex={-1}>
            {sumError}
          </p>
        ) : null}
        {establish.isError && (
          <p className="status-error" role="alert" ref={errorRef} tabIndex={-1}>
            Could not set your start state: {refusalText(establish.error) || "try again"}
          </p>
        )}
        <div className="editor-actions">
          <button
            type="button"
            className="primary"
            disabled={!canSave || establish.isPending}
            onClick={submit}
          >
            Confirm my start
          </button>
        </div>
        {establish.isPending && <p role="status">Saving…</p>}
      </form>
    </>
  );
}

/** A `WorkingTimeSchedule`-shaped view of the in-progress weekday entries, so
 *  the "which days" summary reuses one implementation. */
function scheduleFrom(days: Record<DayKey, number | null>): WorkingTimeSchedule | null {
  if (Object.values(days).every((value) => value === null)) return null;
  return {
    valid_from: "",
    valid_to: null,
    weekly_required_minutes: 0,
    mon_required_minutes: days.mon ?? 0,
    tue_required_minutes: days.tue ?? 0,
    wed_required_minutes: days.wed ?? 0,
    thu_required_minutes: days.thu ?? 0,
    fri_required_minutes: days.fri ?? 0,
    sat_required_minutes: days.sat ?? 0,
    sun_required_minutes: days.sun ?? 0,
    source: "EMPLOYEE",
    version: 0,
  };
}

/** One arrangement's identity: its maintenance key (`valid_from`), its version
 *  and every value the editor writes. The editor re-syncs on THIS — not once —
 *  so the fields always belong to the arrangement the target resolves to, while
 *  a re-render of the same arrangement never clobbers an edit in progress. */
function scheduleKey(schedule: WorkingTimeSchedule | null): string {
  if (!schedule) return "none";
  return [
    schedule.valid_from,
    schedule.version,
    schedule.weekly_required_minutes,
    ...DAY_FIELDS.map((field) => schedule[field]),
  ].join("|");
}

/** The seven weekday entries as the texts they are edited in. */
function dayTextsFrom(schedule: WorkingTimeSchedule | null): Record<DayKey, string> {
  return Object.fromEntries(
    WEEKDAYS.map(({ key }) => [
      key,
      schedule ? formatDuration(schedule[`${key}_required_minutes`]) : "",
    ]),
  ) as Record<DayKey, string>;
}

/** The day after an ISO date. UTC arithmetic on a date-only value, so a DST
 *  change can never shift it. */
function nextDay(value: string): string {
  const next = new Date(`${value}T00:00:00Z`);
  next.setUTCDate(next.getUTCDate() + 1);
  return next.toISOString().slice(0, 10);
}

function WorkingTimeEditor({
  schedule,
  contractWeekly,
  cutoverMonth,
}: {
  /** The arrangement in force TODAY — the panel's first paint, before the
   *  target-keyed read lands. A seed, never the source of truth: the arrangement
   *  the target resolves to is what the fields are re-synced from. */
  schedule: WorkingTimeSchedule | null;
  contractWeekly: number | null;
  cutoverMonth: string | null;
}) {
  const queryClient = useQueryClient();
  const editorId = useId();
  const today = berlinToday();
  // The maintenance target IS the write's own key (`valid_from`): the panel
  // opens on the arrangement that governs now — or on the cutover month when
  // that is still in the future — and a later arrangement is reached by walking
  // the chain from the one before it.
  const cutoverDay = cutoverMonth ? `${cutoverMonth}-01` : null;
  const [target, setTarget] = useState(
    cutoverDay !== null && cutoverDay > today ? cutoverDay : today,
  );
  const dateOk = toIsoDate(target) !== null;
  const atQuery = useQuery({
    queryKey: ["profile-at", target],
    queryFn: () => fetchProfile(target),
    // A date field is a discrete input: a half-typed fragment is not a date and
    // must not fire one request per keystroke.
    enabled: dateOk,
  });
  const settled = atQuery.isSuccess;
  const resolved = settled ? atQuery.data.schedule : null;
  // `set_working_time` corrects an existing EMPLOYEE row in place ONLY on its
  // exact `valid_from`; any other target creates a new arrangement.
  const editing = resolved !== null && resolved.valid_from === target;
  // The write path refuses a date where an authoritative (MASTER/ADMIN_SEED)
  // row starts. Saying so here means the employee never meets the server's 422
  // to find it out.
  const authoritative = editing && resolved.source !== "EMPLOYEE";
  const resolvedFrom = resolved?.valid_from ?? null;

  const save = useMutation({
    mutationFn: saveWorkingTime,
    onSuccess: () => {
      // A schedule change moves required time, findings and every balance
      // derived from it, and both profile reads.
      invalidateFactDerived(queryClient);
      void queryClient.invalidateQueries({ queryKey: ["profile-at"] });
      void queryClient.invalidateQueries({ queryKey: ["profile"] });
    },
  });
  const [weeklyText, setWeeklyText] = useState(
    schedule
      ? formatDuration(schedule.weekly_required_minutes)
      : contractWeekly !== null
        ? formatDuration(contractWeekly)
        : "",
  );
  const [dayTexts, setDayTexts] = useState<Record<DayKey, string>>(() => dayTextsFrom(schedule));
  const [working, setWorking] = useState<Record<DayKey, boolean>>(() =>
    initialWorkingDays(schedule),
  );
  // The key of the arrangement the FORM currently holds. State, not a ref: the
  // gate below must be able to see it, and a query answering is not the same
  // event as the fields having been replaced (review round 3, R7).
  const [appliedKey, setAppliedKey] = useState<string>(() => scheduleKey(schedule));
  // A date the EMPLOYEE typed outranks the normalisation below: they may
  // deliberately name a day inside an arrangement's validity to start a new
  // one there. Reset when the panel re-syncs to a different arrangement.
  const [targetTyped, setTargetTyped] = useState(false);

  // Re-sync the fields whenever the target resolves to a DIFFERENT arrangement,
  // so the panel can never hold an earlier arrangement's values after the target
  // moved to a later one. Keyed on the arrangement's identity, so an unrelated
  // render (or a refetch of the same row) leaves in-progress edits alone.
  useEffect(() => {
    if (!settled) return;
    const key = scheduleKey(resolved);
    if (appliedKey === key) return;
    setAppliedKey(key);
    setTargetTyped(false);
    if (resolved) {
      setTarget(resolved.valid_from);
      setWeeklyText(formatDuration(resolved.weekly_required_minutes));
      setDayTexts(dayTextsFrom(resolved));
      setWorking(initialWorkingDays(resolved));
    } else {
      // No arrangement on that date: the employee is CREATING one. Seed from the
      // contract, never from the arrangement they just moved away from.
      setWeeklyText(contractWeekly !== null ? formatDuration(contractWeekly) : "");
      setDayTexts(dayTextsFrom(null));
      setWorking(initialWorkingDays(null));
    }
  }, [settled, resolved, contractWeekly, appliedKey]);

  // The date field names the arrangement being maintained, not an arbitrary day
  // inside its validity: once the target resolves, it IS that arrangement's own
  // `valid_from` — UNLESS the employee typed the date themselves, in which case
  // their date stands (they may be starting a new arrangement inside the current
  // one). Without that exception the field snapped every in-range date back to
  // the covering arrangement's start, so the deliberate case was unreachable and
  // the retroactive-effect note could never appear.
  useEffect(() => {
    if (resolvedFrom === null || targetTyped) return;
    setTarget((current) => (current === resolvedFrom ? current : resolvedFrom));
  }, [resolvedFrom, targetTyped]);

  const weekly = parseDuration(weeklyText);
  const days = Object.fromEntries(
    WEEKDAYS.map(({ key }) => [key, parseDuration(dayTexts[key])]),
  ) as Record<DayKey, number | null>;
  const allocated = Object.values(days).reduce<number>((sum, value) => sum + (value ?? 0), 0);
  const remaining = (weekly ?? 0) - allocated;

  const dayError = (key: DayKey): string | null => {
    const value = days[key];
    if (value === null) return "Use H:MM";
    if (value > MAX_DAY_MINUTES) return "Max 24:00 per day";
    return null;
  };
  const weeklyError = weekly === null ? "Use H:MM (e.g. 29:00)" : null;
  const sumError =
    remaining !== 0
      ? `${formatDuration(Math.abs(remaining))} ${remaining > 0 ? "remaining" : "over-allocated"} — allocate exactly your weekly target`
      : null;
  // The guard's job is to stop a REDUNDANT write to the arrangement being
  // maintained: same date AND same values. A DIFFERENT date is a new arrangement
  // even when the values match — the split changes from that date onward, which
  // is exactly what the effective date is for — so it must not be blocked. (It
  // still cannot revert a later arrangement: that would require naming the later
  // arrangement's own date, where the values do differ.)
  // The form holds the arrangement the target resolves to. False for the render
  // in which the query has answered but the sync effect has not yet run.
  const synced = settled && appliedKey === scheduleKey(resolved);
  const sameDate = resolved !== null && resolved.valid_from === target;
  const unchanged =
    sameDate &&
    weekly === resolved.weekly_required_minutes &&
    DAY_FIELDS.every((field) => (days[field.slice(0, 3) as DayKey] ?? 0) === resolved[field]);
  const canSave =
    // The target must have RESOLVED **and the fields must have been replaced with
    // that arrangement's values** before Save may act. A query answering is not
    // that event: `isSuccess` flips one committed render before the sync effect
    // runs, so gating on `settled` alone left a window in which Save was enabled
    // while the form still held the PREVIOUS arrangement's values — and pressing
    // it would write those values onto the newly named date, updating a different
    // arrangement in place (review rounds 2-3, R7).
    synced &&
    dateOk &&
    weekly !== null &&
    Object.values(days).every((value) => value !== null) &&
    Object.values(days).every((value) => (value ?? 0) <= MAX_DAY_MINUTES) &&
    remaining === 0 &&
    !authoritative &&
    !unchanged;
  // Why the primary cannot proceed, stated before it is pressed — never a
  // disabled control with its reason left to the reader (or to the server).
  const dateError = dateOk ? null : "Enter the effective date as DD.MM.YYYY.";
  const saveBlocked = !synced
    ? "Checking which arrangement that date belongs to…"
    : authoritative
      ? "A master-data arrangement starts on that date — choose another date."
      : unchanged
        ? "Nothing has changed — this arrangement already has these values."
        : null;

  const setDay = (key: DayKey, text: string) =>
    setDayTexts((current) => ({ ...current, [key]: text }));
  const toggleWorking = (key: DayKey) => {
    setWorking((current) => {
      const next = { ...current, [key]: !current[key] };
      if (!next[key]) setDay(key, "0:00");
      return next;
    });
  };
  const useRemaining = (key: DayKey) => {
    if (remaining > 0) setDay(key, formatDuration(remaining));
  };
  const distributeEvenly = () => {
    const selected = WEEKDAYS.filter(({ key }) => working[key]).map(({ key }) => key);
    if (weekly === null || selected.length === 0) return;
    const base = Math.floor(weekly / selected.length);
    const remainder = weekly % selected.length;
    selected.forEach((key, index) =>
      setDay(key, formatDuration(base + (index < remainder ? 1 : 0))),
    );
  };
  const submit = () => {
    if (!canSave || weekly === null) return;
    const payload = { valid_from: target, weekly_required_minutes: weekly } as Record<
      string,
      number | string
    >;
    for (const field of DAY_FIELDS) payload[field] = days[field.slice(0, 3) as DayKey] ?? 0;
    save.mutate(payload as Parameters<typeof saveWorkingTime>[0]);
  };

  const rangeNote =
    target < today
      ? `Takes effect retroactively from ${isoToGermanDate(target)}; dates before that keep the earlier arrangement.`
      : target > today
        ? `Takes effect on ${isoToGermanDate(target)}; the current arrangement stays until then.`
        : "Takes effect today.";

  return (
    <section className="card working-time">
      <h2>My working time</h2>
      {/* One statement of the model, always: the employee owns this arrangement,
          and it is separate from the HR contract. There is no read-only variant
          — an arrangement from master data is refused by DATE below, in words,
          rather than by turning this panel into a dead end. */}
      <p>
        Used for required-time calculations — it{" "}
        <strong>does not change your employment contract in HR</strong>.
      </p>
      {settled && (
        <p className="working-time-current">
          {editing ? "Editing the arrangement from" : "Creating a new arrangement from"}{" "}
          <strong>{isoToGermanDate(target)}</strong>
          {editing && (
            <>
              {" · "}
              <strong>{formatDuration(resolved.weekly_required_minutes)}/week</strong>{" "}
              <span className="source-badge">
                {resolved.source === "EMPLOYEE" ? "set by you" : "from master data"}
              </span>
            </>
          )}
        </p>
      )}
      {resolved?.valid_to && (
        // The chain step: a finite range is the one thing that tells the reader a
        // later arrangement exists, and `valid_to` is the day before it starts.
        <p className="working-time-note">
          This arrangement runs until {isoToGermanDate(resolved.valid_to)} — a later arrangement
          starts on {isoToGermanDate(nextDay(resolved.valid_to))}.{" "}
          <button type="button" onClick={() => setTarget(nextDay(resolved.valid_to as string))}>
            Maintain that arrangement
          </button>
        </p>
      )}
      <div className="working-time-form">
        <label>
          Weekly target{" "}
          <input
            aria-label="Weekly target"
            inputMode="numeric"
            value={weeklyText}
            onChange={(event) => setWeeklyText(event.target.value)}
            placeholder="29:00"
          />
        </label>
        <label>
          Effective from{" "}
          <DateField
            aria-label="Effective from"
            aria-describedby={dateError ? `${editorId}-date-error` : undefined}
            aria-invalid={dateError ? true : undefined}
            value={target}
            onChange={(value) => {
              setTargetTyped(true);
              setTarget(value);
            }}
          />
        </label>
        {dateError && (
          // WCAG 2.2 SC 3.3.1/1.3.1: a SIBLING of the label, associated with the
          // input it describes (text inside a wrapping label would become part of
          // the control's accessible name).
          <p className="status-error" id={`${editorId}-date-error`}>
            {dateError}
          </p>
        )}
      </div>
      <p className="working-time-note">{rangeNote}</p>
      <fieldset className="working-day-picker">
        <legend>Working days — used by Distribute evenly</legend>
        {WEEKDAYS.map(({ key, label }) => (
          <label key={key} className="checkbox-label">
            <input
              type="checkbox"
              checked={working[key]}
              onChange={() => toggleWorking(key)}
              aria-label={`${label} working day`}
            />
            {label}
          </label>
        ))}
      </fieldset>
      <div className="weekday-grid">
        {WEEKDAYS.map(({ key, label }) => {
          const message = dayError(key);
          return (
            <div className="weekday-cell" key={key}>
              <label>
                {label}{" "}
                <input
                  aria-label={`${label} required time`}
                  aria-describedby={message ? `${editorId}-${key}-error` : undefined}
                  aria-invalid={message ? true : undefined}
                  inputMode="numeric"
                  value={dayTexts[key]}
                  disabled={!working[key]}
                  onChange={(event) => setDay(key, event.target.value)}
                  placeholder="0:00"
                />
              </label>
              <div className="weekday-actions">
                {working[key] && (
                  <button
                    type="button"
                    aria-label={`Use remaining for ${label}`}
                    disabled={remaining <= 0}
                    onClick={() => useRemaining(key)}
                  >
                    Use remaining
                  </button>
                )}
              </div>
              {message && (
                // WCAG 2.2 SC 3.3.1/1.3.1: associated with the input it
                // describes, not merely adjacent to it.
                <p className="status-error weekday-error" id={`${editorId}-${key}-error`}>
                  {message}
                </p>
              )}
            </div>
          );
        })}
      </div>
      <div className="working-time-actions">
        <button type="button" onClick={distributeEvenly}>
          Distribute evenly
        </button>
        <span aria-live="polite" className="working-time-totals">
          <span>
            Weekly target <strong>{formatDuration(weekly ?? 0)}</strong>
          </span>
          <span>
            Allocated <strong>{formatDuration(allocated)}</strong>
          </span>
          <span className={remaining === 0 ? "remaining-ok" : "remaining-excess"}>
            Remaining <strong>{formatDuration(remaining)}</strong>
          </span>
        </span>
      </div>
      {!Object.values(working).some(Boolean) && (
        <p className="suggestion">Select at least one working day to distribute time.</p>
      )}
      {remaining > 0 && (
        <p className="suggestion">
          Suggested: add {formatDuration(remaining)} to any working day — use <em>Use remaining</em>{" "}
          on the day.
        </p>
      )}
      <p className="working-time-note">
        A daily target does not limit actual work — you can record a 9:00 day against a 6:00 target.
      </p>
      {sumError || weeklyError ? (
        <p className="status-error" role="alert">
          {sumError ?? weeklyError}
        </p>
      ) : null}
      {saveBlocked && (
        <p className="working-time-note" id={`${editorId}-save-blocked`}>
          {saveBlocked}
        </p>
      )}
      <button
        type="button"
        disabled={!canSave || save.isPending}
        aria-describedby={saveBlocked ? `${editorId}-save-blocked` : undefined}
        onClick={submit}
      >
        Save working time
      </button>
      {save.isPending && <p role="status">Saving…</p>}
      {save.isError && (
        // role="alert": the server refused the save and the user must know
        // without re-reading the form. Every other action error in the app is
        // announced too (travel.tsx uses role="alert", the rest role="status").
        <p className="status-error" role="alert">
          Could not save: {refusalText(save.error) || "try again"}
        </p>
      )}
      {save.isSuccess && (
        <p className="status-ok" role="status">
          Saved. Required time is{" "}
          <strong>{formatDuration(save.data.weekly_required_minutes)}/week</strong> from{" "}
          {isoToGermanDate(save.data.valid_from)}. Your HR contract is unchanged.
        </p>
      )}
    </section>
  );
}

function TemplateEditor({
  template,
  onChange,
}: {
  template: TemplateInterval[];
  onChange: (value: TemplateInterval[]) => void;
}) {
  const update = (index: number, value: Partial<TemplateInterval>) =>
    onChange(template.map((item, position) => (position === index ? { ...item, ...value } : item)));
  return (
    <div className="template-editor">
      {template.map((item, index) => (
        <div className="template-row" key={`${index}-${item.start}`}>
          <label>
            Start{" "}
            <TimeField
              aria-label={`Template ${index + 1} start`}
              value={item.start.slice(0, 5)}
              onChange={(value) => update(index, { start: value })}
            />
          </label>
          <label>
            End{" "}
            <TimeField
              aria-label={`Template ${index + 1} end`}
              value={item.end.slice(0, 5)}
              onChange={(value) => update(index, { end: value })}
            />
          </label>
          <label>
            Type{" "}
            <select
              value={item.work_type}
              onChange={(event) => update(index, { work_type: event.target.value as WorkType })}
            >
              <option value="ON_SITE">On-site</option>
              <option value="REMOTE">Remote</option>
            </select>
          </label>
          <button
            type="button"
            onClick={() => onChange(template.filter((_, position) => position !== index))}
          >
            Remove
          </button>
        </div>
      ))}
      <button
        type="button"
        onClick={() =>
          onChange([...template, { start: "09:00", end: "17:00", work_type: "ON_SITE" }])
        }
      >
        Add template interval
      </button>
    </div>
  );
}
