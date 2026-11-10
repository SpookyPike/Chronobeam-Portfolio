import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import {
  correctOvertimeAuthorization,
  createOvertimeAuthorization,
  fetchCompensationElection,
  fetchOvertimeAuthorizations,
  fetchOvertimePreview,
  monthRange,
  saveCompensationElection,
  updateInterval,
  type OvertimeAuthorization,
  type OvertimePreview,
} from "../lib/api";
import { plainMinutes } from "./month";
import { MonthContextNav } from "./month";
import { berlinInstant, berlinLocalDateTime } from "../lib/format";
import { DateTimeField } from "../lib/inputs";
import {
  accessRuleLabel,
  authorizationStatusLabel,
  componentLabel,
  electionLabels,
  workTypeLabel,
} from "../lib/labels";
import { invalidateFactDerived } from "../lib/query";
import { refusalText } from "../lib/notice";

/** The only statuses the employee's own correction may set (ACCESS-017): the
 *  approval itself is an institutional decision, so it is not offered here. */
const employeeAuthStatuses = ["REQUESTED", "CANCELLED"] as const;

function visibleInstant(instant: string | null): string {
  return berlinInstant(instant, "—");
}

/** The correction draft for a row: the values the form opens with. */
function authorizationDraft(authorization: OvertimeAuthorization) {
  return {
    validFrom: berlinLocalDateTime(authorization.valid_from),
    validTo: authorization.valid_to ? berlinLocalDateTime(authorization.valid_to) : "",
    purpose: authorization.purpose ?? "",
    // The employee's own correction offers REQUESTED/CANCELLED only. A legacy
    // self-declared decision is shown as-is until the employee picks one of the
    // two, so opening the form never silently rewrites the row.
    status: (authorization.status === "CANCELLED"
      ? "CANCELLED"
      : "REQUESTED") as (typeof employeeAuthStatuses)[number],
    sourceRef: authorization.source_ref ?? "",
  };
}

function AuthorizationRow({
  authorization,
  month,
}: {
  authorization: OvertimeAuthorization;
  month: string;
}) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(() => authorizationDraft(authorization));
  // Whether the employee actually MOVED the select. A legacy self-declared
  // APPROVED/REJECTED row starts the select on REQUESTED, so comparing the
  // selection to the row's status would send `status: "REQUESTED"` on a
  // metadata-only save and silently rewrite the versioned head.
  const [statusTouched, setStatusTouched] = useState(false);

  // Opening the editor always starts from the row's CURRENT values. A draft that
  // survived a Cancel would otherwise be saved by a later unrelated edit — a
  // status the employee had abandoned when they pressed Cancel.
  const resetDraft = () => {
    setDraft(authorizationDraft(authorization));
    setStatusTouched(false);
  };
  const patch = (values: Partial<ReturnType<typeof authorizationDraft>>) =>
    setDraft((current) => ({ ...current, ...values }));
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  // `overtime` is this surface's own record list (a policy record, not a
  // fact-derived view), so it is refreshed explicitly alongside the shared
  // fact-derived families: an authorization change moves the month's overtime
  // findings and credit.
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["overtime", month] });
    invalidateFactDerived(queryClient);
  };

  // The employee may version ONLY a head they authored: the server refuses every
  // /me write on any other provenance, DENY BY DEFAULT. So the control follows
  // the same predicate — an unknown/future source is read-only too, not a button
  // that always fails.
  const employeeWritable = authorization.source === "EMPLOYEE_RECORDED";
  const institutionallyDecided = authorization.source === "INSTITUTIONAL_APPROVAL";
  const selfDeclaredDecision =
    employeeWritable &&
    (authorization.status === "APPROVED" || authorization.status === "REJECTED");

  const exitActive = authorization.status === "REJECTED" || authorization.status === "CANCELLED";
  const electionQuery = useQuery({
    queryKey: ["election", "OVERTIME", authorization.id],
    queryFn: () => fetchCompensationElection("OVERTIME", authorization.id),
    enabled: !exitActive,
  });
  const electionMutation = useMutation({
    mutationFn: (mode: "PAY" | "TIME") =>
      saveCompensationElection({ context_type: "OVERTIME", context_id: authorization.id, mode }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["election", "OVERTIME", authorization.id] });
      // An election is an INPUT to the compensation preview, so the derived
      // views move with it — not just the election read itself.
      invalidateFactDerived(queryClient);
      setNotice({ kind: "ok", text: "Compensation choice saved." });
    },
    onError: (error) => setNotice({ kind: "error", text: refusalText(error) }),
  });
  const saveMutation = useMutation({
    mutationFn: () =>
      correctOvertimeAuthorization(authorization.id, {
        version: authorization.version,
        valid_from: draft.validFrom,
        valid_to: draft.validTo || null,
        purpose: draft.purpose || null,
        source_ref: draft.sourceRef || null,
        // Presence-based: a select the employee never moved must not carry a
        // status, so an untouched save cannot rewrite the versioned head.
        ...(statusTouched ? { status: draft.status } : {}),
      }),
    onSuccess: () => {
      setEditing(false);
      setNotice({ kind: "ok", text: "Authorization reference updated." });
      refresh();
    },
    onError: (error) => setNotice({ kind: "error", text: refusalText(error) }),
  });

  const election = electionQuery.data ?? null;

  return (
    <li className="ot-auth">
      {!editing ? (
        <>
          <p>
            <strong>
              {visibleInstant(authorization.valid_from)} → {visibleInstant(authorization.valid_to)}
            </strong>{" "}
            <span className="work-type-chip">{authorizationStatusLabel(authorization.status)}</span>
          </p>
          {(authorization.purpose || authorization.source_ref) && (
            <p className="private-note">
              {authorization.purpose ? `Purpose: ${authorization.purpose}` : ""}
              {authorization.source_ref ? ` · Reference: ${authorization.source_ref}` : ""}
            </p>
          )}
          <p className="private-note">
            {institutionallyDecided
              ? `Institutional decision${
                  authorization.authorized_by ? ` by ${authorization.authorized_by}` : ""
                } — read-only.`
              : !employeeWritable
                ? "Read-only."
                : selfDeclaredDecision
                  ? "Employee-recorded — it does not authorize a submission."
                  : "A request — only an institutional decision approves overtime."}
          </p>
          {!exitActive && !election && (
            <p className="private-note">No compensation election yet.</p>
          )}
          {!exitActive && election && (
            <p className="private-note">
              Election: {election.mode === "PAY" ? "paid out" : "time compensation"}.
            </p>
          )}
          <div className="workspace-actions">
            <button
              type="button"
              onClick={() => {
                resetDraft();
                setEditing(true);
              }}
              disabled={!employeeWritable}
            >
              Correct
            </button>
          </div>
          {!exitActive && (
            <div className="election-row">
              <fieldset className="election-picker">
                <legend>Compensation election</legend>
                {(["PAY", "TIME"] as const).map((choice) => (
                  <label key={choice} className="checkbox-label">
                    <input
                      type="radio"
                      name={`compensation-election-${authorization.id}`}
                      checked={election?.mode === choice}
                      onChange={() => electionMutation.mutate(choice)}
                      disabled={electionMutation.isPending}
                    />
                    {choice === "PAY" ? "Paid out" : "Time compensation"}
                  </label>
                ))}
              </fieldset>
            </div>
          )}
          {notice?.kind === "ok" && (
            <p role="status" className="status-ok">
              {notice.text}
            </p>
          )}
          {notice?.kind === "error" && (
            <p role="status" className="status-error">
              {notice.text}
            </p>
          )}
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
            Valid from
            <DateTimeField
              value={draft.validFrom}
              onChange={(value) => patch({ validFrom: value })}
              required
            />
          </label>
          <label>
            Valid to
            <DateTimeField value={draft.validTo} onChange={(value) => patch({ validTo: value })} />
          </label>
          <p className="private-note">
            Decision on your record: {authorizationStatusLabel(authorization.status)}. You can
            request or cancel your own request; approving is an institutional decision and is not
            available here.
          </p>
          <label>
            Status
            <select
              value={draft.status}
              onChange={(event) => {
                patch({ status: event.target.value as (typeof employeeAuthStatuses)[number] });
                setStatusTouched(true);
              }}
            >
              {employeeAuthStatuses.map((choice) => (
                <option key={choice} value={choice}>
                  {authorizationStatusLabel(choice)}
                </option>
              ))}
            </select>
          </label>
          <label>
            Purpose
            <input
              value={draft.purpose}
              onChange={(event) => patch({ purpose: event.target.value })}
              maxLength={120}
            />
          </label>
          <label>
            Source / reference
            <input
              value={draft.sourceRef}
              onChange={(event) => patch({ sourceRef: event.target.value })}
              maxLength={120}
            />
          </label>
          <button type="submit" disabled={saveMutation.isPending}>
            Save correction
          </button>
          <button
            type="button"
            onClick={() => {
              setEditing(false);
              resetDraft();
            }}
          >
            Cancel
          </button>
          {notice?.kind === "error" && (
            <p role="status" className="status-error">
              {notice.text}
            </p>
          )}
        </form>
      )}
    </li>
  );
}

function IntervalLinkRow({
  interval,
  authorizations,
  month,
}: {
  interval: OvertimePreview["overtime_intervals"][number];
  authorizations: OvertimeAuthorization[];
  month: string;
}) {
  const queryClient = useQueryClient();
  const [authorizationId, setAuthorizationId] = useState(
    interval.linked_authorization_id ?? authorizations[0]?.id ?? "",
  );
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const linkMutation = useMutation({
    mutationFn: () =>
      updateInterval(interval.interval_id, {
        version: interval.version,
        overtime_authorization_id: authorizationId || null,
      }),
    onSuccess: () => {
      setNotice({ kind: "ok", text: "Authorization reference linked." });
      void queryClient.invalidateQueries({ queryKey: ["overtime-preview", month] });
    },
    onError: (error) => setNotice({ kind: "error", text: refusalText(error) }),
  });
  if (authorizations.length === 0) {
    return <p className="private-note">No authorization references yet.</p>;
  }
  return (
    // A sub-row of the interval it belongs to, not a list item of its own: an
    // <li> here would nest inside the interval's <li> (invalid HTML, and React
    // reports it as a hydration error).
    <div className="ot-link-row">
      <label>
        Authorization
        <select
          value={authorizationId}
          onChange={(event) => setAuthorizationId(event.target.value)}
          aria-label={`Authorization for interval ${visibleInstant(interval.start_at)}`}
        >
          <option value="">No reference (cannot be submitted)</option>
          {authorizations.map((authorization) => (
            <option key={authorization.id} value={authorization.id}>
              {visibleInstant(authorization.valid_from)}{" "}
              {authorization.purpose ?? authorization.source_ref ?? ""}
            </option>
          ))}
        </select>
      </label>
      <button type="button" onClick={() => linkMutation.mutate()} disabled={linkMutation.isPending}>
        Link reference
      </button>
      {notice?.kind === "ok" && (
        <p role="status" className="status-ok">
          {notice.text}
        </p>
      )}
      {notice?.kind === "error" && (
        <p role="status" className="status-error">
          {notice.text}
        </p>
      )}
    </div>
  );
}

export function OvertimePage({ month }: { month: string }) {
  const { from, to } = monthRange(month);
  const queryClient = useQueryClient();
  const [showAdd, setShowAdd] = useState(false);
  const [validFrom, setValidFrom] = useState("");
  const [validTo, setValidTo] = useState("");
  const [purpose, setPurpose] = useState("");
  const [sourceRef, setSourceRef] = useState("");
  const [notice, setNotice] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [showPreview, setShowPreview] = useState(false);

  const authQuery = useQuery({
    queryKey: ["overtime", month],
    queryFn: () => fetchOvertimeAuthorizations(from, to),
  });
  const previewQuery = useQuery({
    queryKey: ["overtime-preview", month],
    queryFn: () => fetchOvertimePreview(from, to),
    enabled: showPreview,
  });
  const addMutation = useMutation({
    mutationFn: () =>
      createOvertimeAuthorization({
        valid_from: validFrom,
        valid_to: validTo || null,
        purpose: purpose || null,
        source_ref: sourceRef || null,
      }),
    onSuccess: () => {
      setShowAdd(false);
      setValidFrom("");
      setValidTo("");
      setPurpose("");
      setSourceRef("");
      setNotice({
        kind: "ok",
        text: "Authorization reference recorded (employee-entered, not verified).",
      });
      void queryClient.invalidateQueries({ queryKey: ["overtime", month] });
      // An authorization reference is one of the facts the OPEN preview is
      // computed over — it decides which overtime is covered. Refreshing only
      // the list left the visible preview stating that the reference does not
      // exist (review R6).
      void queryClient.invalidateQueries({ queryKey: ["overtime-preview", month] });
    },
    onError: (error) => setNotice({ kind: "error", text: refusalText(error) }),
  });

  const monthTitle = new Intl.DateTimeFormat("en", {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(`${month}-01T00:00:00Z`));
  const preview: OvertimePreview | undefined = previewQuery.data;
  if (authQuery.isPending) return <p role="status">Loading your private overtime view…</p>;
  if (authQuery.isError)
    return (
      <p className="status-error">
        Could not load your overtime view. Your saved facts are unchanged.
      </p>
    );
  const authorizations = Array.isArray(authQuery.data) ? authQuery.data : [];
  const eligibleIntervals = preview?.overtime_intervals ?? [];
  const blockedFindings =
    preview?.findings.filter((finding) => finding.status === "BLOCKED_POLICY") ?? [];

  return (
    <section className="workspace" aria-label="Overtime entries and elections">
      <div className="workspace-heading">
        <div>
          <p className="eyebrow">Private — visible only to you</p>
          <h1>Overtime — {monthTitle}</h1>
        </div>
      </div>
      <MonthContextNav month={month} current="overtime" />
      <p className="private-note">
        Record overtime as <em>Overtime paid</em> or <em>Overtime time compensation</em> intervals
        in the day editor, then link an authorization reference here. The entry is a request;
        approval is an institutional decision taken outside Chronobeam and appears here read-only.
      </p>

      <section className="shift-card">
        <div className="day-heading">
          <div>
            <h2>Overtime authorizations (references)</h2>
            <p className="private-note">
              Your requests, plus the institutional decisions recorded on them.
            </p>
          </div>
        </div>
        {authorizations.length === 0 && !showAdd && (
          <p className="private-note">No authorization references recorded this month.</p>
        )}
        <ul className="ocd-calls">
          {authorizations.map((authorization) => (
            <AuthorizationRow key={authorization.id} authorization={authorization} month={month} />
          ))}
        </ul>
        {!showAdd && (
          <button type="button" className="add-interval" onClick={() => setShowAdd(true)}>
            Add authorization reference
          </button>
        )}
        {showAdd && (
          <form
            className="ocd-call-form"
            onSubmit={(event) => {
              event.preventDefault();
              addMutation.mutate();
            }}
          >
            <label>
              Valid from
              <DateTimeField
                aria-label="Authorization valid from"
                value={validFrom}
                onChange={setValidFrom}
                required
              />
            </label>
            <label>
              Valid to
              <DateTimeField
                aria-label="Authorization valid to"
                value={validTo}
                onChange={setValidTo}
              />
            </label>
            <label>
              Purpose
              <input
                value={purpose}
                onChange={(event) => setPurpose(event.target.value)}
                maxLength={120}
              />
            </label>
            <label>
              Source / reference
              <input
                value={sourceRef}
                onChange={(event) => setSourceRef(event.target.value)}
                maxLength={120}
              />
            </label>
            <button type="submit" disabled={addMutation.isPending}>
              Save reference
            </button>
            <button type="button" onClick={() => setShowAdd(false)}>
              Cancel
            </button>
          </form>
        )}
        {notice?.kind === "ok" && (
          <p role="status" className="status-ok">
            {notice.text}
          </p>
        )}
        {notice?.kind === "error" && (
          <p role="status" className="status-error">
            {notice.text}
          </p>
        )}
      </section>

      <section className="shift-card">
        <div className="day-heading">
          <div>
            <h2>Overtime classification preview</h2>
            <p className="private-note">
              Private derived classification — no amounts are calculated.
            </p>
          </div>
        </div>
        <div className="workspace-actions">
          <button type="button" onClick={() => setShowPreview((value) => !value)}>
            Show / refresh preview
          </button>
        </div>
        {previewQuery.isError && (
          <p role="status" className="status-error">
            {refusalText(previewQuery.error)}
          </p>
        )}
        {preview && (
          <aside className="preview" aria-label="Private overtime preview">
            <strong>Overtime preview — not shared</strong>
            <p>
              Only your OVERTIME intervals, their authorization references, classified components,
              and election are included. Ordinary intervals are excluded.
            </p>
            {eligibleIntervals.length === 0 && (
              <p className="private-note">No overtime intervals in this month.</p>
            )}
            {eligibleIntervals.length > 0 && (
              <ul>
                {eligibleIntervals.map((interval) => (
                  <li key={interval.interval_id}>
                    {visibleInstant(interval.start_at)} → {visibleInstant(interval.end_at)} ·{" "}
                    {plainMinutes(Math.round(interval.elapsed_seconds / 60))} ·{" "}
                    {workTypeLabel(interval.work_type)}
                    {interval.linked_authorization_id
                      ? " · authorization reference linked"
                      : " · no authorization reference"}
                    {!interval.linked_authorization_id && (
                      <IntervalLinkRow
                        interval={interval}
                        authorizations={authorizations}
                        month={month}
                      />
                    )}
                  </li>
                ))}
              </ul>
            )}
            {preview.classified_components.length > 0 && (
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
            )}
            <p className="private-note">
              Election:{" "}
              {preview.election.mode
                ? (electionLabels[preview.election.mode] ?? preview.election.mode)
                : "not chosen"}
            </p>
            {blockedFindings.map((finding) => (
              <p
                key={`${finding.rule_id}-${finding.message}`}
                className={`severity-${finding.status.toLowerCase()}`}
              >
                <strong>{accessRuleLabel(finding.rule_id)}</strong> {finding.message}
              </p>
            ))}
          </aside>
        )}
      </section>
    </section>
  );
}
