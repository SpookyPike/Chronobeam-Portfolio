import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useRef, useState } from "react";

import {
  createSegment,
  createTrip,
  deleteSegment,
  deleteTrip,
  fetchTravelSummary,
  fetchTravelTrips,
  updateSegment,
  type BusinessTrip,
  type TravelClassification,
  type TripStartOrigin,
} from "../lib/api";
import { ConfirmAction } from "../lib/confirm";
import { DateField, TimeField } from "../lib/inputs";
import { invalidateFactDerived } from "../lib/query";
import { refusalText } from "../lib/notice";

/** Signed 24-hour duration display; the UI never exposes raw minutes. */
export function hhmm(minutes: number): string {
  const negative = minutes < 0;
  const absolute = Math.abs(Math.round(minutes));
  const text = `${Math.floor(absolute / 60)}:${String(absolute % 60).padStart(2, "0")}`;
  return negative ? `−${text}` : text;
}

/** Calendar month (YYYY-MM) of a plain date string — TZ-safe. */
export function monthKey(day: string): string {
  return day.slice(0, 7);
}

/** "Tue, 19 May" from a plain date string (deterministic, TZ-safe). */
function dayLabel(day: string): string {
  const parts = new Intl.DateTimeFormat("en", {
    weekday: "short",
    day: "numeric",
    month: "long",
    timeZone: "UTC",
  }).formatToParts(new Date(`${day}T00:00:00Z`));
  const part = (type: string) => parts.find((item) => item.type === type)?.value ?? "";
  return `${part("weekday")}, ${part("day")} ${part("month")}`;
}

function monthLabel(month: string): string {
  return new Intl.DateTimeFormat("en", { month: "long", year: "numeric", timeZone: "UTC" }).format(
    new Date(`${month}-01T00:00:00Z`),
  );
}

function adjacentMonth(month: string, offset: number): string {
  const [year, monthNumber] = month.split("-").map(Number);
  const date = new Date(Date.UTC(year, monthNumber - 1 + offset, 1));
  return date.toISOString().slice(0, 7);
}

/** Local wall parts from an ISO datetime string (no UTC shift — the parts are
 *  already Europe/Berlin wall times). */
function wallDate(iso: string): string {
  return iso.slice(0, 10);
}
function wallTime(iso: string): string {
  return iso.slice(11, 16);
}

/** The composed local wall datetime, RANGE INCLUDED.
 *
 *  One guard, both callers: the stated requirement and the DST probe. A
 *  shape-only test let `29:00` through, and `Date.parse("…T29:00:00Z")` is
 *  `NaN`, so `Intl.DateTimeFormat.format` threw `RangeError: Invalid time
 *  value` and took the whole form down (review R2b). The range is exactly what
 *  `TimeField`'s native pattern enforces — `([01][0-9]|2[0-3]):[0-5][0-9]` —
 *  so the app states the browser's rule instead of being surprised by it. */
const WALL_LOCAL = /^\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d$/;

/** A Berlin wall time has two possible instants only in the autumn DST fold. */
function isAmbiguousBerlin(wallLocal: string): boolean {
  // Partially typed, empty or out-of-range inputs are not yet determinable (and
  // must not crash the DST probe with an Invalid-Date formatting result).
  if (!WALL_LOCAL.test(wallLocal)) return false;
  const wallAsUtc = Date.parse(`${wallLocal.slice(0, 16)}:00Z`);
  const wall = wallLocal.slice(11, 16);
  const hourAt = (milliseconds: number) =>
    new Intl.DateTimeFormat("en-GB", {
      timeZone: "Europe/Berlin",
      hour: "2-digit",
      hourCycle: "h23",
      minute: "2-digit",
    }).format(new Date(milliseconds));
  return hourAt(wallAsUtc - 7_200_000) === wall && hourAt(wallAsUtc - 3_600_000) === wall;
}

function OccurrenceChoice({
  label,
  wallLocal,
  value,
  onChange,
}: {
  label: string;
  wallLocal: string;
  value: 0 | 1 | null;
  onChange: (fold: 0 | 1 | null) => void;
}) {
  if (!isAmbiguousBerlin(wallLocal)) return null;
  return (
    <label className="fold-choice">
      Occurrence for {label}
      <select
        aria-label={`Occurrence for ${label}`}
        value={value ?? ""}
        onChange={(event) =>
          onChange(event.target.value === "" ? null : event.target.value === "1" ? 1 : 0)
        }
      >
        <option value="" disabled>
          Choose occurrence…
        </option>
        <option value={0}>First — summer time (CEST)</option>
        <option value={1}>Second — winter time (CET)</option>
      </select>
    </label>
  );
}

const classifications: Array<[TravelClassification, string]> = [
  ["PASSIVE_TRAVEL", "Passive travel"],
  ["ACTIVE_WORK", "Active work"],
  ["DRIVING", "Driving"],
];

function classificationLabel(value: TravelClassification): string {
  return classifications.find(([key]) => key === value)?.[1] ?? value;
}

function outcomeText(outcome: { ok: boolean; message: string }): string {
  return outcome.message;
}

/** The first unmet requirement of a start/end pair, in the form's own words —
 *  `null` when the pair can be submitted.
 *
 *  A disabled primary that states nothing is the acceptance class this closes
 *  (E3/C2/B4): the ordering rule and the DST fold already blocked the action,
 *  but only an unassociated hint said why, and a form whose only explanation is
 *  a disabled button tells the employee nothing. The rule is stated here and the
 *  primary points at it; native validation stays as the backstop, never as the
 *  only voice. */
function pairRequirement(input: {
  noun: string;
  start: string;
  end: string;
  startWall: string;
  endWall: string;
  startAmbiguous: boolean;
  endAmbiguous: boolean;
  startFold: 0 | 1 | null;
  endFold: 0 | 1 | null;
}): string | null {
  // A PARTIAL value is as blocking as an empty one: the field's native `pattern`
  // would otherwise refuse the submit silently, leaving an enabled button that
  // does nothing (acceptance E3, review R2). `startWall`/`endWall` are the
  // composed local datetimes, so one test covers a partial date and a partial
  // time together.
  // `WALL_LOCAL` is the same range-aware guard the DST probe uses, so the
  // stated rule and the crash-free probe can never drift apart.
  // The date's own range needs no rule here: `DateField`'s pattern only fixes
  // the shape, so an impossible date reaches the server and comes back as a
  // named field rejection.
  const COMPLETE = WALL_LOCAL;
  if (
    input.start === "" ||
    input.end === "" ||
    !COMPLETE.test(input.startWall) ||
    !COMPLETE.test(input.endWall)
  )
    return `Enter the ${input.noun}'s start and end date and time.`;
  if (input.startWall >= input.endWall) return `The ${input.noun} end must be after its start.`;
  if (input.startAmbiguous && input.startFold === null)
    return `The ${input.noun} start exists twice on the clock-change day — choose which occurrence you mean.`;
  if (input.endAmbiguous && input.endFold === null)
    return `The ${input.noun} end exists twice on the clock-change day — choose which occurrence you mean.`;
  return null;
}

function TripForm({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [start, setStart] = useState("");
  const [startTime, setStartTime] = useState("08:00");
  const [end, setEnd] = useState("");
  const [endTime, setEndTime] = useState("18:00");
  const [startFold, setStartFold] = useState<0 | 1 | null>(null);
  const [endFold, setEndFold] = useState<0 | 1 | null>(null);
  const [origin, setOrigin] = useState<TripStartOrigin>("WORKPLACE");
  const [label, setLabel] = useState("");
  // Stable command id for the life of the form: a double-click replays into
  // the server's idempotency guard instead of creating a duplicate trip.
  const commandId = useRef(crypto.randomUUID());
  const create = useMutation({
    mutationFn: () =>
      createTrip({
        start_local: `${start}T${startTime}:00`,
        end_local: `${end}T${endTime}:00`,
        start_origin: origin,
        reference_label: label.trim() ? label.trim() : null,
        ...(startFold === null ? {} : { start_fold: startFold }),
        ...(endFold === null ? {} : { end_fold: endFold }),
        client_request_id: commandId.current,
      }),
    onSuccess: () => {
      onCreated();
      onClose();
    },
  });
  const startWall = `${start}T${startTime}`;
  const endWall = `${end}T${endTime}`;
  const startAmbiguous = start !== "" && isAmbiguousBerlin(startWall);
  const endAmbiguous = end !== "" && isAmbiguousBerlin(endWall);
  const requirement = pairRequirement({
    noun: "trip",
    start,
    end,
    startWall,
    endWall,
    startAmbiguous,
    endAmbiguous,
    startFold,
    endFold,
  });
  // `valid` IS the requirement: one computation, so the button's gate and the
  // sentence beside it can never disagree.
  const valid = requirement === null;
  const unordered = start !== "" && end !== "" && startWall >= endWall;
  const startInvalid = start === "" || unordered;
  const endInvalid = end === "" || unordered;
  return (
    <form
      className="travel-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (valid) create.mutate();
      }}
    >
      <h3>New business trip</h3>
      <div className="travel-fields">
        <label>
          Starts
          <DateField
            aria-label="Trip start date"
            aria-invalid={startInvalid}
            value={start}
            onChange={setStart}
          />
          <TimeField
            aria-label="Trip start time"
            aria-invalid={startInvalid}
            value={startTime}
            onChange={setStartTime}
          />
          <OccurrenceChoice
            label="trip start"
            wallLocal={startWall}
            value={startFold}
            onChange={setStartFold}
          />
        </label>
        <label>
          Ends
          <DateField
            aria-label="Trip end date"
            aria-invalid={endInvalid}
            value={end}
            onChange={setEnd}
          />
          <TimeField
            aria-label="Trip end time"
            aria-invalid={endInvalid}
            value={endTime}
            onChange={setEndTime}
          />
          <OccurrenceChoice
            label="trip end"
            wallLocal={endWall}
            value={endFold}
            onChange={setEndFold}
          />
        </label>
        <label>
          Journey begins
          <select
            aria-label="Trip start origin"
            value={origin}
            onChange={(event) => setOrigin(event.target.value as TripStartOrigin)}
          >
            <option value="WORKPLACE">At the workplace (commute not counted)</option>
            <option value="HOME">Directly from home (counts from leaving home)</option>
          </select>
        </label>
        <label className="travel-wide-field">
          Reference (optional)
          <input
            value={label}
            maxLength={200}
            placeholder="e.g. BESSY II beamtime"
            onChange={(event) => setLabel(event.target.value)}
          />
        </label>
      </div>
      {create.isError && (
        <p className="status-error" role="alert">
          {refusalText(create.error) || "Could not create trip."}
        </p>
      )}
      <div className="travel-actions">
        <button
          type="submit"
          disabled={!valid || create.isPending}
          aria-describedby={valid ? undefined : "trip-requirement"}
        >
          Create trip
        </button>
        <button type="button" onClick={onClose}>
          Cancel
        </button>
      </div>
      {requirement && (
        <p className="field-requirement" id="trip-requirement" role="status">
          {requirement}
        </p>
      )}
    </form>
  );
}

function SegmentForm({
  trip,
  onClose,
  onSaved,
}: {
  trip: BusinessTrip;
  onClose: () => void;
  onSaved: (message: string) => void;
}) {
  const [start, setStart] = useState(wallDate(trip.start_local));
  const [startTime, setStartTime] = useState(wallTime(trip.start_local));
  const [end, setEnd] = useState(wallDate(trip.end_local));
  const [endTime, setEndTime] = useState(wallTime(trip.end_local));
  const [classification, setClassification] = useState<TravelClassification>("PASSIVE_TRAVEL");
  const [fictitious, setFictitious] = useState(false);
  const [startFold, setStartFold] = useState<0 | 1 | null>(null);
  const [endFold, setEndFold] = useState<0 | 1 | null>(null);
  const commandId = useRef(crypto.randomUUID());
  const create = useMutation({
    mutationFn: () =>
      createSegment(trip.id, {
        start_local: `${start}T${startTime}:00`,
        end_local: `${end}T${endTime}:00`,
        classification,
        basis:
          classification === "PASSIVE_TRAVEL" && fictitious
            ? "FICTITIOUS_BUSINESS_ROUTE"
            : "ACTUAL",
        ...(startFold === null ? {} : { start_fold: startFold }),
        ...(endFold === null ? {} : { end_fold: endFold }),
        client_request_id: commandId.current,
      }),
    onSuccess: (segment) => {
      onSaved(
        segment.work_interval_id
          ? `Segment recorded — ${classificationLabel(segment.classification)} is actual working time and appears in the ledger.`
          : "Passive travel recorded. It is not working time.",
      );
      onClose();
    },
  });
  const startWall = `${start}T${startTime}`;
  const endWall = `${end}T${endTime}`;
  const startAmbiguous = start !== "" && isAmbiguousBerlin(startWall);
  const endAmbiguous = end !== "" && isAmbiguousBerlin(endWall);
  const requirement = pairRequirement({
    noun: "segment",
    start,
    end,
    startWall,
    endWall,
    startAmbiguous,
    endAmbiguous,
    startFold,
    endFold,
  });
  const valid = requirement === null;
  const unordered = start !== "" && end !== "" && startWall >= endWall;
  const startInvalid = start === "" || unordered;
  const endInvalid = end === "" || unordered;
  return (
    <form
      className="travel-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (valid) create.mutate();
      }}
    >
      <h3>Add travel segment</h3>
      <div className="travel-fields">
        <label>
          From
          <DateField
            aria-label="Segment start date"
            aria-invalid={startInvalid}
            value={start}
            onChange={setStart}
          />
          <TimeField
            aria-label="Segment start time"
            aria-invalid={startInvalid}
            value={startTime}
            onChange={setStartTime}
          />
          <OccurrenceChoice
            label="segment start"
            wallLocal={startWall}
            value={startFold}
            onChange={setStartFold}
          />
        </label>
        <label>
          To
          <DateField
            aria-label="Segment end date"
            aria-invalid={endInvalid}
            value={end}
            onChange={setEnd}
          />
          <TimeField
            aria-label="Segment end time"
            aria-invalid={endInvalid}
            value={endTime}
            onChange={setEndTime}
          />
          <OccurrenceChoice
            label="segment end"
            wallLocal={endWall}
            value={endFold}
            onChange={setEndFold}
          />
        </label>
        <label>
          Classification
          <select
            aria-label="Segment classification"
            value={classification}
            onChange={(event) => setClassification(event.target.value as TravelClassification)}
          >
            {classifications.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
      </div>
      {classification === "PASSIVE_TRAVEL" ? (
        <label className="travel-checkbox">
          <input
            type="checkbox"
            aria-label="Fictitious business route"
            checked={fictitious}
            onChange={(event) => setFictitious(event.target.checked)}
          />
          Only the route a business-only journey would have taken (personal extension not counted)
        </label>
      ) : (
        <p className="travel-note">
          This segment creates an actual work interval (business-trip work) in your regular ledger —
          it is never double-counted as travel credit.
        </p>
      )}
      <p className="travel-note">
        A journey crossing midnight ends after 00:00 on the next day — set the end date accordingly.
        Waiting, transfers and delays are passive travel.
      </p>
      {create.isError && (
        <p className="status-error" role="alert">
          {refusalText(create.error) || "Could not add segment."}
        </p>
      )}
      <div className="travel-actions">
        <button
          type="submit"
          disabled={!valid || create.isPending}
          aria-describedby={valid ? undefined : "segment-requirement"}
        >
          Add segment
        </button>
        <button type="button" onClick={onClose}>
          Cancel
        </button>
      </div>
      {requirement && (
        <p className="field-requirement" id="segment-requirement" role="status">
          {requirement}
        </p>
      )}
    </form>
  );
}

function SegmentRow({
  trip,
  segmentId,
  summary,
  onClosed,
}: {
  trip: BusinessTrip;
  segmentId: string;
  summary: (message: string) => void;
  onClosed: () => void;
}) {
  const segment = trip.segments.find((item) => item.id === segmentId);
  const [start, setStart] = useState(segment ? wallDate(segment.start_local) : "");
  const [startTime, setStartTime] = useState(segment ? wallTime(segment.start_local) : "");
  const [end, setEnd] = useState(segment ? wallDate(segment.end_local) : "");
  const [endTime, setEndTime] = useState(segment ? wallTime(segment.end_local) : "");
  const [classification, setClassification] = useState<TravelClassification>(
    segment?.classification ?? "PASSIVE_TRAVEL",
  );
  const [reason, setReason] = useState("");
  const [startFold, setStartFold] = useState<0 | 1 | null>(null);
  const [endFold, setEndFold] = useState<0 | 1 | null>(null);
  if (!segment) return null;
  const patch = useMutation({
    mutationFn: () =>
      updateSegment(segment.id, {
        version: segment.version,
        reason,
        start_local: `${start}T${startTime}:00`,
        end_local: `${end}T${endTime}:00`,
        classification,
        ...(startFold === null ? {} : { start_fold: startFold }),
        ...(endFold === null ? {} : { end_fold: endFold }),
      }),
    onSuccess: () => {
      summary(
        "Segment corrected; all travel calculations were re-derived from your current facts.",
      );
      onClosed();
    },
  });
  const remove = useMutation({
    mutationFn: () => deleteSegment(segment.id, segment.version),
    onSuccess: () => {
      summary("Segment removed; travel calculations re-derived.");
      onClosed();
    },
  });
  const startWall = `${start}T${startTime}`;
  const endWall = `${end}T${endTime}`;
  const startAmbiguous = start !== "" && isAmbiguousBerlin(startWall);
  const endAmbiguous = end !== "" && isAmbiguousBerlin(endWall);
  // The facts first, then the correction's own bookkeeping: both are stated
  // rather than left to a disabled button.
  const wallRequirement = pairRequirement({
    noun: "segment",
    start,
    end,
    startWall,
    endWall,
    startAmbiguous,
    endAmbiguous,
    startFold,
    endFold,
  });
  const reasonMissing = reason.trim() === "";
  const requirement =
    wallRequirement ?? (reasonMissing ? "Enter a reason for this correction." : null);
  const valid = requirement === null;
  const unordered = start !== "" && end !== "" && startWall >= endWall;
  const startInvalid = start === "" || unordered;
  const endInvalid = end === "" || unordered;
  return (
    <form
      className="travel-form"
      onSubmit={(event) => {
        event.preventDefault();
        if (valid) patch.mutate();
      }}
    >
      <h3>Correct segment · v{segment.version}</h3>
      <div className="travel-fields">
        <label>
          From
          <DateField
            aria-label="Corrected segment start date"
            aria-invalid={startInvalid}
            value={start}
            onChange={setStart}
          />
          <TimeField
            aria-label="Corrected segment start time"
            aria-invalid={startInvalid}
            value={startTime}
            onChange={setStartTime}
          />
          <OccurrenceChoice
            label="corrected segment start"
            wallLocal={startWall}
            value={startFold}
            onChange={setStartFold}
          />
        </label>
        <label>
          To
          <DateField
            aria-label="Corrected segment end date"
            aria-invalid={endInvalid}
            value={end}
            onChange={setEnd}
          />
          <TimeField
            aria-label="Corrected segment end time"
            aria-invalid={endInvalid}
            value={endTime}
            onChange={setEndTime}
          />
          <OccurrenceChoice
            label="corrected segment end"
            wallLocal={endWall}
            value={endFold}
            onChange={setEndFold}
          />
        </label>
        <label>
          Classification
          <select
            aria-label="Corrected classification"
            value={classification}
            onChange={(event) => setClassification(event.target.value as TravelClassification)}
          >
            {classifications.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label className="travel-wide-field">
          Reason (required)
          <input
            value={reason}
            maxLength={500}
            aria-invalid={wallRequirement === null && reasonMissing}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
      </div>
      {patch.isError && (
        <p className="status-error" role="alert">
          {refusalText(patch.error) || "Could not correct segment."}
        </p>
      )}
      <div className="travel-actions">
        <button
          type="submit"
          disabled={!valid || patch.isPending}
          aria-describedby={valid ? undefined : "correction-requirement"}
        >
          Save correction
        </button>
        <button
          type="button"
          className="danger-ghost"
          disabled={remove.isPending}
          onClick={() => remove.mutate()}
        >
          Delete segment
        </button>
        <button type="button" onClick={onClosed}>
          Cancel
        </button>
      </div>
      {requirement && (
        <p className="field-requirement" id="correction-requirement" role="status">
          {requirement}
        </p>
      )}
    </form>
  );
}

export function TravelPage({ period }: { period: string }) {
  const queryClient = useQueryClient();
  const trips = useQuery({ queryKey: ["travel-trips"], queryFn: fetchTravelTrips });
  const summary = useQuery({
    queryKey: ["travel-summary", period],
    queryFn: () => fetchTravelSummary(period),
    placeholderData: keepPreviousData,
  });
  const [addingTrip, setAddingTrip] = useState(false);
  const [addingSegmentFor, setAddingSegmentFor] = useState<string | null>(null);
  const [correctingSegment, setCorrectingSegment] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  // A trip or a journey leg is a fact the month derives from: the travel
  // summary, the trip list, the month balance and its findings all refresh
  // together.
  const refresh = () => invalidateFactDerived(queryClient);
  const invalidateTrip = (message: string) => {
    setNotice(message);
    refresh();
  };
  const deleteTripMutation = useMutation({
    mutationFn: (trip: BusinessTrip) => deleteTrip(trip.id, trip.version),
    onSuccess: () => invalidateTrip("Trip removed."),
  });
  // keepPreviousData preserves loading continuity. Do not render those old
  // values underneath a newly selected month heading (F7).
  // Backend serializes period_month as a full date ("2026-07-01"); the route
  // param is "YYYY-MM". Compare on the month prefix so both shapes match.
  const currentSummary =
    summary.data?.period_month?.slice(0, 7) === period ? summary.data : undefined;
  const monthTrips = new Map((currentSummary?.trips ?? []).map((item) => [item.trip_id, item]));
  // The list shows the SELECTED month's trips. `/me/travel-trips` takes no range
  // and returns every trip the employee has ever recorded, so the month-scoped
  // summary is what decides membership — without this, a July trip sat under a
  // November heading and no month could ever look empty.
  // While the summary is still answering for another period it says NOTHING about
  // this month's membership. Falling back to every lifetime trip put out-of-month
  // trips under the newly selected month's heading — where the employee could act
  // on them, e.g. delete a trip belonging to another month (review round 2,
  // Major 2). Blank rather than wrong, and say why the list is empty.
  const monthTripList = currentSummary
    ? (trips.data ?? []).filter((trip) => monthTrips.has(trip.id))
    : [];
  const tripsResolved = !trips.isLoading && !trips.isError && currentSummary !== undefined;
  // PENDING and UNAVAILABLE are different states and must not share one
  // message: a summary that has finished but answers for another period (or
  // failed) is not still loading, and leaving "Loading…" up forever hid both
  // the trips and the Add action behind a spinner that never resolved
  // (review round 3, Medium).
  const summaryPending = summary.isPending;
  const summaryUnavailable = !summary.isPending && currentSummary === undefined;
  // 9.1: the empty state is a card, not bare prose, and the card carries the
  // same add action the populated branch does.
  const isEmpty = tripsResolved && monthTripList.length === 0;
  const addTripAction = addingTrip ? (
    <TripForm
      onClose={() => setAddingTrip(false)}
      onCreated={() => invalidateTrip("Business trip created.")}
    />
  ) : (
    <button type="button" className="add-interval" onClick={() => setAddingTrip(true)}>
      Add business trip
    </button>
  );
  return (
    <section className="travel-page" aria-label={`Business travel ${monthLabel(period)}`}>
      <p className="eyebrow">Private — visible only to you</p>
      <h1>Business travel</h1>
      <div className="travel-month-nav">
        <Link
          to="/travel/$period"
          params={{ period: adjacentMonth(period, -1) }}
          aria-label="Previous month"
        >
          ← Previous month
        </Link>
        <strong>{monthLabel(period)}</strong>
        <Link
          to="/travel/$period"
          params={{ period: adjacentMonth(period, 1) }}
          aria-label="Next month"
        >
          Next month →
        </Link>
      </div>
      <p className="travel-note">
        Driving and actual work during travel are working time; passive travel is not.
      </p>
      {notice && (
        <p className="status-ok" role="status">
          {notice}
        </p>
      )}
      {currentSummary && currentSummary.findings.length > 0 && (
        <ul className="travel-findings" aria-label="Travel findings">
          {currentSummary.findings.map((finding, index) => (
            <li
              key={`${finding.rule_id}-${index}`}
              className={`travel-finding travel-finding-${finding.severity.toLowerCase()}`}
            >
              {finding.date ? <strong>{dayLabel(finding.date)}</strong> : null}
              {finding.date ? " — " : ""}
              {finding.message}
            </li>
          ))}
        </ul>
      )}
      {trips.isLoading && <p role="status">Loading trips…</p>}
      {trips.isError && (
        <p className="status-error" role="alert">
          Could not load travel trips.
        </p>
      )}
      <div className="travel-trip-list">
        {isEmpty && (
          <section className="standalone-card">
            <p className="empty-state">
              No trips recorded this month. Trips, their driving and passive segments, and the
              non-credited travel window appear here.
            </p>
            {addTripAction}
          </section>
        )}
        {summaryPending && (
          <p role="status" className="travel-note">
            Loading this month's trips…
          </p>
        )}
        {summaryUnavailable && (
          <p className="status-error" role="alert">
            This month's travel summary could not be read, so the month's trips cannot be listed
            here. Your recorded trips are unchanged.
          </p>
        )}
        {monthTripList.map((trip) => {
          const window = monthTrips.get(trip.id);
          const remaining = window ? window.threshold_minutes - window.eligible_minutes : null;
          return (
            <article
              className="travel-trip-card"
              key={trip.id}
              aria-label={`Business trip from ${wallDate(trip.start_local)}`}
            >
              <header>
                <h2>
                  Business trip · {dayLabel(wallDate(trip.start_local))} →{" "}
                  {dayLabel(wallDate(trip.end_local))}
                </h2>
                <span className="source-badge">employee-declared</span>
                {trip.reference_label && (
                  <span className="travel-label">{trip.reference_label}</span>
                )}
                <span className="travel-origin">
                  starts {trip.start_origin === "HOME" ? "from home" : "at the workplace"}
                </span>
              </header>
              {window && (
                // UI-026 class: a name on a role-less `div` is dropped by
                // assistive technology. A named section is a real region, and
                // the file already names its other blocks this way.
                <section
                  className="travel-threshold"
                  aria-label="Non-credited travel assessment window"
                >
                  <p className="travel-threshold-heading">
                    Travel credit window · {dayLabel(window.window_start)} →{" "}
                    {dayLabel(window.window_end)}
                  </p>
                  {/* The threshold figures below are the same three numbers, so the
                      running line is only worth showing while they are still short
                      of the threshold. */}
                  {window.excess_minutes === 0 && (
                    <p className="travel-progress" role="status">
                      {hhmm(window.eligible_minutes)} / {hhmm(window.threshold_minutes)} threshold —{" "}
                      {hhmm(Math.max(remaining ?? 0, 0))} remaining
                    </p>
                  )}
                  {window.excess_minutes > 0 && (
                    <dl className="travel-credit-calc">
                      <div>
                        <dt>Eligible non-credited travel</dt>
                        <dd>{hhmm(window.eligible_minutes)}</dd>
                      </div>
                      <div>
                        <dt>Threshold</dt>
                        <dd>{hhmm(window.threshold_minutes)}</dd>
                      </div>
                      <div>
                        <dt>Excess</dt>
                        <dd>{hhmm(window.excess_minutes)}</dd>
                      </div>
                      <div>
                        <dt>25% time credit</dt>
                        <dd className="travel-credit-value">{hhmm(window.credit_minutes)}</dd>
                      </div>
                      <div>
                        <dt>Credit added to balance</dt>
                        <dd>{dayLabel(window.credit_effective_on)}</dd>
                      </div>
                    </dl>
                  )}
                </section>
              )}
              <ul className="travel-segments" aria-label="Travel segments">
                {trip.segments.length === 0 && (
                  <li className="travel-empty">No segments yet — add the journey legs below.</li>
                )}
                {[...trip.segments]
                  .sort((a, b) => a.start_local.localeCompare(b.start_local))
                  .map((segment) => (
                    <li key={segment.id} className="travel-segment">
                      <span className="travel-segment-day">
                        {dayLabel(wallDate(segment.start_local))}
                      </span>
                      <span className="travel-segment-time">
                        {wallTime(segment.start_local)}–{wallTime(segment.end_local)}
                        {/* Same hint the interval rows carry: a leg that runs past
                            midnight does not end on the day named above it. */}
                        {wallDate(segment.start_local) !== wallDate(segment.end_local) && (
                          <span className="next-day">
                            {" "}
                            · ends {dayLabel(wallDate(segment.end_local))}
                          </span>
                        )}
                      </span>
                      <span
                        className={`work-type-chip travel-chip-${segment.classification.toLowerCase()}`}
                      >
                        {classificationLabel(segment.classification)}
                      </span>
                      <span className="travel-duration">
                        {hhmm(
                          (Date.parse(segment.end_local) - Date.parse(segment.start_local)) / 60000,
                        )}
                      </span>
                      <span className="travel-qualifier">
                        {segment.classification === "PASSIVE_TRAVEL"
                          ? segment.basis === "FICTITIOUS_BUSINESS_ROUTE"
                            ? "business-route (personal extension excluded)"
                            : "travel time, not working time"
                          : "working time in your ledger"}
                      </span>
                      {segment.work_interval_id && (
                        <Link
                          to="/month/$month"
                          params={{ month: segment.start_local.slice(0, 7) }}
                          className="travel-linked"
                        >
                          linked work
                        </Link>
                      )}
                      <button
                        type="button"
                        className="quick-action"
                        onClick={() => setCorrectingSegment(segment.id)}
                      >
                        Correct
                      </button>
                    </li>
                  ))}
              </ul>
              {addingSegmentFor === trip.id ? (
                <SegmentForm
                  trip={trip}
                  onClose={() => setAddingSegmentFor(null)}
                  onSaved={invalidateTrip}
                />
              ) : (
                <button
                  type="button"
                  className="quick-action"
                  onClick={() => setAddingSegmentFor(trip.id)}
                >
                  Add segment
                </button>
              )}
              {correctingSegment && trip.segments.some((item) => item.id === correctingSegment) && (
                <SegmentRow
                  trip={trip}
                  segmentId={correctingSegment}
                  summary={invalidateTrip}
                  onClosed={() => setCorrectingSegment(null)}
                />
              )}
              <ConfirmAction
                label="Delete trip"
                confirmLabel="Delete trip"
                className="danger-ghost"
                disabled={trip.segments.length > 0}
                busy={deleteTripMutation.isPending}
                title={trip.segments.length > 0 ? "Remove the segments first" : undefined}
                onConfirm={() => deleteTripMutation.mutate(trip)}
              />
              {deleteTripMutation.isError && (
                <p className="status-error" role="alert">
                  {outcomeText({ ok: false, message: "Could not delete the trip." })}
                </p>
              )}
            </article>
          );
        })}
      </div>
      {!isEmpty && addTripAction}
      {currentSummary && currentSummary.topups.filter((topup) => topup.minutes > 0).length > 0 && (
        <section className="card travel-topups" aria-label="Travel day top-ups this month">
          <h2>Travel-day credit · {monthLabel(period)}</h2>
          <p className="travel-note">
            Passive travel filled these days up to your required time. A derived balance credit —
            not working time, not an absence entitlement.
          </p>
          <ul>
            {currentSummary.topups.map((topup) => (
              <li key={topup.day}>
                <span className="travel-credit-day">{dayLabel(topup.day)}</span>
                <span className="travel-credit-amount">+{hhmm(topup.minutes)} credited</span>
              </li>
            ))}
          </ul>
        </section>
      )}
      {currentSummary && currentSummary.credits.length > 0 && (
        <section className="card travel-credits" aria-label="Generated travel credits">
          <h2>Generated travel credit · {monthLabel(period)}</h2>
          <p className="travel-note">Derived from the excess travel below by hand.</p>
          <ul>
            {currentSummary.credits.map((credit) => (
              <li key={`${credit.source_entity_id}-${credit.occurred_on}`}>
                <span className="travel-credit-day">{dayLabel(credit.occurred_on)}</span>
                <span className="travel-credit-amount">+{hhmm(credit.minutes)}</span>
                <span className="travel-credit-basis">
                  {hhmm(credit.excess_minutes)} over {hhmm(credit.threshold_minutes)} non-credited
                  travel
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </section>
  );
}
