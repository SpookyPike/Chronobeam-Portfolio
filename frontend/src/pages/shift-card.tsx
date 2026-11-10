import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import {
  confirmShift,
  createBreak,
  editShiftPlan,
  fetchShiftReportPreview,
  saveShiftCancellation,
  saveShiftDeviation,
  withdrawShiftPlan,
  type Shift,
  type ShiftReportPreview,
} from "../lib/api";
import { berlinInstant, berlinLocalDateTime } from "../lib/format";
import { MomentField, toIsoDateTime } from "../lib/inputs";
import { fieldIssueFor } from "../lib/api";
import { ActionNotice, errorNotice, FieldIssue, okNotice, type Notice } from "../lib/notice";
import { ConfirmAction } from "../lib/confirm";
import { OverflowMenu } from "../lib/menu";
import {
  compensationReviewLabel,
  decisionMakerRoleFieldLabel,
  decisionMakerRoleOther,
  decisionMakerRoles,
  deviationTypeLabel,
  planSourceLabel,
  shiftKindLabel,
  shiftKindLabels,
} from "../lib/labels";
import { invalidateFactDerived } from "../lib/query";

const deviationTypes = [
  "CANCELLED",
  // The Company cutting a shift short AFTER it started (D13, POL-02). A
  // distinct value on purpose: `save_deviation` deletes every linked actual
  // interval for a CANCELLED deviation, which would erase the partial work the
  // entitlement is calculated from, and SHORTENED would under-claim.
  "ABORTED_BY_COMPANY",
  "RESCHEDULED",
  "ADDITIONAL",
  "SWAPPED",
  "SHORTENED",
  "OVERRUN",
  "OVERRUN_GT_10H",
  "MINOR_CHANGE",
  "CANCELLED_SICKNESS",
];

/** SHIFT-003 paid shift-break requirement, in employee-facing words. */
const paidBreakLabels: Record<Shift["paid_break_status"], string> = {
  EXPLICIT_FACT_REQUIRED: "Not recorded yet",
  RECORDED: "Recorded",
};

/** The copy for the three non-active statuses (plan §6.2.1).
 *
 *  All three are READ-ONLY, but they are not synonyms and must not share a
 *  sentence: `WITHDRAWN` is the employee's own retraction of a mistaken entry
 *  and carries NO entitlement; `CANCELLED` is the operational Company event,
 *  which PRESERVES the plan and may carry allowances; `SUPERSEDED` is lineage.
 *  Labelling a withdrawal as a cancellation (or the reverse) would misreport the
 *  employee's own record back to them, so each branch returns its own string and
 *  `null` means "this IS an active plan".
 *
 *  A `switch` rather than a `Record<Status, string>`: the compiler then proves
 *  every non-active member is covered, and a future status cannot silently
 *  inherit another's words. */
function nonActiveCopy(status: Shift["status"]): string | null {
  if (status === "WITHDRAWN") return "Withdrawn";
  if (status === "CANCELLED") return "Cancelled";
  if (status === "SUPERSEDED") return "Superseded by a later revision";
  return null;
}

/** Why an ACTIVE plan can no longer be changed (D3), and what to do instead.
 *
 *  §6.5: a frozen plan shows the REASON and the correct action — never a
 *  disabled button (`overtime.tsx:92-95`: a control that can only fail tells the
 *  employee nothing). The sentence names the actual cause rather than assuming
 *  it was recorded time, because the freeze has three separate triggers. */
function frozenPlanReason(shift: Shift): string {
  if (shift.actual_start_at)
    return "This shift already has recorded actual time. Record a deviation instead.";
  if (shift.deviation)
    return "A deviation is already recorded for this shift, so the plan can no longer be changed.";
  return "A paid shift break is already recorded for this shift, so the plan can no longer be changed.";
}

function visibleTime(instant: string | null): string {
  return berlinInstant(instant);
}

/** The DST fold (0 = first occurrence / CEST, 1 = second / CET) that reproduces a
 *  STORED aware instant for a Berlin wall time.
 *
 *  A plan whose window sits in the fall-back hour is otherwise unsaveable: the
 *  server refuses an ambiguous wall time without an explicit fold rather than
 *  guessing one (docs/ARCHITECTURE.md), so an untouched field would be rejected for a value
 *  the employee never changed. The same four lines exist in `month.tsx`; they are
 *  module-local there and a page must not import from another page, so the copy
 *  lives here — the duplication `isAmbiguousBerlin` already has between
 *  `month.tsx` and `travel.tsx`.
 *
 *  Sign: for Europe/Berlin the wall clock is AHEAD of the instant, so the second
 *  occurrence of the fall-back hour (CET, +01:00) is a −60 minute difference and
 *  the first (CEST, +02:00) is −120. The result is therefore the offset the stored
 *  instant already has, which is exactly what an untouched field must write back;
 *  outside the ambiguous hour the server ignores the fold, so the value is inert
 *  there. */
function foldFor(instantIso: string, wallLocal: string): 0 | 1 {
  const instant = Date.parse(instantIso);
  const wallAsUtc = Date.parse(`${wallLocal.slice(0, 16)}:00Z`);
  return Math.round((instant - wallAsUtc) / 60000) === -60 ? 1 : 0;
}

export function ShiftPlanActualCard({
  shift,
  onFactsChanged,
}: {
  shift: Shift;
  /** A shift write moves the facts the MONTH page is drawn from, so the page's
   *  normal-week preview (a claim about which days are still eligible) must be
   *  dropped in the same tick, not after a refetch (review R6). */
  onFactsChanged?: () => void;
}) {
  const queryClient = useQueryClient();
  const [showDeviation, setShowDeviation] = useState(Boolean(shift.deviation));
  const [preview, setPreview] = useState<ShiftReportPreview | null>(null);
  const [deviationType, setDeviationType] = useState(
    shift.deviation?.deviation_type ?? "SHORTENED",
  );
  const [actualStart, setActualStart] = useState(
    shift.actual_start_at
      ? berlinLocalDateTime(shift.actual_start_at)
      : berlinLocalDateTime(shift.approved_revision.planned_start_at),
  );
  const [actualEnd, setActualEnd] = useState(
    shift.actual_end_at
      ? berlinLocalDateTime(shift.actual_end_at)
      : berlinLocalDateTime(shift.approved_revision.planned_end_at),
  );
  const [decisionTime, setDecisionTime] = useState(
    shift.deviation?.noticed_or_decided_at
      ? berlinLocalDateTime(shift.deviation.noticed_or_decided_at)
      : "",
  );
  const [maker, setMaker] = useState(shift.deviation?.decision_maker_type ?? "");
  const [reason, setReason] = useState(shift.deviation?.reason ?? "");
  const [showBreak, setShowBreak] = useState(false);
  // Break timing is never inferred: the fields start empty and must be filled
  // with the confirmed factual break.
  const [breakStart, setBreakStart] = useState("");
  const [breakEnd, setBreakEnd] = useState("");
  // --- The cancellation form (§6.3, D11/D22) -------------------------------
  const [showCancellation, setShowCancellation] = useState(false);
  // The reported instant is prompted FIRST because it is the entitlement-
  // relevant input (SHIFT-009). The dropdown starts on the escape hatch: any of
  // the four named roles would be an authority claim the employee has not made
  // yet, and `decision_maker_type` is stored unvalidated (SHIFT-013).
  const [cancelNoticedAt, setCancelNoticedAt] = useState("");
  const [cancelRole, setCancelRole] = useState<string>(decisionMakerRoleOther);
  const [cancelRoleText, setCancelRoleText] = useState("");
  const [cancelReason, setCancelReason] = useState("");
  // --- The plan-window correction (§6.5, docs/INVARIANTS.md) ------------------------
  // The window is stated WHOLE rather than as a delta: the fields are filled from
  // the row when the form OPENS, so an untouched field writes back the value the
  // plan already had, and a form reopened after a correction shows the corrected
  // row rather than the values it replaced.
  const [showCorrection, setShowCorrection] = useState(false);
  const [plannedStart, setPlannedStart] = useState("");
  const [plannedEnd, setPlannedEnd] = useState("");
  const [plannedKind, setPlannedKind] = useState("SHIFT");
  const [notice, setNotice] = useState<Notice>(null);
  // Confirming or deviating a shift writes the actual work fact, so the day
  // panel, the calendar cell and the month balance all change with it — not just
  // this card's own list.
  const refresh = () => invalidateFactDerived(queryClient);
  const confirmMutation = useMutation({
    mutationFn: () => confirmShift(shift.id, shift.approved_revision.id),
    onSuccess: () => {
      setPreview(null);
      refresh();
      onFactsChanged?.();
      setNotice(okNotice("Shift confirmed as planned."));
    },
    onError: (error) => setNotice(errorNotice(error)),
  });
  const deviationMutation = useMutation({
    mutationFn: () =>
      saveShiftDeviation(shift.id, {
        deviation_type: deviationType,
        ...(deviationType === "CANCELLED"
          ? {}
          : { actual_start_local: actualStart, actual_end_local: actualEnd }),
        ...(decisionTime ? { noticed_or_decided_local: decisionTime } : {}),
        ...(maker ? { decision_maker_type: maker } : {}),
        ...(reason ? { reason } : {}),
        ...(shift.deviation ? { version: shift.deviation.version } : {}),
      }),
    onSuccess: () => {
      setPreview(null);
      refresh();
      onFactsChanged?.();
      setNotice(okNotice("Deviation recorded. The shift report reflects the actual facts."));
    },
    onError: (error) => setNotice(errorNotice(error)),
  });
  // Act B (§4.6): the Company cancelled a real planned shift. It posts to the
  // EXISTING deviation endpoint through `saveShiftCancellation` — no new route,
  // no new table — and the plan itself is left byte-identical, because the
  // scheduled shift is the basis for any allowance. The reported role is what the
  // employee was TOLD, which is why the field is `decisionMakerRoleFieldLabel`
  // and never "Cancelled by" (D22).
  const cancellationMutation = useMutation({
    mutationFn: () =>
      saveShiftCancellation(shift.id, {
        // An empty field sends NOTHING rather than an empty string, which the
        // server would reject as a malformed datetime.
        noticed_or_decided_local: toIsoDateTime(cancelNoticedAt) ?? undefined,
        // "Other / not stated" is a UI-only sentinel (lib/labels.ts) and is never
        // stored: the TEXT is what is sent, and an empty text sends nothing at
        // all rather than the sentinel.
        decisionMakerRole:
          (cancelRole === decisionMakerRoleOther ? cancelRoleText.trim() : cancelRole) || null,
        reason: cancelReason.trim() || null,
        ...(shift.deviation ? { version: shift.deviation.version } : {}),
      }),
    onSuccess: () => {
      setPreview(null);
      refresh();
      onFactsChanged?.();
      setNotice(okNotice("Cancellation recorded. Your planned shift is kept."));
    },
    onError: (error) => setNotice(errorNotice(error)),
  });
  // Act A (§4.6, D12): the employee retracts their OWN plan record because it was
  // entered in error or was never the real plan. `WITHDRAWN`, no deviation, no
  // entitlement — deliberately a different act from the cancellation above.
  const withdrawMutation = useMutation({
    mutationFn: () => withdrawShiftPlan(shift.id, shift.approved_revision.id),
    onSuccess: () => {
      setPreview(null);
      refresh();
      onFactsChanged?.();
      setNotice(okNotice("Plan withdrawn. No cancellation was recorded."));
    },
    onError: (error) => setNotice(errorNotice(error)),
  });
  // The THIRD act on this card, and the only one that moves the plan itself: a
  // correction of a window that was recorded wrongly. Deliberately neither of the
  // other two — it is not a cancellation (Act B, which leaves the plan
  // byte-identical because the scheduled shift is the basis for any allowance) and
  // not a withdrawal (Act A, for a shift that was never really planned), and the
  // form says so in words.
  //
  // The concurrency token is the CURRENT approved revision id read from the row on
  // screen — the same one `confirmShift` and `withdrawShiftPlan` send. A
  // correction carries NO `client_request_id` (it is not a create), and a token the
  // server no longer holds is answered with 409 `CONFLICT_SHIFT_PLAN_CHANGED`
  // rather than overwriting a revision the employee has not seen.
  const correctionMutation = useMutation({
    mutationFn: () =>
      editShiftPlan(shift.id, {
        approved_revision_id: shift.approved_revision.id,
        planned_start_local: plannedStartIso as string,
        planned_end_local: plannedEndIso as string,
        ...(startFold === undefined ? {} : { start_fold: startFold }),
        ...(endFold === undefined ? {} : { end_fold: endFold }),
        // The kind travels only when the row's own kind is one the shared label
        // map can present: a `<select>` cannot show a code it does not know, and
        // submitting the map's options over an unknown kind would silently change
        // a fact the employee never chose. An omitted key means "unchanged".
        ...(kindOffered ? { shift_kind: plannedKind } : {}),
      }),
    onSuccess: () => {
      setPreview(null);
      setShowCorrection(false);
      refresh();
      onFactsChanged?.();
      setNotice(okNotice("Planned shift corrected. The plan now shows the window you recorded."));
    },
    onError: (error) => setNotice(errorNotice(error)),
  });
  // Every Shift Report input this card can observe, serialized.
  //
  // The list is the server's own (`app/shift/report.py::ShiftReportInputs`) —
  // the fields the projection is BUILT FROM — minus the two the client never
  // sees (`employee_identifier`, and the policy edition behind
  // `policy_version`/`policy_status`). The first version of this fingerprint
  // carried only status, actual span, deviation version/type and the break
  // status, so a shift-plan update that moved the APPROVED revision while those
  // stayed put left the preview claiming the former plan (review R7).
  const factSignature = JSON.stringify([
    shift.status,
    // Plan provenance is a projection input like any other: the report states
    // who authored the plan, so a refetched shift that changed source must drop
    // the snapshot that still states the old one (plan §6.5).
    shift.plan_source ?? null,
    // the approved plan — a revision change is a projection change
    shift.approved_revision.id,
    shift.approved_revision.revision_no,
    shift.approved_revision.planned_start_at,
    shift.approved_revision.planned_end_at,
    shift.approved_revision.communicated_at,
    shift.actual_start_at,
    shift.actual_end_at,
    // the complete deviation projection
    shift.deviation?.version ?? null,
    shift.deviation?.deviation_type ?? null,
    shift.deviation?.actual_start_at ?? null,
    shift.deviation?.actual_end_at ?? null,
    shift.deviation?.noticed_or_decided_at ?? null,
    shift.deviation?.decision_maker_type ?? null,
    shift.deviation?.reason ?? null,
    shift.paid_break_status,
    // The break FACTS, not just the status: a second break leaves the status at
    // RECORDED, so without these the preview would survive a change to the facts
    // it projects (review R6/R8 — the same window `factSignature` exists to close).
    shift.paid_breaks.map((item) => [item.id, item.start_at, item.end_at, item.version]),
    // findings are derived from the facts above and the policy edition, and the
    // report copies them UNCHANGED — `message` included (`ShiftFindingOut`).
    // Fingerprinting only `rule_id:status` left the preview showing superseded
    // wording when the server re-derived the same finding in new words
    // (review R8).
    shift.findings.map((finding) => [finding.rule_id, finding.status, finding.message]),
  ]);
  useEffect(() => setPreview(null), [factSignature]);

  const previewMutation = useMutation({
    mutationFn: () => fetchShiftReportPreview(shift.id),
    onSuccess: setPreview,
  });
  // A preview is a snapshot of the projection over the RECORDED facts, and this
  // card is NOT the only writer of them: the day timeline can edit or delete the
  // linked paid break, and the shift arrives here as a refetched prop. The three
  // writes below therefore drop it synchronously (a failed or slow refetch must
  // not leave the stale projection presented as current — review R4), and this
  // fingerprint catches every OTHER writer, which no callback on this card can
  // see (review R6). Both, because they cover different windows.
  // The assignment id is unambiguous on this card, so the linked paid break
  // needs no selector.
  const breakMutation = useMutation({
    mutationFn: () =>
      createBreak({
        start_local: breakStart,
        end_local: breakEnd,
        kind: "PAID_SHIFT_BREAK",
        linked_shift_assignment_id: shift.id,
      }),
    onSuccess: () => {
      setPreview(null);
      refresh();
      onFactsChanged?.();
      // Cleared so a SECOND break on the same shift is one more entry rather
      // than an edit of the first, which is the whole point of the action now
      // that a paid break no longer closes the form.
      setBreakStart("");
      setBreakEnd("");
      setNotice(okNotice("Paid shift break recorded. The shift report will show it."));
    },
    onError: (error) => setNotice(errorNotice(error)),
  });
  // The displayed state is ALWAYS the server's. A mutation flag would stay
  // "success" for the life of this card, so a later correction or deletion of the
  // break in the month timeline would leave the card claiming the requirement is
  // still met — and hide the action that lets the employee fix it.
  const paidBreakRecorded = shift.paid_break_status === "RECORDED";
  // In start order, so the list reads the way the shift happened. These are the
  // assignment's LINKED facts rather than a day's, which is why a break after
  // midnight appears here even though the day panel's own list would not show it
  // on this date.
  const paidBreaks = [...shift.paid_breaks].sort((a, b) => a.start_at.localeCompare(b.start_at));

  // §6.5: ONLY `PLANNED` is an active plan. Every other status is retained
  // lineage, so every gate below is a POSITIVE test on `PLANNED` — the previous
  // `!== "SUPERSEDED"` shape left a WITHDRAWN plan showing the confirm, deviation
  // and break controls.
  const isActivePlan = shift.status === "PLANNED";

  // D3: a plan is mutable only while NOTHING depends on it — a linked actual
  // interval, a current deviation, or a linked paid shift break.
  const planMutable =
    isActivePlan && !shift.actual_start_at && !shift.deviation && !paidBreakRecorded;

  // A paid break is a fact INSIDE an active plan and a long shift can hold more
  // than one, so this is NOT gated on whether one is already recorded — only on
  // the plan being active, the same positive test on `PLANNED` as every other
  // gate here (§6.5). It deliberately does not depend on actual work or on a
  // deviation, which is what makes it the one action still offered on a frozen
  // plan. Correcting or removing a break stays the day timeline's job, where the
  // supersede/version and delete-with-reason paths already live.
  const breakAllowed = isActivePlan;

  // §4.6 Act B has a DIFFERENT precondition: a cancellation is allowed on a plan
  // that already carries a deviation of the same chain (that is where the
  // reported context lands), and refused once actual work or a paid break depends
  // on the plan. Reusing `planMutable` here would hide the one path the plan
  // reserves for exactly that case.
  const cancellationAllowed = isActivePlan && !shift.actual_start_at && !paidBreakRecorded;

  // Frozen but ACTIVE: the reason is stated and the correct action is offered —
  // never a disabled button.
  const frozen = isActivePlan && !cancellationAllowed;
  const statusCopy = nonActiveCopy(shift.status);

  // Every field on this form owns its own parser (`lib/inputs.tsx`), so the app
  // can state the rule the browser would otherwise enforce silently. A native
  // `pattern` mismatch blocks the `submit` event: no request, no message, an
  // enabled button that does nothing (acceptance E3). The `pattern` stays as a
  // backstop; the app states the same requirement first.
  const spansRequired = deviationType !== "CANCELLED";
  const spansIncomplete =
    spansRequired && (toIsoDateTime(actualStart) === null || toIsoDateTime(actualEnd) === null);
  const decisionTimeIncomplete = decisionTime !== "" && toIsoDateTime(decisionTime) === null;
  const deviationValid = !spansIncomplete && !decisionTimeIncomplete;
  const deviationRequirement = spansIncomplete
    ? "Enter the actual start and end as HH:MM — for example 22:30, or 14.10.2026 22:30 for another day."
    : "Use HH:MM — for example 18:00, or 05.12.2026 18:00 for another day.";
  // UI-012 precedent (the month day panel's `intervalOrderError`): a break whose
  // end is not after its start is refused by the server as
  // `VALIDATION_INTERVAL_ORDER`, so the app states that rule before the submit
  // button enables rather than offering a button that can only fail. Containment
  // inside the linked shift stays the SERVER's answer — it is a domain rule with
  // its own words, and the card surfaces the refusal.
  const breakStartIso = toIsoDateTime(breakStart);
  const breakEndIso = toIsoDateTime(breakEnd);
  const breakOrderError =
    breakStartIso !== null && breakEndIso !== null && breakEndIso <= breakStartIso
      ? "The break end must be after the break start."
      : null;
  const breakValid = breakStartIso !== null && breakEndIso !== null && breakOrderError === null;
  // The reported cancellation instant is OPTIONAL (`shift/schemas.py:165` defaults
  // it to None). Making the form require it would be a process tightening under
  // INV-15: an absent instant leaves SHIFT-009 BLOCKED_POLICY — the honest answer —
  // and the employee must still be able to record what happened. Only a value the
  // employee actually typed and that does not parse blocks the submit, exactly like
  // the deviation form's `decisionTimeIncomplete` above.
  const cancelNoticedIso = toIsoDateTime(cancelNoticedAt);
  const cancelNoticedIncomplete = cancelNoticedAt !== "" && cancelNoticedIso === null;
  // --- the correction's derived values -------------------------------------
  const plannedStartIso = toIsoDateTime(plannedStart);
  const plannedEndIso = toIsoDateTime(plannedEnd);
  // The window is a PAIR: the server resolves both ends together and keeps the
  // whole current window when neither arrives (`shift/service.py`), while
  // `ShiftPlanPatch` refuses one without the other as half-applied. So a cleared
  // end is not "keep the old end" — it either advances the revision as if nothing
  // had changed or is refused outright. Nothing here is newly MANDATORY (INV-15):
  // both values are prefilled from the row, so an untouched field already satisfies
  // the requirement, which is only stated once a field has been emptied or
  // half-typed.
  const windowIncomplete = plannedStartIso === null || plannedEndIso === null;
  // The row's own window as the fields present it, and the folds that reproduce the
  // stored instants.
  //
  // A fold is sent ONLY for a field the employee has NOT touched: there it is not a
  // choice the client is making, it re-states the occurrence the row already
  // carries. A NEW ambiguous wall time is left to the server, which refuses it in
  // words ("that wall time happens twice…") instead of letting the client pick an
  // occurrence on the employee's behalf (docs/ARCHITECTURE.md) — the same honest refusal the
  // deviation form's actual times already get.
  const storedStartLocal = berlinLocalDateTime(shift.approved_revision.planned_start_at);
  const storedEndLocal = berlinLocalDateTime(shift.approved_revision.planned_end_at);
  // The day each moment field resolves a bare "HH:MM" against. A shift's START
  // field anchors on the day the shift starts and its END field on the day it
  // ends, which is what keeps a night shift correct with no "ends next day"
  // control: the prefilled day for the end is already the next morning.
  const plannedStartDate = storedStartLocal.slice(0, 10);
  const plannedEndDate = storedEndLocal.slice(0, 10);
  const plannedStartTime = storedStartLocal.slice(11, 16);
  const startFold =
    plannedStart === storedStartLocal
      ? foldFor(shift.approved_revision.planned_start_at, plannedStart)
      : undefined;
  const endFold =
    plannedEnd === storedEndLocal
      ? foldFor(shift.approved_revision.planned_end_at, plannedEnd)
      : undefined;
  // The kind the row already carries, offered back unchanged. A row whose kind the
  // label map cannot present gets NO kind field (an engine code is never primary
  // copy, and offering the map's options instead would replace a fact the employee
  // never chose); the payload then omits the key, which means "unchanged".
  const kindOffered = Object.hasOwn(shiftKindLabels, shift.shift_kind);
  // The row's own values, read at OPEN time: after a correction the row carries the
  // new window, and a form reopened on the values it replaced would offer to write
  // back a window the plan no longer has.
  const openCorrection = () => {
    setPlannedStart(storedStartLocal);
    setPlannedEnd(storedEndLocal);
    setPlannedKind(shift.shift_kind);
    setShowCorrection(true);
  };
  // R5: a message rendered beside a field is not enough — the CONTROL must point
  // at it and declare itself invalid, or a screen-reader user cannot get from the
  // field to its rejection. The id is the one the matching <FieldIssue> renders.
  const issueProps = (field: string, id: string, error: unknown = deviationMutation.error) =>
    fieldIssueFor(error, field) ? { "aria-invalid": true as const, "aria-describedby": id } : {};
  // Ids are per shift: two cards on one day can be open for correction at the same
  // time, and a shared id would point both controls at the first card's message.
  const correctionId = (name: string) => `shift-${shift.id}-correction-${name}`;
  // Which inline form is currently revealed, in words. ONE statement for all four
  // forms: the reveal is a state change no button can announce by itself, and the
  // menu that triggers three of them unmounts on selection, so the live region has
  // to sit outside it and name whichever form just opened.
  const revealedFormAnnouncement = showCorrection
    ? "The correction form is open below."
    : showCancellation
      ? "The cancellation form is open below."
      : showBreak
        ? "The paid shift-break form is open below."
        : showDeviation
          ? shift.deviation
            ? "The recorded deviation is shown below."
            : "The deviation form is open below."
          : "";

  return (
    <section
      className="shift-card"
      aria-label={`${shiftKindLabel(shift.shift_kind)} plan and actual`}
    >
      <div className="day-heading">
        <div>
          {/* h4, not h3: this card is rendered INSIDE the day panel's "Planned
           *  assignments" section, whose own heading is an h3. As an h3 the shift
           *  title was a PEER of the section containing it, so heading navigation
           *  presented "Night shift" as a sibling of "Planned assignments" rather
           *  than as content within it. */}
          <h4>{shiftKindLabel(shift.shift_kind)}</h4>
        </div>
        {/* §6.2 fail-closed provenance. The chip is the employee's own record
         *  marker and is keyed on the same positive test as the label: only
         *  `EMPLOYEE_RECORDED` earns it, an authoritative source keeps the
         *  existing institutional wording, and an unknown source gets neither —
         *  an approval claim nobody made is the one outcome that must never
         *  happen.
         *
         *  "Entered by employee", not "Recorded by you": the wording has to say
         *  what the fact IS (the employee recorded the plan that applies to them)
         *  and must not read as institutional authorisation. It is deliberately
         *  quiet — a chip, not a heading — so provenance stays available without
         *  competing with the assignment's own facts. */}
        {shift.plan_source === "EMPLOYEE_RECORDED" && (
          <span className="source-badge">Entered by employee</span>
        )}
        <span className="policy-badge">2026 rules provisional</span>
      </div>
      {/* Read in order, each its own line, so a non-active card cannot be
       *  mistaken for an active plan: the status first, then the provenance. */}
      {statusCopy && <p className="private-note">{statusCopy}</p>}
      {shift.plan_source === "EMPLOYEE_RECORDED" && (
        <p className="private-note">Employee-entered plan.</p>
      )}
      <dl className="shift-times">
        <div>
          {/* Never hard-coded: `planSourceLabel` is the fail-closed three-way
           *  label, so a plan nobody approved cannot read "Approved plan". */}
          <dt>{planSourceLabel(shift.plan_source)}</dt>
          <dd>
            {visibleTime(shift.approved_revision.planned_start_at)} →{" "}
            {visibleTime(shift.approved_revision.planned_end_at)}
          </dd>
        </div>
        <div>
          <dt>Actual</dt>
          <dd>
            {/* A cancelled shift never happened, so "Not confirmed yet" would read
             *  as outstanding work. Name the reason instead: the plan is kept as
             *  the basis for any allowance, and the deviation is the record that
             *  nothing was worked. */}
            {shift.actual_start_at
              ? `${visibleTime(shift.actual_start_at)} → ${visibleTime(shift.actual_end_at)}`
              : shift.deviation?.deviation_type === "CANCELLED"
                ? "None — the shift was cancelled"
                : "Not confirmed yet"}
          </dd>
        </div>
        <div>
          <dt>Paid shift break</dt>
          <dd>
            {paidBreakLabels[shift.paid_break_status]}
            {paidBreaks.length > 0 && (
              <ul className="paid-break-list">
                {paidBreaks.map((item) => (
                  <li key={item.id}>
                    {visibleTime(item.start_at)} → {visibleTime(item.end_at)}
                  </li>
                ))}
              </ul>
            )}
            {paidBreaks.length > 0 && (
              <span className="private-note">Correct or remove one in the day timeline.</span>
            )}
          </dd>
        </div>
      </dl>
      {/* §6.5 frozen-but-active: the reason and the correct action, in words.
       *  `role="status"` because the state can arrive from a refetch while the
       *  employee is looking at the card. */}
      {frozen && (
        <p className="private-note" role="status">
          {frozenPlanReason(shift)}
        </p>
      )}
      {/* ACTION HIERARCHY (owner directive, 2026-09-26).
       *
       *  The five actions used to be five identical sibling buttons in five
       *  separate rows, with the destructive one the only visually distinct
       *  control on the card — so the interface shouted the mistake path and
       *  whispered the normal one. They are now ordered by what the employee
       *  actually does:
       *
       *    PRIMARY   the normal workflow: confirm the plan was worked, or record
       *              that it was not. Visible, and the confirm is the only navy
       *              solid control in the panel.
       *    SECONDARY everything rarer, behind ONE menu: correct the window, record
       *              a cancellation, record a paid shift break.
       *    DESTRUCTIVE last inside that menu, quiet, still two-step: remove a plan
       *              that was entered by mistake.
       *
       *  The removal is NOT the same operation as a cancellation and the two are
       *  kept apart in wording, position and mutation: removal (withdrawal) says
       *  the plan was never real and carries no entitlement, while a cancellation
       *  reports that a genuinely planned shift did not happen and may carry
       *  allowances. The steering sentence that tells them apart travels with the
       *  destructive item INSIDE the menu, so it is read at the moment of choice
       *  instead of being one more paragraph on the card.
       *
       *  Every gate below is unchanged: all three are positive tests on PLANNED,
       *  so a WITHDRAWN or SUPERSEDED row offers no action at all. */}
      {isActivePlan && (
        <div className="workspace-actions shift-actions">
          {!shift.deviation && (
            <>
              {!shift.actual_start_at && (
                <button
                  type="button"
                  className="primary"
                  onClick={() => confirmMutation.mutate()}
                  disabled={confirmMutation.isPending}
                >
                  Confirm worked as planned
                </button>
              )}
              <button type="button" onClick={() => setShowDeviation(true)}>
                Record a deviation
              </button>
            </>
          )}
          {(planMutable || cancellationAllowed || breakAllowed) && (
            <OverflowMenu
              className="more-menu"
              popoverClassName="shift-actions-popover"
              toggleLabel="More shift actions"
              toggleText="More shift actions"
              items={(close) => (
                <>
                  {planMutable && (
                    <button
                      type="button"
                      disabled={showCorrection}
                      onClick={() => {
                        close();
                        openCorrection();
                      }}
                    >
                      Correct the planned shift
                    </button>
                  )}
                  {cancellationAllowed && (
                    <button
                      type="button"
                      disabled={showCancellation}
                      onClick={() => {
                        close();
                        setShowCancellation(true);
                      }}
                    >
                      Record a shift cancellation
                    </button>
                  )}
                  {breakAllowed && (
                    <button
                      type="button"
                      disabled={showBreak}
                      onClick={() => {
                        close();
                        setShowBreak(true);
                      }}
                    >
                      Record a paid shift break
                    </button>
                  )}
                  {planMutable && (
                    <>
                      <p className="menu-note">
                        Only for a shift that was never really planned. A Company cancellation is a
                        cancellation — it may carry allowances.
                      </p>
                      <ConfirmAction
                        label="Remove plan entered by mistake"
                        confirmLabel="Remove the planned shift"
                        className="danger-ghost"
                        busy={withdrawMutation.isPending}
                        onConfirm={() => {
                          close();
                          withdrawMutation.mutate();
                        }}
                      />
                    </>
                  )}
                </>
              )}
            />
          )}
          {/* The reveal is a state change a button cannot announce by itself, and
           *  it must live OUTSIDE the menu: selecting an item closes the menu, so
           *  a status region inside it would be unmounted in the same commit that
           *  produced the message. Rendered in BOTH states with only its TEXT
           *  changing, because a live region mounted with its content already
           *  present is not reliably announced (the `lib/confirm.tsx` lesson). */}
          <span className="confirm-status" role="status">
            {revealedFormAnnouncement}
          </span>
        </div>
      )}
      {/* Inline, per the repo's convention (`AddShiftPlanForm` in `month.tsx`, the
       *  OCD card's `CorrectAssignmentForm`): a real `<fieldset>`/`<legend>` group
       *  with a real `<label>` bound to every control — the label text IS the
       *  accessible name, no `aria-label` standing in for a visible label — native
       *  `<select>`/`<button>`, and the disabled primary stating its own
       *  requirement through `aria-describedby` (WCAG 2.2 SC 1.3.1, 2.1.1, 3.3.2,
       *  4.1.2). Field order is the order the employee needs: the two window ends
       *  first (they are what a correction is for), then the kind. */}
      {planMutable && showCorrection && (
        <fieldset className="interval-editor new-interval">
          <legend>Correct the planned shift</legend>
          {/* The distinction is not decoration: withdrawal carries no entitlement
           *  and a cancellation may, so an employee who picks the wrong act
           *  misreports their own record. Naming the other two acts is how this
           *  form stays tellable apart from both (§6.3, §6.4). */}
          <p className="private-note">
            Changes the planned window only. Record a cancellation if the Company cancelled it;
            remove the plan if it was never really planned.
          </p>
          <div className="editor-fields">
            <label>
              Planned start
              <MomentField
                value={plannedStart}
                onChange={setPlannedStart}
                anchorDate={plannedStartDate}
                required
                {...issueProps(
                  "planned_start_local",
                  correctionId("start-issue"),
                  correctionMutation.error,
                )}
              />
            </label>
            <label>
              Planned end
              <MomentField
                value={plannedEnd}
                onChange={setPlannedEnd}
                anchorDate={plannedEndDate}
                required
                {...issueProps(
                  "planned_end_local",
                  correctionId("end-issue"),
                  correctionMutation.error,
                )}
              />
            </label>
          </div>
          {kindOffered && (
            <label>
              Shift kind
              <select
                value={plannedKind}
                onChange={(event) => setPlannedKind(event.target.value)}
                {...issueProps("shift_kind", correctionId("kind-issue"), correctionMutation.error)}
              >
                {Object.entries(shiftKindLabels).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
          )}
          <div className="editor-actions">
            <button type="button" onClick={() => setShowCorrection(false)}>
              Cancel
            </button>
            <button
              type="button"
              className="primary"
              onClick={() => correctionMutation.mutate()}
              disabled={correctionMutation.isPending || windowIncomplete}
              aria-describedby={windowIncomplete ? correctionId("requirement") : undefined}
            >
              Save correction
            </button>
          </div>
          {windowIncomplete && (
            <p className="field-requirement" id={correctionId("requirement")} role="status">
              Enter the planned start and end as DD.MM.YYYY HH:MM — for example 06.07.2026 20:00.
              Both ends are needed to move the window.
            </p>
          )}
          <FieldIssue
            error={correctionMutation.error}
            field="planned_start_local"
            id={correctionId("start-issue")}
          />
          <FieldIssue
            error={correctionMutation.error}
            field="planned_end_local"
            id={correctionId("end-issue")}
          />
          <FieldIssue
            error={correctionMutation.error}
            field="shift_kind"
            id={correctionId("kind-issue")}
          />
        </fieldset>
      )}
      {breakAllowed && showBreak && (
        <form
          className="shift-deviation-form"
          onSubmit={(event) => {
            event.preventDefault();
            breakMutation.mutate();
          }}
        >
          <h4>Paid shift break</h4>
          <label>
            Break start
            <MomentField
              value={breakStart}
              onChange={setBreakStart}
              anchorDate={plannedStartDate}
              rollBefore={plannedStartTime}
              required
              {...issueProps("start_local", "break-start-issue", breakMutation.error)}
            />
          </label>
          <label>
            Break end
            <MomentField
              value={breakEnd}
              onChange={setBreakEnd}
              anchorDate={plannedStartDate}
              rollBefore={plannedStartTime}
              required
              {...issueProps("end_local", "break-end-issue", breakMutation.error)}
            />
          </label>
          <button
            type="submit"
            disabled={breakMutation.isPending || !breakValid}
            aria-describedby={breakValid ? undefined : "break-requirement"}
          >
            Save paid break
          </button>
          {!breakValid && (
            <p className="field-requirement" id="break-requirement" role="status">
              {breakOrderError ??
                "Enter the confirmed break start and end as HH:MM — for example 12:00 and 12:30."}
            </p>
          )}
          <FieldIssue error={breakMutation.error} field="start_local" id="break-start-issue" />
          <FieldIssue error={breakMutation.error} field="end_local" id="break-end-issue" />
        </form>
      )}
      {/* One channel: a new attempt replaces the previous message, so a stale
       *  success can never sit beside a current failure (acceptance E4). */}
      <ActionNotice notice={notice} />
      {/* §6.3 the cancellation form (Act B, D11/D22).
       *
       *  Accessibility: every control is a native element with a real `<label>`
       *  bound to it (no `aria-label` standing in for a visible one), so the whole
       *  flow — open, choose the role, type the instant, submit — is keyboard-only
       *  and every field is announced with its own name (WCAG 2.2 SC 1.3.1, 2.1.1,
       *  3.3.2, 4.1.2). Field order is the order the employee needs: the reported
       *  instant first, because nothing can be classified without it. The reveal
       *  driven by the escape-hatch option is announced through the existing
       *  screen-reader-only `role="status"` region (SC 4.1.3), the same contract
       *  `lib/confirm.tsx` uses for its armed state, and the refusal/success
       *  messages keep the card's one-notice `role="alert"`/`role="status"`
       *  contract. */}
      {isActivePlan && showCancellation && cancellationAllowed && (
        <form
          className="shift-deviation-form"
          onSubmit={(event) => {
            event.preventDefault();
            cancellationMutation.mutate();
          }}
        >
          <h4>Shift cancellation</h4>
          <label>
            When were you told?
            {/* NO `required`: the instant is optional (INV-15). A `required` here
             *  would let the BROWSER block the submit, which is the same process
             *  tightening as a disabled button — just hidden in the DOM instead of
             *  the JSX. Only a value the employee typed and that does not parse is
             *  refused, and that refusal is rendered as a field message. */}
            <MomentField
              value={cancelNoticedAt}
              onChange={setCancelNoticedAt}
              anchorDate={plannedStartDate}
              {...issueProps(
                "noticed_or_decided_local",
                "cancellation-instant-issue",
                cancellationMutation.error,
              )}
            />
          </label>
          <p className="private-note">
            Record this instant if you know it — the entitlement cannot be classified without it,
            but the cancellation itself can be saved either way.
          </p>
          <label>
            {decisionMakerRoleFieldLabel}
            <select
              value={cancelRole}
              onChange={(event) => setCancelRole(event.target.value)}
              {...issueProps(
                "decision_maker_type",
                "cancellation-role-issue",
                cancellationMutation.error,
              )}
            >
              {/* Exactly the source-backed decision roles plus the escape hatch
               *  (lib/labels.ts `decisionMakerRoles`); "Local Contact" and "Works
               *  Council" are deliberately absent (D22). */}
              {decisionMakerRoles.map((role) => (
                <option key={role} value={role}>
                  {role}
                </option>
              ))}
            </select>
          </label>
          {/* The reveal is a status change the select itself cannot announce. The
           *  region is rendered in BOTH states and only its text changes, because
           *  a live region mounted with its content already present is not
           *  reliably announced (the `lib/confirm.tsx` lesson). */}
          <span className="confirm-status" role="status">
            {cancelRole === decisionMakerRoleOther ? "A decision-maker field is below." : ""}
          </span>
          {cancelRole === decisionMakerRoleOther && (
            <label>
              Who decided
              <input
                value={cancelRoleText}
                onChange={(event) => setCancelRoleText(event.target.value)}
                maxLength={120}
                {...issueProps(
                  "decision_maker_type",
                  "cancellation-role-issue",
                  cancellationMutation.error,
                )}
              />
            </label>
          )}
          <label className="wide-field">
            Notes / context
            <textarea
              value={cancelReason}
              onChange={(event) => setCancelReason(event.target.value)}
              maxLength={500}
              {...issueProps("reason", "cancellation-reason-issue", cancellationMutation.error)}
            />
          </label>
          {/* Disabled only for a MALFORMED value the employee typed — the same
           *  R5 pattern as the deviation and paid-break forms, whose submit is
           *  gated on `!deviationValid` / `!breakValid`. An EMPTY field is valid,
           *  because the instant is optional (INV-15): a `required` here would be
           *  the tightening the plan forbids, just hidden in the DOM. */}
          <button
            type="submit"
            disabled={cancellationMutation.isPending || cancelNoticedIncomplete}
            aria-describedby={cancelNoticedIncomplete ? "cancellation-requirement" : undefined}
          >
            Save cancellation
          </button>
          {cancelNoticedIncomplete && (
            <p className="field-requirement" id="cancellation-requirement" role="status">
              Use the format DD.MM.YYYY HH:MM — for example 05.12.2026 18:00.
            </p>
          )}
          <FieldIssue
            error={cancellationMutation.error}
            field="noticed_or_decided_local"
            id="cancellation-instant-issue"
          />
          <FieldIssue
            error={cancellationMutation.error}
            field="decision_maker_type"
            id="cancellation-role-issue"
          />
          <FieldIssue
            error={cancellationMutation.error}
            field="reason"
            id="cancellation-reason-issue"
          />
        </form>
      )}
      {isActivePlan && showCancellation && cancellationAllowed && (
        <p className="private-note">
          The planned shift stays as the basis for allowances — record this rather than removing the
          plan.
        </p>
      )}
      {isActivePlan && showDeviation && (
        <form
          className="shift-deviation-form"
          onSubmit={(event) => {
            event.preventDefault();
            deviationMutation.mutate();
          }}
        >
          <h4>Actual difference</h4>
          <label>
            Deviation
            <select
              value={deviationType}
              onChange={(event) => setDeviationType(event.target.value)}
              {...issueProps("deviation_type", "deviation-type-issue")}
            >
              {deviationTypes.map((type) => (
                <option key={type} value={type}>
                  {type.replaceAll("_", " ")}
                </option>
              ))}
            </select>
          </label>
          {deviationType !== "CANCELLED" && (
            <>
              <label>
                Actual start
                <MomentField
                  value={actualStart}
                  onChange={setActualStart}
                  anchorDate={plannedStartDate}
                  required
                  {...issueProps("actual_start_local", "deviation-actual-start-issue")}
                />
              </label>
              <label>
                Actual end
                <MomentField
                  value={actualEnd}
                  onChange={setActualEnd}
                  anchorDate={plannedEndDate}
                  required
                  {...issueProps("actual_end_local", "deviation-actual-end-issue")}
                />
              </label>
            </>
          )}
          <label>
            Decision time
            <MomentField
              value={decisionTime}
              onChange={setDecisionTime}
              anchorDate={plannedStartDate}
              {...issueProps("noticed_or_decided_local", "deviation-decision-time-issue")}
            />
          </label>
          <label>
            Decision maker / reported role
            <input
              value={maker}
              onChange={(event) => setMaker(event.target.value)}
              maxLength={120}
              {...issueProps("decision_maker_type", "deviation-maker-issue")}
            />
          </label>
          <label className="wide-field">
            Decision context / reason
            <textarea
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              maxLength={500}
              {...issueProps("reason", "deviation-reason-issue")}
            />
          </label>
          <button
            type="submit"
            disabled={deviationMutation.isPending || !deviationValid}
            aria-describedby={deviationValid ? undefined : "deviation-requirement"}
          >
            Save deviation
          </button>
          {!deviationValid && (
            <p className="field-requirement" id="deviation-requirement" role="status">
              {deviationRequirement}
            </p>
          )}
          <FieldIssue
            error={deviationMutation.error}
            field="noticed_or_decided_local"
            id="deviation-decision-time-issue"
          />
          <FieldIssue
            error={deviationMutation.error}
            field="actual_start_local"
            id="deviation-actual-start-issue"
          />
          <FieldIssue
            error={deviationMutation.error}
            field="actual_end_local"
            id="deviation-actual-end-issue"
          />
          {/* Every field the server can name must be able to render its message:
           *  the notice says "check the marked fields", so a field that cannot be
           *  marked would make that sentence false. */}
          <FieldIssue
            error={deviationMutation.error}
            field="decision_maker_type"
            id="deviation-maker-issue"
          />
          <FieldIssue error={deviationMutation.error} field="reason" id="deviation-reason-issue" />
          <FieldIssue
            error={deviationMutation.error}
            field="deviation_type"
            id="deviation-type-issue"
          />
        </form>
      )}
      <p className="private-note">Add only confirmed actual break facts.</p>
      {shift.findings.length > 0 && (
        <details className="shift-findings-details">
          <summary>Shift findings ({shift.findings.length})</summary>
          <ul className="shift-findings">
            {shift.findings.map((finding) => (
              <li key={finding.rule_id} className={`severity-${finding.status.toLowerCase()}`}>
                <strong>{finding.message}</strong>
              </li>
            ))}
          </ul>
        </details>
      )}
      {isActivePlan && shift.deviation && (
        <button
          type="button"
          onClick={() => previewMutation.mutate()}
          disabled={previewMutation.isPending}
        >
          Preview Shift Report
        </button>
      )}
      {preview && (
        <aside className="preview" aria-label="Private Shift Report preview">
          <strong>Shift Report preview — not shared</strong>
          <p>
            {visibleTime(preview.planned_start_at)} → {visibleTime(preview.planned_end_at)}
          </p>
          {/* The preview exists so the employee can read what they are about to
           *  hand over. The server sends the actual window, the deviation and
           *  the findings in this same payload, so rendering only the plan made
           *  the sentence below describe fields the employee could not see —
           *  and hid the deviation, which is the whole reason a Shift Report is
           *  submitted at all (ACCESS-004 scope, `deviationTypeLabels`). */}
          <p>
            Actual:{" "}
            {preview.actual_start_at && preview.actual_end_at
              ? `${visibleTime(preview.actual_start_at)} → ${visibleTime(preview.actual_end_at)}`
              : "not confirmed"}
          </p>
          {preview.deviation_type && (
            <p>
              Deviation: {deviationTypeLabel(preview.deviation_type)}
              {preview.reason ? ` — ${preview.reason}` : ""}
            </p>
          )}
          {/* The recipient's view renders both of these (`HumanPreview`), so the
           *  employee's preview must too — otherwise the deviation context the
           *  recipient reads cannot be checked before it is handed over. Same
           *  wording on both sides, deliberately. */}
          {preview.noticed_or_decided_at && (
            <p>Noticed or decided: {berlinInstant(String(preview.noticed_or_decided_at))}</p>
          )}
          {preview.decision_maker_type && (
            <p>Decided or reported by: {String(preview.decision_maker_type)}</p>
          )}
          {/* Provenance and the review status are what the recipient gets
           *  INSTEAD of an entitlement: a cancelled or aborted shift's factual
           *  report is submittable and a human decides the compensation, so the
           *  employee must see the same one-line state before sharing it. */}
          <p>{planSourceLabel(preview.plan_source)}</p>
          <p>Paid shift break: {paidBreakLabels[preview.paid_break_status]}</p>
          <p>Compensation review: {compensationReviewLabel(preview.compensation_review_status)}</p>
          {preview.findings.length > 0 && (
            <ul className="shift-findings">
              {preview.findings.map((finding) => (
                <li key={finding.rule_id} className={`severity-${finding.status.toLowerCase()}`}>
                  <strong>{finding.message}</strong>
                </li>
              ))}
            </ul>
          )}
          <p>
            Only this shift’s plan, actual facts and deviation context are included. Ordinary
            intervals are excluded.
          </p>
        </aside>
      )}
    </section>
  );
}
