import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useRef, useState } from "react";
import { createPortal } from "react-dom";

import {
  createSubmission,
  fetchOcd,
  fetchOcdCompensationPreview,
  fetchOvertimePreview,
  fetchShiftReportPreview,
  fetchShifts,
  fetchSubmissionRecipients,
  fetchSubmissions,
  monthRange,
  type SubmissionPurpose,
  withdrawSubmission,
} from "../lib/api";
import { currentBerlinMonth, plainMinutes } from "./month";
import { MonthContextNav } from "./month";
import { ConfirmAction } from "../lib/confirm";
import { berlinInstant, berlinRange } from "../lib/format";
import { MonthField, isoToGermanDate } from "../lib/inputs";
import { ApiError } from "../lib/api";
import { refusalText } from "../lib/notice";
import {
  authorizationStatusLabel,
  compensationReviewLabel,
  componentLabel,
  deviationTypeLabel,
  electionLabels,
  paidBreakLabel,
  planSourceLabel,
  shiftKindLabel,
  submissionStatusLabels,
  shortNoticeLabel,
  workTypeLabel,
} from "../lib/labels";

const labels: Record<SubmissionPurpose, string> = {
  // UI-036, resolved differently after verification: `ShiftReportPreviewOut`
  // always carries `actual_start_at`/`actual_end_at`, the deviation type, its
  // reason and decision and the paid-break status — the package was never
  // planned-only, so renaming it "Planned shift" would UNDERSTATE what the
  // employee is about to disclose. The incumbent name is accurate; the finding's
  // real user-facing complaint (the recipient cannot tell whether the shift was
  // worked as planned) is fixed in `HumanPreview`, which now always renders the
  // actual times and the deviation.
  SHIFT_REPORT: "Share Shift Report",
  OCD_COMPENSATION: "Submit OCD Compensation",
  OVERTIME_PAID: "Submit Overtime — Pay",
  OVERTIME_TIME_COMP: "Submit Overtime — Time compensation",
};
const shortNames: Record<SubmissionPurpose, string> = {
  SHIFT_REPORT: "Shift report",
  OCD_COMPENSATION: "OCD compensation",
  OVERTIME_PAID: "Overtime — Pay",
  OVERTIME_TIME_COMP: "Overtime — time compensation",
};
function isOvertimePurpose(purpose: SubmissionPurpose): boolean {
  return purpose === "OVERTIME_PAID" || purpose === "OVERTIME_TIME_COMP";
}

/* A compensation election only exists for on-call duty and overtime; a shift
 * report has no election, so naming one there is a term with no referent. */
function hasElection(purpose: SubmissionPurpose): boolean {
  return purpose === "OCD_COMPENSATION" || isOvertimePurpose(purpose);
}

/** The recipient-visible projection, in words.
 *
 *  Exported because the recipient queue renders the SAME component: the
 *  package payload IS this preview projection (disclosure/fingerprint.py
 *  payload_dict), so FE-SCREEN-007's "preview shows exactly what recipients
 *  see" holds by construction rather than by two renderers agreeing.
 *
 *  COMPLETENESS IS THE CONTRACT, not a nicety. This is the employee's only
 *  check on what a disclosure will contain and the recipient's only record of
 *  it, so every substantive payload field is rendered here — including the
 *  fields whose only earlier representation was the raw-JSON disclosure this
 *  phase removed (actual times, the deviation's reason and decision, the
 *  individual calls, the overtime intervals and authorizations). Only pure
 *  traceability is left out — `policy_version`, `policy_status`, `report_type`
 *  and the assignment UUIDs — because it names the machinery rather than the
 *  facts, and it stays in the API payload and on /admin. */
export function HumanPreview({
  purpose,
  preview,
  audience = "employee",
}: {
  purpose: SubmissionPurpose;
  preview: Record<string, unknown>;
  audience?: "employee" | "recipient";
}) {
  const components =
    (preview.classified_components as
      | Array<{ component: string; minutes: number; time_equivalent_minutes: number | null }>
      | undefined) ?? [];
  const findings =
    (preview.findings as Array<{ status: string; message: string; rule_id: string }> | undefined) ??
    [];
  const election = preview.election as { mode?: string | null } | null | undefined;
  const intervals =
    (preview.overtime_intervals as
      | Array<{
          start_at: string;
          end_at: string;
          work_type: string;
          elapsed_seconds: number;
          linked_authorization_id: string | null;
        }>
      | undefined) ?? [];
  const authorizations =
    (preview.authorizations as
      | Array<{
          id: string;
          valid_from: string;
          valid_to: string | null;
          purpose: string | null;
          status: string;
          source_ref: string | null;
        }>
      | undefined) ?? [];
  const calls =
    (preview.calls as
      | Array<{
          mode: string;
          call_start_at: string;
          call_end_at: string;
          travel_out_minutes: number | null;
          travel_back_minutes: number | null;
          span_start_at: string;
          span_end_at: string;
          elapsed_seconds: number;
        }>
      | undefined) ?? [];
  const duty = preview.duty as
    | {
        duty_minutes: number;
        duty_kind: string;
        units: number | null;
        short_notice: boolean | null;
      }
    | null
    | undefined;
  const linked = intervals.filter((item) => item.linked_authorization_id).length;
  /* Which authorization supports THIS interval. The payload carries the link
   * (`linked_authorization_id`); the list below is numbered so the two can be
   * read together without a UUID ever reaching the screen. */
  const referenceOf = (id: string | null): string | null => {
    if (!id) return null;
    const index = authorizations.findIndex((authorization) => authorization.id === id);
    return index >= 0 ? `reference ${index + 1}` : "reference not listed";
  };
  const callMinutes = (start: string, end: string): number =>
    Math.round((Date.parse(end) - Date.parse(start)) / 60_000);
  return (
    <div className="human-preview">
      <p>
        <strong>{shortNames[purpose]}</strong> · Employee reference:{" "}
        {String(preview.employee_identifier ?? "—")}
      </p>
      {purpose === "SHIFT_REPORT" && (
        <>
          <p>
            Planned: {berlinInstant(String(preview.planned_start_at ?? ""))} →{" "}
            {berlinInstant(String(preview.planned_end_at ?? ""))}
          </p>
          <p>
            Actual:{" "}
            {preview.actual_start_at || preview.actual_end_at
              ? `${berlinInstant(preview.actual_start_at as string | null, "—")} → ${berlinInstant(preview.actual_end_at as string | null, "—")}`
              : "not recorded yet"}
          </p>
          <p>
            Deviation:{" "}
            {preview.deviation_type
              ? deviationTypeLabel(String(preview.deviation_type))
              : "none recorded"}
          </p>
          {preview.noticed_or_decided_at ? (
            <p>Noticed or decided: {berlinInstant(String(preview.noticed_or_decided_at))}</p>
          ) : null}
          {preview.decision_maker_type ? (
            <p>Decided or reported by: {String(preview.decision_maker_type)}</p>
          ) : null}
          {preview.reason ? <p>Reason given: {String(preview.reason)}</p> : null}
          <p>Paid shift break: {paidBreakLabel(String(preview.paid_break_status ?? ""))}</p>
          {/* Provenance and the review status are what the recipient gets
           *  INSTEAD of an entitlement on a cancelled or aborted shift: the
           *  factual report is submittable and a human decides the compensation.
           *  Read through `planSourceLabel`, which is fail-closed — a source
           *  nobody approved can never read "Approved plan" here either. */}
          {preview.plan_source ? (
            <p>Plan source: {planSourceLabel(String(preview.plan_source))}</p>
          ) : null}
          {preview.compensation_review_status ? (
            <p>
              Compensation review:{" "}
              {compensationReviewLabel(String(preview.compensation_review_status))}
            </p>
          ) : null}
        </>
      )}
      {purpose === "OCD_COMPENSATION" && (
        <>
          <p>
            Assignment: {berlinInstant(String(preview.assignment_start_at ?? ""))} →{" "}
            {berlinInstant(String(preview.assignment_end_at ?? ""))}
          </p>
          {preview.plan_source ? (
            /* "on-call duty": this is the OCD package, and the same helper's
             * default noun would tell the recipient the employee planned a shift. */
            <p>Plan source: {planSourceLabel(String(preview.plan_source), "on-call duty")}</p>
          ) : null}
          {/* REPORTED, not authoritative: the employee's own account of when they
           *  were told. No classifier reads it, so it decides no allowance — and
           *  saying so is what keeps it from reading like an institutional notice
           *  time. */}
          {preview.reported_notice_at ? (
            <p>
              Notice (as reported): {berlinInstant(String(preview.reported_notice_at))} — kept as
              reported; it decides no allowance.
            </p>
          ) : null}
          {calls.length > 0 ? (
            <ul className="preview-calls">
              {calls.map((call, index) => (
                <li key={index}>
                  <span>
                    {berlinRange(String(call.call_start_at), String(call.call_end_at))} ·{" "}
                    {workTypeLabel(call.mode)} ·{" "}
                    {plainMinutes(
                      callMinutes(String(call.call_start_at), String(call.call_end_at)),
                    )}
                    {call.travel_out_minutes || call.travel_back_minutes
                      ? ` · travel out ${plainMinutes(call.travel_out_minutes ?? 0)}, back ${plainMinutes(
                          call.travel_back_minutes ?? 0,
                        )}`
                      : ""}
                  </span>
                  {/* The effective-work span is its own disclosed fact: it is the
                   * call window widened by the travel, and it is what the
                   * package credits. */}
                  <span className="preview-call-span">
                    Effective work{" "}
                    {berlinRange(String(call.span_start_at), String(call.span_end_at))} ·{" "}
                    {plainMinutes(Math.round(call.elapsed_seconds / 60))}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p>No calls recorded.</p>
          )}
          {duty ? (
            <p>
              Duty {plainMinutes(duty.duty_minutes)} ·{" "}
              {duty.duty_kind === "DUTY_GE_12H"
                ? `${duty.units ?? "—"} hourly-wage unit(s)`
                : "12.5% hourly basis"}
              {" · "}
              {shortNoticeLabel(duty.short_notice)}
            </p>
          ) : null}
        </>
      )}
      {isOvertimePurpose(purpose) && (
        <>
          <p>
            Period: {isoToGermanDate(String(preview.period_start ?? ""))} →{" "}
            {isoToGermanDate(String(preview.period_end ?? ""))}
          </p>
          <p>
            Overtime intervals ({intervals.length}): {linked} linked to an authorization reference
          </p>
          {intervals.length > 0 && (
            <ul className="preview-intervals">
              {intervals.map((item, index) => (
                <li key={index}>
                  {berlinRange(String(item.start_at), String(item.end_at))} ·{" "}
                  {workTypeLabel(item.work_type)} ·{" "}
                  {plainMinutes(Math.round(item.elapsed_seconds / 60))}
                  {item.linked_authorization_id
                    ? ` · ${referenceOf(item.linked_authorization_id)}`
                    : " — no authorization reference"}
                </li>
              ))}
            </ul>
          )}
          {authorizations.length > 0 && (
            <>
              <p>Authorization references ({authorizations.length}):</p>
              <ul className="preview-authorizations">
                {authorizations.map((authorization, index) => (
                  <li key={index}>
                    Reference {index + 1}: {berlinInstant(authorization.valid_from)} →{" "}
                    {berlinInstant(authorization.valid_to, "open-ended")} ·{" "}
                    {authorizationStatusLabel(authorization.status)}
                    {authorization.purpose ? ` · ${authorization.purpose}` : ""}
                    {authorization.source_ref ? ` · ${authorization.source_ref}` : ""}
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
      {components.length > 0 && (
        <ul className="preview-components">
          {components.map((component) => (
            <li key={component.component}>
              {componentLabel(component.component)}: {plainMinutes(component.minutes)}
              {component.time_equivalent_minutes != null
                ? ` (${plainMinutes(component.time_equivalent_minutes)} time equivalent)`
                : ""}
            </li>
          ))}
        </ul>
      )}
      {hasElection(purpose) && (
        <p className="private-note">
          Election:{" "}
          {election?.mode ? (electionLabels[election.mode] ?? election.mode) : "not chosen"}
        </p>
      )}
      <p className="private-note">
        {audience === "employee"
          ? `Things to check stay with you: ${findings.length} included in this preview, none shared.`
          : "Employee-private notes are never part of a package."}
      </p>
    </div>
  );
}

function PackageCard({ purpose, month }: { purpose: SubmissionPurpose; month: string }) {
  const queryClient = useQueryClient();
  const previewSlot = useContext(PreviewSlot);
  const { from, to } = monthRange(month);
  const [scopeId, setScopeId] = useState("");
  const [overtimeMonth, setOvertimeMonth] = useState(month);
  // Selecting a recipient is an explicit disclosure act (INV-05), so nothing
  // is pre-selected: the submit guard below blocks until one is chosen.
  const [selectedRecipients, setSelectedRecipients] = useState<string[]>([]);
  const [preview, setPreview] = useState<Record<string, unknown> | null>(null);
  const [fingerprint, setFingerprint] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  // The recipient list is server-controlled (the registry), never a constant.
  const recipientsQuery = useQuery({
    queryKey: ["submission-recipients"],
    queryFn: fetchSubmissionRecipients,
  });
  const shiftQuery = useQuery({
    queryKey: ["submission-shifts", month],
    queryFn: () => fetchShifts(from, to),
    enabled: purpose === "SHIFT_REPORT",
  });
  const ocdQuery = useQuery({
    queryKey: ["submission-ocd", month],
    queryFn: () => fetchOcd(from, to),
    enabled: purpose === "OCD_COMPENSATION",
  });
  // A preview answers for ONE scope. If the employee changes the scope while the
  // request is in flight, the response describes the OLD scope and must not
  // populate the panel: it would show assignment A's recipient projection while
  // B is selected, and carry A's fingerprint into a submit aimed at B (review
  // R19). The requested scope travels WITH the request, and a response whose
  // scope is no longer the selected one is discarded.
  const scopeKey =
    purpose === "SHIFT_REPORT" || purpose === "OCD_COMPENSATION" ? scopeId : overtimeMonth;
  const liveScope = useRef(scopeKey);
  liveScope.current = scopeKey;
  const previewMutation = useMutation({
    mutationFn: async (forScope: string) => {
      if (purpose === "SHIFT_REPORT") return fetchOcdOrShift(false, forScope, overtimeMonth);
      if (purpose === "OCD_COMPENSATION") return fetchOcdOrShift(true, forScope, overtimeMonth);
      const range = monthRange(forScope);
      return fetchOvertimePreview(range.from, range.to);
    },
    onSuccess: (data, forScope) => {
      if (forScope !== liveScope.current) return;
      // `preview_fingerprint` is declared on every preview response type, so no
      // cast is needed here — the type surface is what caught the drift.
      const { findings: _findings, preview_fingerprint: value, ...recipientPayload } = data;
      setPreview(recipientPayload);
      setFingerprint(value);
      setNotice(null);
    },
    onError: (error) => setNotice(refusalText(error)),
  });
  const submitMutation = useMutation({
    mutationFn: () =>
      createSubmission(purpose, {
        scope:
          purpose === "SHIFT_REPORT" || purpose === "OCD_COMPENSATION"
            ? { assignment_id: scopeId }
            : { month: overtimeMonth },
        preview_fingerprint: fingerprint,
        client_request_id: crypto.randomUUID(),
        recipients: selectedRecipients,
      }),
    onSuccess: () => {
      setNotice("Submitted. Recipients can read only this package.");
      setPreview(null);
      void queryClient.invalidateQueries({ queryKey: ["submissions"] });
    },
    onError: (error) => {
      // The CODE is the stable contract; matching on the server's sentence meant
      // a copy edit could silently turn this into a raw-message display.
      setNotice(
        error instanceof ApiError && error.code === "SUBMISSION_CHANGED"
          ? "Facts changed — re-preview before submitting."
          : refusalText(error),
      );
    },
  });
  const fetchOcdOrShift = (ocd: boolean, id: string, _selectedMonth: string) =>
    ocd ? fetchOcdCompensationPreview(id) : fetchShiftReportPreview(id);
  const needsAssignment = purpose === "SHIFT_REPORT" || purpose === "OCD_COMPENSATION";
  const recipients = Array.isArray(recipientsQuery.data) ? recipientsQuery.data : [];

  // Overtime packages are submittable only when every eligible
  // interval carries an authorization reference and an election is chosen.
  const otIntervals =
    (preview?.overtime_intervals as
      | Array<{ linked_authorization_id: string | null }>
      | undefined) ?? [];
  const otUnlinked = otIntervals.filter((item) => !item.linked_authorization_id).length;
  const otElectionChosen = Boolean(
    (preview?.election as { mode?: string | null } | undefined)?.mode,
  );
  const overtimeReady = isOvertimePurpose(purpose)
    ? preview !== null && otIntervals.length > 0 && otUnlinked === 0 && otElectionChosen
    : true;

  return (
    <section className="submission-card">
      <h2>{labels[purpose]}</h2>
      {needsAssignment ? (
        <label>
          Assignment
          <select
            value={scopeId}
            onChange={(event) => {
              setScopeId(event.target.value);
              setPreview(null);
              setFingerprint("");
            }}
          >
            <option value="">Choose an assignment</option>
            {purpose === "SHIFT_REPORT"
              ? (Array.isArray(shiftQuery.data) ? shiftQuery.data : []).map((assignment) => (
                  <option key={assignment.id} value={assignment.id}>
                    {`${shiftKindLabel(assignment.shift_kind)} · ${berlinRange(String(assignment.approved_revision.planned_start_at), String(assignment.approved_revision.planned_end_at))}`}
                  </option>
                ))
              : (Array.isArray(ocdQuery.data) ? ocdQuery.data : []).map((assignment) => (
                  <option key={assignment.id} value={assignment.id}>
                    {`On-call duty · ${berlinRange(String(assignment.start_at), String(assignment.end_at))}`}
                  </option>
                ))}
          </select>
        </label>
      ) : (
        <label>
          Overtime month
          <MonthField
            value={overtimeMonth}
            onChange={(value) => {
              setOvertimeMonth(value);
              setPreview(null);
              setFingerprint("");
            }}
          />
        </label>
      )}
      <p className="private-note">
        {needsAssignment ? "Choose an assignment to preview." : "Uses this month."}
      </p>
      <div className="recipient-picker">
        <span>Recipients</span>
        {recipientsQuery.isPending ? (
          <span role="status">Loading recipients…</span>
        ) : recipientsQuery.isError ? (
          <span className="status-error">Could not load the recipient list.</span>
        ) : recipients.length === 0 ? (
          <span className="status-muted">
            No recipient is configured — nothing can be shared yet.
          </span>
        ) : (
          recipients.map((recipient) => (
            <label key={recipient.subject}>
              <input
                type="checkbox"
                checked={selectedRecipients.includes(recipient.subject)}
                disabled={!recipient.readable}
                onChange={() =>
                  setSelectedRecipients((current) =>
                    current.includes(recipient.subject)
                      ? current.filter((value) => value !== recipient.subject)
                      : [...current, recipient.subject],
                  )
                }
              />{" "}
              {recipient.subject}
              {!recipient.readable && (
                <span className="status-muted" role="note">
                  — cannot read submissions yet, so a package sent here could not be opened. Ask an
                  administrator to enable this recipient.
                </span>
              )}
            </label>
          ))
        )}
      </div>
      <button
        type="button"
        // 7B: four cards used to expose the identical accessible name "Preview
        // exact package", so a screen-reader user could not tell them apart. The
        // visible label stays; the accessible name is a superset of it, which
        // keeps WCAG 2.5.3 (label in name) satisfied while making each card's
        // button distinct.
        aria-label={`Preview exact package — ${shortNames[purpose]}`}
        onClick={() => previewMutation.mutate(scopeKey)}
        disabled={previewMutation.isPending || (needsAssignment && !scopeId)}
      >
        Preview exact package
      </button>
      {preview &&
        previewSlot &&
        createPortal(
          <section className="preview-panel" aria-label="What the recipient will see">
            <h3>What the recipient will see</h3>
            <HumanPreview purpose={purpose} preview={preview} />
            {isOvertimePurpose(purpose) && !overtimeReady && (
              <ul className="readiness-checklist" aria-label="Not ready to submit">
                <li className={otIntervals.length > 0 ? "ready-ok" : ""}>
                  {otIntervals.length > 0
                    ? "Overtime intervals are in the preview."
                    : "No overtime intervals in this month — record them in the day editor first."}
                </li>
                <li className={otUnlinked === 0 ? "ready-ok" : ""}>
                  {otUnlinked === 0
                    ? "Every overtime interval has an authorization reference."
                    : `${otUnlinked} overtime interval(s) still need an authorization reference — link them on the Overtime page.`}
                </li>
                <li className={otElectionChosen ? "ready-ok" : ""}>
                  {otElectionChosen
                    ? "Compensation election chosen."
                    : "Choose a compensation election (paid out or time compensation) on the Overtime page."}
                </li>
              </ul>
            )}
            {/* A primary that cannot proceed states its own requirement, in the
             *  form's words, and the control points at it (acceptance O2). This
             *  is a readiness line rather than a validation error: it holds
             *  whether the submit reads as disabled or as enabled-and-inert. */}
            {selectedRecipients.length === 0 && (
              <p
                className="field-requirement"
                id={`submit-recipient-requirement-${purpose}`}
                role="status"
              >
                Select at least one recipient — a package is readable only by the recipients you
                name.
              </p>
            )}
            <button
              type="button"
              className="primary"
              onClick={() => submitMutation.mutate()}
              disabled={
                submitMutation.isPending ||
                selectedRecipients.length === 0 ||
                (isOvertimePurpose(purpose) && !overtimeReady)
              }
              aria-describedby={
                selectedRecipients.length === 0
                  ? `submit-recipient-requirement-${purpose}`
                  : undefined
              }
            >
              Submit this {shortNames[purpose]} package
            </button>
          </section>,
          previewSlot,
        )}
      {notice && (
        <p
          role="status"
          className={
            submitMutation.isError || previewMutation.isError ? "status-error" : "status-ok"
          }
        >
          {notice}
        </p>
      )}
    </section>
  );
}

/** Where a card's preview panel is placed. The panel belongs to the card's
 *  state but must not sit inside the card: the four cards share a two-column
 *  grid, so a taller card would push the next row down and move its neighbours
 *  (UI-043). The page owns the slot; each card portals its panel into it. */
const PreviewSlot = createContext<HTMLElement | null>(null);

export function SubmissionsPage({ month = currentBerlinMonth() }: { month?: string }) {
  const queryClient = useQueryClient();
  const packages = useQuery({ queryKey: ["submissions"], queryFn: fetchSubmissions });
  const [withdrawNotice, setWithdrawNotice] = useState<string | null>(null);
  const [previewSlot, setPreviewSlot] = useState<HTMLDivElement | null>(null);
  const withdraw = useMutation({
    mutationFn: withdrawSubmission,
    onSuccess: () => {
      setWithdrawNotice("Package withdrawn — recipients can no longer open it.");
      void queryClient.invalidateQueries({ queryKey: ["submissions"] });
    },
    onError: (error) => setWithdrawNotice(refusalText(error)),
  });
  return (
    <section className="submissions-page">
      <p className="eyebrow">Employee-controlled sharing</p>
      <h1>Submissions &amp; Sharing</h1>
      <p>
        Each package is purpose-specific; the preview shows exactly what the selected recipients
        will see.
      </p>
      <MonthContextNav month={month} current="submissions" />
      <PreviewSlot.Provider value={previewSlot}>
        <div className="submission-grid">
          {(Object.keys(labels) as SubmissionPurpose[]).map((purpose) => (
            <PackageCard key={purpose} purpose={purpose} month={month} />
          ))}
        </div>
        <div className="preview-slot" ref={setPreviewSlot} />
      </PreviewSlot.Provider>
      <h2>Your packages</h2>
      {packages.isPending ? (
        <p role="status">Loading packages…</p>
      ) : packages.isError ? (
        <p className="status-error">Could not load packages.</p>
      ) : packages.data.length === 0 ? (
        <section className="standalone-card">
          <p className="empty-state">No shared packages yet.</p>
        </section>
      ) : (
        <ul className="package-list">
          {packages.data.map((item) => (
            <li key={item.id}>
              <strong>{labels[item.purpose]}</strong> ·{" "}
              {item.period_start === item.period_end
                ? isoToGermanDate(item.period_start)
                : `${isoToGermanDate(item.period_start)}–${isoToGermanDate(item.period_end)}`}{" "}
              · Version {item.version} · {submissionStatusLabels[item.status] ?? item.status}
              {item.supersedes_id && " (supersedes an earlier package)"}
              <br />
              Recipients:{" "}
              {item.recipients.map((recipient) => recipient.recipient_principal).join(", ")}
              {item.status === "SUBMITTED" && (
                <ConfirmAction
                  label="Withdraw"
                  confirmLabel="Withdraw package"
                  busy={withdraw.isPending}
                  onConfirm={() => withdraw.mutate(item.id)}
                />
              )}
            </li>
          ))}
        </ul>
      )}
      {withdrawNotice && (
        <p role="status" className={withdraw.isError ? "status-error" : "status-ok"}>
          {withdrawNotice}
        </p>
      )}
    </section>
  );
}
