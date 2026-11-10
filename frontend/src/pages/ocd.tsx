import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import {
  cancelOcdAssignment,
  correctOcdAssignment,
  correctOcdCall,
  createOcdAssignment,
  createOcdCall,
  fetchCompensationElection,
  fetchIdentity,
  fetchOcd,
  fetchOcdCompensationPreview,
  monthRange,
  saveCompensationElection,
  voidOcdCall,
  type OcdAssignment,
  type OcdCall,
  type OcdCompensationPreview,
} from "../lib/api";
import { plainMinutes } from "./month";
import { MonthContextNav } from "./month";
import { ConfirmAction } from "../lib/confirm";
import { berlinInstant, berlinLocalDateTime } from "../lib/format";
import { DateTimeField, toIsoDateTime } from "../lib/inputs";
import {
  componentLabel,
  electionLabels,
  planSourceLabel,
  shortNoticeLabel,
  shortNoticeRuleNote,
} from "../lib/labels";
import { invalidateFactDerived } from "../lib/query";
import { fieldIssueFor } from "../lib/api";
import { ActionNotice, errorNotice, FieldIssue, okNotice, type Notice } from "../lib/notice";

/** Human names for an OCD assignment's lifecycle state (never the raw enum). */
const assignmentStatusLabels: Record<string, string> = {
  SCHEDULED: "Availability",
  CANCELLED: "Cancelled",
  SUPERSEDED: "Replaced",
};

function visibleInstant(instant: string | null): string {
  return berlinInstant(instant);
}

/** The call's travel component, in employee-facing words.
 *
 *  A remote call has none, and the call's mode is already shown as a chip
 *  beside the interval, so this note stays empty for it rather than repeating
 *  the word "Remote" underneath the chip. */
function travelText(call: OcdCall): string {
  if (call.mode === "REMOTE") return "";
  if (call.travel_out_minutes == null && call.travel_back_minutes == null) return "";
  return `On-site · travel ${call.travel_out_minutes ?? 0} + ${call.travel_back_minutes ?? 0} min`;
}

/** `onFactsChanged` runs after a call write SUCCEEDS, so the parent can drop the
 *  private compensation preview in the same tick. Waiting for the invalidated
 *  query to refetch is not equivalent: a failed refetch leaves the snapshot on
 *  screen describing an assignment that no longer exists (review R3). */
function CallRow({ call, onFactsChanged }: { call: OcdCall; onFactsChanged: () => void }) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [mode, setMode] = useState(call.mode);
  const [start, setStart] = useState(berlinLocalDateTime(call.call_start_at));
  const [end, setEnd] = useState(berlinLocalDateTime(call.call_end_at));
  const [travelOut, setTravelOut] = useState(call.travel_out_minutes?.toString() ?? "");
  const [travelBack, setTravelBack] = useState(call.travel_back_minutes?.toString() ?? "");
  const [comment, setComment] = useState(call.comment ?? "");
  const [notice, setNotice] = useState<Notice>(null);
  // A call is working time and it re-types the day, so correcting or voiding one
  // changes the day panel, the calendar cell and the month balance too — not just
  // this assignment's own call list.
  const refresh = () => invalidateFactDerived(queryClient);
  // Correcting a call needs the same stated requirement as recording one: a
  // pattern-invalid value makes the form natively invalid, which blocks the
  // submit event with no request and no message (acceptance E3, review R3).
  const correctionValid = toIsoDateTime(start) !== null && toIsoDateTime(end) !== null;
  // The CONTROL must point at its own message and declare itself invalid, or a
  // screen-reader user cannot get from the field to its rejection (review R9).
  const issueProps = (field: string, id: string) =>
    fieldIssueFor(saveMutation.error, field)
      ? { "aria-invalid": true as const, "aria-describedby": id }
      : {};

  const saveMutation = useMutation({
    mutationFn: () =>
      correctOcdCall(call.id, {
        version: call.version,
        mode,
        call_start_local: start,
        call_end_local: end,
        travel_out_minutes: mode === "ON_SITE" && travelOut !== "" ? Number(travelOut) : null,
        travel_back_minutes: mode === "ON_SITE" && travelBack !== "" ? Number(travelBack) : null,
        comment: comment || null,
      }),
    onSuccess: () => {
      setEditing(false);
      setNotice(okNotice("Call and its work fact corrected together."));
      onFactsChanged();
      refresh();
    },
    onError: (error) => setNotice(errorNotice(error)),
  });
  const voidMutation = useMutation({
    mutationFn: () => voidOcdCall(call.id, call.version),
    onSuccess: () => {
      setNotice(okNotice("Call voided; the linked work fact was removed."));
      onFactsChanged();
      refresh();
    },
    onError: (error) => setNotice(errorNotice(error)),
  });

  if (call.status === "VOIDED") return null;

  return (
    <li className="ocd-call">
      {!editing ? (
        <>
          <p>
            <strong>
              {visibleInstant(call.call_start_at)} → {visibleInstant(call.call_end_at)}
            </strong>{" "}
            <span className="work-type-chip">{call.mode === "REMOTE" ? "Remote" : "On-site"}</span>
            <span className="ocd-duration">
              {" "}
              {plainMinutes(Math.round(call.elapsed_seconds / 60))}
            </span>
          </p>
          {travelText(call) && <p className="private-note">{travelText(call)}</p>}
          {call.comment && <p className="private-note">{call.comment}</p>}
          <div className="workspace-actions">
            <button type="button" onClick={() => setEditing(true)}>
              Correct call
            </button>
            <button
              type="button"
              className="danger-ghost"
              onClick={() => voidMutation.mutate()}
              disabled={voidMutation.isPending}
            >
              Void call
            </button>
          </div>
        </>
      ) : (
        <form
          className="ocd-call-form"
          onSubmit={(event) => {
            event.preventDefault();
            saveMutation.mutate();
          }}
        >
          <label>
            Mode
            <select
              value={mode}
              onChange={(event) => setMode(event.target.value as "REMOTE" | "ON_SITE")}
            >
              <option value="ON_SITE">On-site</option>
              <option value="REMOTE">Remote</option>
            </select>
          </label>
          <label>
            Call start
            <DateTimeField
              value={start}
              onChange={setStart}
              required
              {...issueProps("call_start_local", "ocd-correction-start-issue")}
            />
          </label>
          <label>
            Call end
            <DateTimeField
              value={end}
              onChange={setEnd}
              required
              {...issueProps("call_end_local", "ocd-correction-end-issue")}
            />
          </label>
          {mode === "ON_SITE" && (
            <>
              <label>
                Travel to site (min)
                <input
                  type="number"
                  min={0}
                  step={5}
                  value={travelOut}
                  onChange={(event) => setTravelOut(event.target.value)}
                />
              </label>
              <label>
                Travel back (min)
                <input
                  type="number"
                  min={0}
                  step={5}
                  value={travelBack}
                  onChange={(event) => setTravelBack(event.target.value)}
                />
              </label>
            </>
          )}
          <label className="wide-field">
            Comment (private)
            <textarea
              value={comment}
              onChange={(event) => setComment(event.target.value)}
              maxLength={500}
            />
          </label>
          <button
            type="submit"
            disabled={saveMutation.isPending || !correctionValid}
            aria-describedby={correctionValid ? undefined : "ocd-correction-requirement"}
          >
            Save correction
          </button>
          {!correctionValid && (
            <p className="field-requirement" id="ocd-correction-requirement" role="status">
              Enter the call start and end as DD.MM.YYYY HH:MM — for example 19.10.2026 23:00.
            </p>
          )}
          <FieldIssue
            error={saveMutation.error}
            field="call_start_local"
            id="ocd-correction-start-issue"
          />
          <FieldIssue
            error={saveMutation.error}
            field="call_end_local"
            id="ocd-correction-end-issue"
          />
          <button type="button" onClick={() => setEditing(false)}>
            Cancel
          </button>
        </form>
      )}
      <ActionNotice notice={notice} />
    </li>
  );
}

/** The availability window's own rule, stated once for both entry paths (the
 *  add form and the correction form).
 *
 *  Both values are `YYYY-MM-DDTHH:MM` in the canonical zone, so they compare as
 *  strings; parsing them into `Date`s would reintroduce a timezone question for an
 *  answer that is already exact. An availability may cross midnight, so only the
 *  ORDER matters — there is no same-day restriction. A typed notice that does not
 *  parse blocks the submit with its own sentence rather than being dropped
 *  silently or guessed; an EMPTY notice is "not stated" and blocks nothing. */
function availabilityWindow(start: string, end: string, reported: string) {
  const startIso = toIsoDateTime(start);
  const endIso = toIsoDateTime(end);
  const reportedIso = toIsoDateTime(reported);
  const reportedIncomplete = reported.trim() !== "" && reportedIso === null;
  const orderError =
    startIso !== null && endIso !== null && startIso >= endIso
      ? "The availability must end after it starts."
      : null;
  return {
    startIso,
    endIso,
    reportedIso,
    orderError,
    valid: startIso !== null && endIso !== null && orderError === null && !reportedIncomplete,
  };
}

/** Record an on-call AVAILABILITY window (INV-10): a plan, never actual work.
 *
 *  `reported_notice_local` is the employee's OWN report of when they were told.
 *  There is deliberately NO `scheduled_at` field here: that is the INSTITUTIONAL
 *  notice instant, it is the sole input to the notice finding and to the
 *  short-notice compensation classification, and asserting when the Company gave
 *  notice is not the employee's to do (§4.3) — the server forces it NULL on every
 *  row written here. The reported instant stays OPTIONAL: the backend defaults it
 *  to none, so requiring it would tighten the process under INV-15. */
function AddOcdAssignmentForm({ onFactsChanged }: { onFactsChanged: () => void }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [reported, setReported] = useState("");
  const [notice, setNotice] = useState<Notice>(null);
  const { startIso, endIso, reportedIso, orderError, valid } = availabilityWindow(
    start,
    end,
    reported,
  );
  const createMutation = useMutation({
    mutationFn: () =>
      createOcdAssignment({
        start_local: startIso as string,
        end_local: endIso as string,
        // The key is present only when the employee actually stated an instant:
        // nothing is invented for them.
        ...(reportedIso === null ? {} : { reported_notice_local: reportedIso }),
      }),
    onSuccess: () => {
      // An availability moves the calendar, the month and the compensation
      // inputs, so the whole fact-derived family refreshes — not just this page.
      invalidateFactDerived(queryClient);
      onFactsChanged();
      setNotice(okNotice("On-call availability recorded."));
      setOpen(false);
      setStart("");
      setEnd("");
      setReported("");
    },
    onError: (error) => setNotice(errorNotice(error)),
  });
  // The CONTROL must declare itself invalid and point at its own message, or a
  // screen-reader user cannot get from the field to its rejection (R5/R6/R9).
  const issueProps = (field: string, id: string) =>
    fieldIssueFor(createMutation.error, field)
      ? { "aria-invalid": true as const, "aria-describedby": id }
      : {};
  // The notice field carries the explanation of what it is for as well as any
  // rejection, so both are announced with the field rather than only one.
  const noticeIssue = fieldIssueFor(createMutation.error, "reported_notice_local");
  if (!open) {
    return (
      <>
        <button
          type="button"
          className="add-interval"
          onClick={() => {
            setStart("");
            setEnd("");
            setReported("");
            setNotice(null);
            setOpen(true);
          }}
        >
          + Add on-call duty
        </button>
        {/* The notice lives OUTSIDE the form on purpose: a successful save
         *  collapses the form, and a message rendered inside it would be
         *  unmounted in the same commit that produced it (the AddShiftPlanForm
         *  precedent). */}
        <ActionNotice notice={notice} />
      </>
    );
  }
  return (
    <fieldset className="interval-editor new-interval">
      <legend>Add on-call duty</legend>
      <div className="editor-fields">
        <label>
          Starts
          <DateTimeField
            value={start}
            onChange={setStart}
            required
            {...issueProps("start_local", "new-ocd-start-issue")}
          />
        </label>
        <label>
          Ends
          <DateTimeField
            value={end}
            onChange={setEnd}
            required
            {...issueProps("end_local", "new-ocd-end-issue")}
          />
        </label>
        {orderError && (
          <p className="field-error" id="new-ocd-order-error">
            {orderError}
          </p>
        )}
      </div>
      <label>
        When were you told? (optional — recorded as reported)
        <DateTimeField
          value={reported}
          onChange={setReported}
          aria-describedby={
            noticeIssue ? "new-ocd-notice-issue new-ocd-notice-note" : "new-ocd-notice-note"
          }
          {...(noticeIssue ? { "aria-invalid": true as const } : {})}
        />
      </label>
      <p className="private-note" id="new-ocd-notice-note">
        Kept as reported — it decides no allowance.
      </p>
      <div className="editor-actions">
        <button type="button" onClick={() => setOpen(false)}>
          Cancel
        </button>
        <button
          type="button"
          className="primary"
          onClick={() => createMutation.mutate()}
          disabled={createMutation.isPending || !valid}
          aria-describedby={valid ? undefined : "new-ocd-requirement"}
        >
          Add
        </button>
      </div>
      {!valid && (
        <p className="field-requirement" id="new-ocd-requirement" role="status">
          Enter the availability start and end as DD.MM.YYYY HH:MM — for example 19.07.2026 19:00. A
          notice you type must use the same form.
        </p>
      )}
      <ActionNotice notice={notice} />
      <FieldIssue error={createMutation.error} field="start_local" id="new-ocd-start-issue" />
      <FieldIssue error={createMutation.error} field="end_local" id="new-ocd-end-issue" />
      <FieldIssue
        error={createMutation.error}
        field="reported_notice_local"
        id="new-ocd-notice-issue"
      />
    </fieldset>
  );
}

/** The row's optimistic-concurrency token, as the row itself reports it.
 *
 *  `PATCH` and `cancel` are version-guarded: the server compares the token against
 *  the chain HEAD and refuses a stale one (`CONFLICT_VERSION`), so the value must
 *  come from the row on screen — a token remembered from an earlier response is
 *  stale by construction. When the read payload carries no token the controls are
 *  not offered at all: a guessed token would corrupt a correctable record, and an
 *  action that can only fail is worse than no action.
 *
 *  `OcdOut` reports the token (it did not at first — the call rows did, and the
 *  per-card controls were silently unofferable against the real API). The read is
 *  still defensive rather than a bare property access: the TYPE is optional so a
 *  partial or older payload cannot crash a render, and a payload without a token
 *  must withhold the controls rather than send a guess. */
function versionToken(assignment: OcdAssignment): number | null {
  const token = assignment.version;
  return typeof token === "number" && token >= 1 ? token : null;
}

/** Correct an availability you recorded, as a NEW version of the same window.
 *
 *  The window is stated WHOLE rather than as a delta: the fields arrive prefilled
 *  with the row's own values, so an untouched field writes back the value it
 *  already had. `reported_notice_local` therefore always travels with the field's
 *  current value, and an EMPTIED field sends an explicit `null` — the only way to
 *  clear a notice entered by mistake (§4.3.1: an omitted key PRESERVES it). */
function CorrectAssignmentForm({
  assignment,
  token,
  onSaved,
  onClose,
}: {
  assignment: OcdAssignment;
  token: number;
  onSaved: (text: string) => void;
  onClose: () => void;
}) {
  const [start, setStart] = useState(berlinLocalDateTime(assignment.start_at));
  const [end, setEnd] = useState(berlinLocalDateTime(assignment.end_at));
  const [reported, setReported] = useState(
    assignment.reported_notice_at ? berlinLocalDateTime(assignment.reported_notice_at) : "",
  );
  const [notice, setNotice] = useState<Notice>(null);
  const { startIso, endIso, reportedIso, orderError, valid } = availabilityWindow(
    start,
    end,
    reported,
  );
  const saveMutation = useMutation({
    mutationFn: () =>
      correctOcdAssignment(assignment.id, {
        version: token,
        start_local: startIso as string,
        end_local: endIso as string,
        reported_notice_local: reportedIso,
      }),
    // The card owns the one notice channel, so a success is handed back to it
    // rather than rendered here: this form unmounts in the same commit.
    onSuccess: () => onSaved("Availability corrected."),
    onError: (error) => setNotice(errorNotice(error)),
  });
  // Ids are per row: two cards can be open for correction at the same time, and a
  // shared id would point both controls at the first card's message.
  const issueId = (field: string) => `ocd-correct-${assignment.id}-${field}-issue`;
  const requirementId = `ocd-correct-${assignment.id}-requirement`;
  const issueProps = (field: string) =>
    fieldIssueFor(saveMutation.error, field)
      ? { "aria-invalid": true as const, "aria-describedby": issueId(field) }
      : {};
  return (
    <fieldset className="interval-editor new-interval">
      <legend>Correct this availability</legend>
      <div className="editor-fields">
        <label>
          Starts
          <DateTimeField
            value={start}
            onChange={setStart}
            required
            {...issueProps("start_local")}
          />
        </label>
        <label>
          Ends
          <DateTimeField value={end} onChange={setEnd} required {...issueProps("end_local")} />
        </label>
        {orderError && (
          <p className="field-error" id={`ocd-correct-${assignment.id}-order-error`}>
            {orderError}
          </p>
        )}
      </div>
      <label>
        When were you told? (optional — recorded as reported)
        <DateTimeField
          value={reported}
          onChange={setReported}
          {...issueProps("reported_notice_local")}
        />
      </label>
      <p className="private-note">
        Clear this field to withdraw the notice. Kept as reported — it decides no allowance.
      </p>
      <div className="editor-actions">
        <button type="button" onClick={onClose}>
          Cancel
        </button>
        <button
          type="button"
          className="primary"
          onClick={() => saveMutation.mutate()}
          disabled={saveMutation.isPending || !valid}
          aria-describedby={valid ? undefined : requirementId}
        >
          Save correction
        </button>
      </div>
      {!valid && (
        <p className="field-requirement" id={requirementId} role="status">
          Enter the availability start and end as DD.MM.YYYY HH:MM — for example 19.07.2026 19:00. A
          notice you type must use the same form.
        </p>
      )}
      <ActionNotice notice={notice} />
      <FieldIssue error={saveMutation.error} field="start_local" id={issueId("start_local")} />
      <FieldIssue error={saveMutation.error} field="end_local" id={issueId("end_local")} />
      <FieldIssue
        error={saveMutation.error}
        field="reported_notice_local"
        id={issueId("reported_notice_local")}
      />
    </fieldset>
  );
}

function OcdAssignmentCard({
  assignment,
  canPlan,
}: {
  assignment: OcdAssignment;
  canPlan: boolean;
}) {
  const queryClient = useQueryClient();
  const [showAddCall, setShowAddCall] = useState(false);
  const [mode, setMode] = useState<"ON_SITE" | "REMOTE">("ON_SITE");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [travelOut, setTravelOut] = useState("");
  const [travelBack, setTravelBack] = useState("");
  const [comment, setComment] = useState("");
  const [notice, setNotice] = useState<Notice>(null);
  const [preview, setPreview] = useState<OcdCompensationPreview | null>(null);
  const [election, setElection] = useState<"PAY" | "TIME" | "">("");
  const [editing, setEditing] = useState(false);
  const refresh = () => invalidateFactDerived(queryClient);
  // Provenance is read through the same fallback the API documents: `OcdOut`
  // reports the fact as `source`, a newer payload as `plan_source`.
  const source = assignment.plan_source ?? assignment.source;
  const employeeRecorded = source === "EMPLOYEE_RECORDED";
  const token = versionToken(assignment);
  // The availability is writable only while it is the current SCHEDULED head, has
  // no calls at all (the backend's D3 freeze refuses edit and cancel once a call
  // exists, a voided one included), was written by the employee, and this session
  // holds the OCD domain's own capability. Anything else would be an action that
  // can only fail.
  const writable =
    canPlan &&
    employeeRecorded &&
    assignment.status === "SCHEDULED" &&
    assignment.calls.length === 0 &&
    token !== null;
  // The freeze is stated in words instead of being offered as a button that can
  // only fail — and only to someone who could otherwise act on it.
  const frozen =
    canPlan && employeeRecorded && assignment.status === "SCHEDULED" && assignment.calls.length > 0;
  // The app states the field's own rule before the browser can enforce it
  // silently: a native `pattern` mismatch blocks the `submit` event, leaving an
  // enabled button that does nothing (acceptance E3).
  const callValid = toIsoDateTime(start) !== null && toIsoDateTime(end) !== null;
  // A message beside a field is not enough — the CONTROL must declare itself
  // invalid and point at the message, or a screen-reader user cannot get from
  // the field to its rejection (review R6, same wiring as R5/R9).
  const addIssueProps = (field: string, id: string) =>
    fieldIssueFor(addMutation.error, field)
      ? { "aria-invalid": true as const, "aria-describedby": id }
      : {};
  // A compensation preview is a snapshot of the computation over the RECORDED
  // calls. Adding, correcting or voiding one leaves the snapshot describing an
  // assignment that no longer exists, so it is cleared rather than left to
  // present itself as current; the next Preview re-derives it. The signature is
  // the calls' own identity + version, so an unrelated re-render keeps it.
  const callsSignature = assignment.calls
    .map((call) => `${call.id}:${call.version}:${call.status}`)
    .join("|");
  useEffect(() => setPreview(null), [callsSignature]);

  const addMutation = useMutation({
    mutationFn: () =>
      createOcdCall({
        ocd_assignment_id: assignment.id,
        call_start_local: start,
        call_end_local: end,
        mode,
        travel_out_minutes: mode === "ON_SITE" && travelOut !== "" ? Number(travelOut) : null,
        travel_back_minutes: mode === "ON_SITE" && travelBack !== "" ? Number(travelBack) : null,
        comment: comment || null,
      }),
    onSuccess: () => {
      setShowAddCall(false);
      setStart("");
      setEnd("");
      setTravelOut("");
      setTravelBack("");
      setComment("");
      setNotice(okNotice("Call recorded together with its work time."));
      setPreview(null);
      refresh();
    },
    onError: (error) => setNotice(errorNotice(error)),
  });

  const previewMutation = useMutation({
    mutationFn: () => fetchOcdCompensationPreview(assignment.id),
    onSuccess: setPreview,
  });

  // Cancelling the AVAILABILITY (INV-10) is a lifecycle write on the plan, not on
  // any call: nothing recorded is erased, so the previously planned window stays
  // reproducible. The token is the row's own; the guard keeps a guessed one from
  // ever reaching the server if `writable` and the token ever drift apart.
  const cancelMutation = useMutation({
    mutationFn: () => {
      if (token === null) throw new Error("this availability reports no version token");
      return cancelOcdAssignment(assignment.id, token);
    },
    onSuccess: () => {
      setEditing(false);
      setNotice(okNotice("Availability cancelled."));
      setPreview(null);
      refresh();
    },
    onError: (error) => setNotice(errorNotice(error)),
  });

  const electionQuery = useQuery({
    queryKey: ["election", "OCD", assignment.id],
    queryFn: () => fetchCompensationElection("OCD", assignment.id),
    enabled: assignment.status === "SCHEDULED",
  });
  const electionMutation = useMutation({
    mutationFn: (modeValue: "PAY" | "TIME") =>
      saveCompensationElection({ context_type: "OCD", context_id: assignment.id, mode: modeValue }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["election", "OCD", assignment.id] });
      // The preview is a snapshot of the computation and the election is one of
      // its inputs, so it is dropped rather than left stating the previous
      // choice (review R18). The next Preview re-derives it.
      setPreview(null);
      setNotice(okNotice("Compensation choice saved."));
    },
    onError: (error) => setNotice(errorNotice(error)),
  });

  const canRecord = assignment.status === "SCHEDULED";
  const currentElection = electionQuery.data ?? null;
  const displayedElection = election || currentElection?.mode || "";

  return (
    <section
      className="shift-card ocd-card"
      aria-label={`OCD assignment ${visibleInstant(assignment.start_at)} to ${visibleInstant(assignment.end_at)}`}
    >
      <div className="day-heading">
        <div>
          <h2>On-call duty</h2>
        </div>
        {/* §6.2 fail-closed provenance, keyed on the same positive test as the
         *  label: only `EMPLOYEE_RECORDED` earns the badge. An authoritative
         *  source keeps the institutional wording and an unknown one gets
         *  neither — an approval claim nobody made is the one outcome that must
         *  never happen. */}
        {employeeRecorded && <span className="source-badge">Recorded by you</span>}
        <span className="policy-badge">
          {assignmentStatusLabels[assignment.status] ?? assignment.status}
        </span>
      </div>
      {employeeRecorded && <p className="private-note">Employee-entered plan.</p>}
      {/* The window is stated once, under the provenance that labels it — never
       *  hard-coded: `planSourceLabel` is the fail-closed three-way label, so an
       *  availability nobody approved cannot read "Approved plan".
       *
       *  The NOUN is passed explicitly. The default is "shift", and this is an
       *  on-call availability: without it the card told the employee "Planned
       *  shift" about a duty that is not a shift, on the one page whose whole
       *  subject is the distinction (INV-10). The disclosed OCD package already
       *  passes the noun; this surface was the one still saying the wrong word. */}
      <dl className="shift-times">
        <div>
          <dt>{planSourceLabel(source, "on-call duty")}</dt>
          <dd>
            {visibleInstant(assignment.start_at)} → {visibleInstant(assignment.end_at)}
          </dd>
        </div>
      </dl>
      {assignment.scheduled_at && (
        <p className="private-note">
          Scheduled/communicated {visibleInstant(assignment.scheduled_at)}
        </p>
      )}
      {/* REPORTED, not authoritative: this instant is the employee's own account
       *  of when they were told. No classifier reads it, so it decides no
       *  allowance — saying so is what keeps it from reading like the
       *  institutional notice time above. */}
      {assignment.reported_notice_at && (
        <p className="private-note">
          Notice you reported: {visibleInstant(assignment.reported_notice_at)} — kept as reported.
          It is not the Company's notice time and decides no allowance.
        </p>
      )}
      <h3 className="calls-heading">Calls (effective work)</h3>
      {assignment.calls.length === 0 ? (
        <p className="private-note">No calls recorded.</p>
      ) : (
        <ul className="ocd-calls">
          {assignment.calls.map((call) => (
            <CallRow key={call.id} call={call} onFactsChanged={() => setPreview(null)} />
          ))}
        </ul>
      )}
      {canRecord && !showAddCall && (
        <button type="button" className="add-interval" onClick={() => setShowAddCall(true)}>
          Add call
        </button>
      )}
      {canRecord && showAddCall && (
        <form
          className="ocd-call-form"
          onSubmit={(event) => {
            event.preventDefault();
            addMutation.mutate();
          }}
        >
          <label>
            Mode
            <select
              value={mode}
              onChange={(event) => setMode(event.target.value as "ON_SITE" | "REMOTE")}
            >
              <option value="ON_SITE">On-site</option>
              <option value="REMOTE">Remote</option>
            </select>
          </label>
          <label>
            Call start
            <DateTimeField
              value={start}
              onChange={setStart}
              required
              {...addIssueProps("call_start_local", "ocd-call-start-issue")}
            />
          </label>
          <label>
            Call end
            <DateTimeField
              value={end}
              onChange={setEnd}
              required
              {...addIssueProps("call_end_local", "ocd-call-end-issue")}
            />
          </label>
          {mode === "ON_SITE" && (
            <>
              <label>
                Travel to site (min)
                <input
                  type="number"
                  min={0}
                  step={5}
                  value={travelOut}
                  onChange={(event) => setTravelOut(event.target.value)}
                />
              </label>
              <label>
                Travel back (min)
                <input
                  type="number"
                  min={0}
                  step={5}
                  value={travelBack}
                  onChange={(event) => setTravelBack(event.target.value)}
                />
              </label>
            </>
          )}
          <label className="wide-field">
            Comment (private)
            <textarea
              value={comment}
              onChange={(event) => setComment(event.target.value)}
              maxLength={500}
            />
          </label>
          <button
            type="submit"
            disabled={addMutation.isPending || !callValid}
            aria-describedby={callValid ? undefined : "ocd-call-requirement"}
          >
            Record call + work time
          </button>
          {!callValid && (
            <p className="field-requirement" id="ocd-call-requirement" role="status">
              Enter the call start and end as DD.MM.YYYY HH:MM — for example 19.10.2026 23:00.
            </p>
          )}
          <FieldIssue
            error={addMutation.error}
            field="call_start_local"
            id="ocd-call-start-issue"
          />
          <FieldIssue error={addMutation.error} field="call_end_local" id="ocd-call-end-issue" />
          <button type="button" onClick={() => setShowAddCall(false)}>
            Cancel
          </button>
        </form>
      )}
      {/* The availability's own lifecycle. Both controls are withheld together
       *  because the server refuses both for the same reasons, and each refusal
       *  is stated in words rather than hidden behind a button that can only
       *  fail. */}
      {frozen && (
        <p className="private-note" role="status">
          This availability already has recorded calls, so it can no longer be corrected or
          cancelled — its calls stay linked to it.
        </p>
      )}
      {writable && !editing && (
        <div className="workspace-actions">
          <button type="button" onClick={() => setEditing(true)}>
            Correct
          </button>
          {/* Two-step: cancelling an availability is a lifecycle write, and a
           *  single mis-click must not perform it (the ConfirmAction contract). */}
          <ConfirmAction
            label="Cancel"
            confirmLabel="Cancel this availability"
            className="danger-ghost"
            busy={cancelMutation.isPending}
            onConfirm={() => cancelMutation.mutate()}
          />
        </div>
      )}
      {writable && editing && token !== null && (
        <CorrectAssignmentForm
          assignment={assignment}
          token={token}
          onClose={() => setEditing(false)}
          onSaved={(text) => {
            setEditing(false);
            setNotice(okNotice(text));
          }}
        />
      )}
      {/* One channel per card: a new attempt replaces the previous message, so a
       *  stale success can never sit beside a current failure (acceptance E4). */}
      <ActionNotice notice={notice} />
      {assignment.findings.length > 0 && (
        <ul className="shift-findings">
          {assignment.findings.map((finding) => (
            <li
              key={`${finding.rule_id}-${finding.message}`}
              className={`severity-${finding.status.toLowerCase()}`}
            >
              <strong>{finding.message}</strong>
            </li>
          ))}
        </ul>
      )}
      {canRecord && (
        <div className="election-row">
          <fieldset className="election-picker">
            <legend>Compensation election</legend>
            {(["PAY", "TIME"] as const).map((choice) => (
              <label key={choice} className="checkbox-label">
                <input
                  type="radio"
                  name={`compensation-election-${assignment.id}`}
                  checked={displayedElection === choice}
                  onChange={() => {
                    setElection(choice);
                    electionMutation.mutate(choice);
                  }}
                  disabled={electionMutation.isPending}
                />
                {choice === "PAY" ? "Paid out" : "Time compensation"}
              </label>
            ))}
          </fieldset>
          {electionQuery.isError && <p className="private-note">Election status unavailable.</p>}
        </div>
      )}
      <div className="workspace-actions">
        <button
          type="button"
          onClick={() => previewMutation.mutate()}
          disabled={previewMutation.isPending}
        >
          Preview OCD compensation
        </button>
      </div>
      {preview && (
        <aside className="preview" aria-label="Private OCD compensation preview">
          <strong>OCD compensation preview — not shared</strong>
          <p>
            Only this assignment’s calls, required travel, classified minutes, duty classification,
            and your election are included. Ordinary intervals are excluded. No monetary amount is
            calculated.
          </p>
          {preview.duty && (
            <>
              <p>
                Duty {plainMinutes(Math.round(preview.duty.duty_minutes))} ·{" "}
                {preview.duty.duty_kind === "DUTY_GE_12H"
                  ? (preview.duty.units ?? "—") + " hourly-wage unit(s)"
                  : "12.5% hourly basis"}
                {" · "}
                {shortNoticeLabel(preview.duty.short_notice)}
              </p>
              {/* The assignment's own finding below and this classification are
               *  about different rules that both say "short notice" (acceptance
               *  G4/H1). Neither threshold changes; saying so is the fix. */}
              <p className="private-note">{shortNoticeRuleNote}</p>
            </>
          )}
          {preview.classified_components.length > 0 ? (
            <ul>
              {preview.classified_components.map((component) => (
                <li key={component.component}>
                  {componentLabel(component.component)}: {plainMinutes(component.minutes)}
                  {component.time_equivalent_minutes != null
                    ? ` (${plainMinutes(component.time_equivalent_minutes)} time equivalent)`
                    : ""}
                </li>
              ))}
            </ul>
          ) : (
            <p className="private-note">No classified components yet.</p>
          )}
          {preview.election && (
            <p className="private-note">
              Election:{" "}
              {preview.election.mode
                ? (electionLabels[preview.election.mode] ?? preview.election.mode)
                : "not chosen"}
            </p>
          )}
          {preview.findings.map((finding) => (
            <p key={finding.rule_id} className={`severity-${finding.status.toLowerCase()}`}>
              <strong>{finding.message}</strong>
            </p>
          ))}
        </aside>
      )}
    </section>
  );
}

export function OcdPage({ month }: { month: string }) {
  const queryClient = useQueryClient();
  const { from, to } = monthRange(month);
  const ocdQuery = useQuery({
    queryKey: ["ocd", month],
    queryFn: () => fetchOcd(from, to),
  });
  // The SAME `["identity"]` cache entry the shell already reads, so the add
  // control cannot disagree with the navigation about this session.
  //
  // Deliberately NOT part of the blocking gate below: the capability only decides
  // whether an OPTIONAL control is offered, so a slow or failed identity read must
  // not hold the page hostage. An absent capability is read as "not granted"
  // (`?? []`), never as "allowed" — the fail-closed direction.
  const identityQuery = useQuery({ queryKey: ["identity"], queryFn: fetchIdentity });
  const canPlanOcd = (identityQuery.data?.capabilities ?? []).includes("OCD_PLAN_ENTRY_WRITE");

  const monthTitle = new Intl.DateTimeFormat("en", {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(`${month}-01T00:00:00Z`));
  if (ocdQuery.isPending) return <p role="status">Loading your private OCD view…</p>;
  if (ocdQuery.isError)
    return (
      <p className="status-error">Could not load your OCD view. Your saved facts are unchanged.</p>
    );
  const assignments = Array.isArray(ocdQuery.data) ? ocdQuery.data : [];

  return (
    <section className="workspace" aria-label="On-call duty assignments and calls">
      <div className="workspace-heading">
        <div>
          <p className="eyebrow">Private — visible only to you</p>
          <h1>On-call duty — {monthTitle}</h1>
        </div>
      </div>
      <MonthContextNav month={month} current="ocd" />
      <p className="private-note">
        Availability assignments and effective calls stay separate. A call becomes working time
        (including required travel) only when you record it.
      </p>
      {/* Offered only while this session holds the OCD domain's own switch: the
       *  availability-planning path can move to a controlled source without
       *  touching shift entry (and vice versa). */}
      {canPlanOcd && (
        <AddOcdAssignmentForm onFactsChanged={() => invalidateFactDerived(queryClient)} />
      )}
      {assignments.length === 0 ? (
        <p className="private-note">No OCD assignments in this month.</p>
      ) : (
        assignments.map((assignment) => (
          <OcdAssignmentCard key={assignment.id} assignment={assignment} canPlan={canPlanOcd} />
        ))
      )}
    </section>
  );
}
