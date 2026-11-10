import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";

import {
  createHrAccessRequest,
  declineAccessRequest,
  fetchDisclosure,
  fetchHrAccessRequests,
  fetchIdentity,
  fetchMyAccessRequests,
  grantAccessRequest,
  type DisclosureView,
} from "../lib/api";
import { MonthField, isoToGermanDate, toIsoMonth } from "../lib/inputs";
import { berlinInstant, monthTitle } from "../lib/format";
import { workTypeLabel } from "../lib/labels";
import { refusalText } from "../lib/notice";

/** Human names for an access-request purpose (never the raw enum). */
const purposeLabels: Record<string, string> = {
  RANDOM_CHECK: "Random check",
  WORK_TIME_RECORD: "Working-time record",
};

/** Human names for an access request's state (never the raw enum). "Opened"
 *  alone cannot tell an exhausted reader whether the employee granted access or
 *  HR has already looked at it. */
const statusLabels: Record<string, string> = {
  PENDING: "Waiting for the employee",
  GRANTED: "Opened by the employee",
  DECLINED: "Declined by the employee",
};

/** Signed hh:mm from minutes (frozen-view rows are durations). */
function hhmm(minutes: number): string {
  const sign = minutes < 0 ? "−" : "";
  const absolute = Math.abs(minutes);
  return `${sign}${Math.floor(absolute / 60)}:${String(absolute % 60).padStart(2, "0")}`;
}

function FrozenSnapshot({ view }: { view: DisclosureView }) {
  const snapshot = view.snapshot as {
    employee_identifier?: string;
    intervals?: Array<{
      start_at: string;
      end_at: string;
      work_type: string;
      elapsed_seconds: number;
    }>;
    daily?: Array<{
      date: string;
      worked_minutes: number;
      required_minutes: number;
      delta_minutes: number;
    }>;
  };
  return (
    <section className="frozen-view">
      <h3>Shared view</h3>
      <p>
        Employee reference: {String(snapshot.employee_identifier ?? "—")} ·{" "}
        {isoToGermanDate(view.period_start)}–{isoToGermanDate(view.period_end)} · read-only
      </p>
      {snapshot.daily && snapshot.daily.length > 0 && (
        <table className="frozen-daily">
          <thead>
            <tr>
              <th>Day</th>
              <th>Worked</th>
              <th>Required</th>
              <th>Delta</th>
            </tr>
          </thead>
          <tbody>
            {snapshot.daily.map((row) => (
              <tr key={row.date}>
                <td>{isoToGermanDate(row.date)}</td>
                <td>{hhmm(row.worked_minutes)}</td>
                <td>{hhmm(row.required_minutes)}</td>
                <td>{hhmm(row.delta_minutes)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {snapshot.intervals && snapshot.intervals.length > 0 && (
        <ul className="frozen-intervals">
          {snapshot.intervals.map((interval, index) => (
            <li key={`${interval.start_at}-${index}`}>
              {berlinInstant(interval.start_at)} → {berlinInstant(interval.end_at)} ·{" "}
              {hhmm(Math.round(interval.elapsed_seconds / 60))} ·{" "}
              {workTypeLabel(interval.work_type)}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function EmployeeRequests() {
  const queryClient = useQueryClient();
  const requests = useQuery({ queryKey: ["my-access-requests"], queryFn: fetchMyAccessRequests });
  const [notice, setNotice] = useState<string | null>(null);
  const grant = useMutation({
    mutationFn: grantAccessRequest,
    onSuccess: () => {
      setNotice("Requested scope opened to the named recipient — nothing else is visible to them.");
      void queryClient.invalidateQueries({ queryKey: ["my-access-requests"] });
    },
    onError: (error) => setNotice(refusalText(error)),
  });
  const decline = useMutation({
    mutationFn: declineAccessRequest,
    onSuccess: () => {
      setNotice("Request declined — no data was opened.");
      void queryClient.invalidateQueries({ queryKey: ["my-access-requests"] });
    },
    onError: (error) => setNotice(refusalText(error)),
  });
  if (requests.isPending)
    return (
      <section className="standalone-card">
        <p role="status">Loading access requests…</p>
      </section>
    );
  if (requests.isError)
    return (
      <section className="standalone-card">
        <p className="status-error">Could not load employee access requests.</p>
      </section>
    );
  return (
    <section className="standalone-card">
      <h2>Requests for you</h2>
      {requests.data.length === 0 ? (
        <p className="empty-state">
          No access requests. Nothing about your working time is visible to HR or supervisors unless
          you open a scope.
        </p>
      ) : (
        <ul className="package-list">
          {requests.data.map((request) => (
            <li key={request.id}>
              <strong>{purposeLabels[request.purpose] ?? request.purpose}</strong> ·{" "}
              {isoToGermanDate(request.period_start)}–{isoToGermanDate(request.period_end)}
              <br />
              Requested by: {request.requester_principal}
              {request.message && (
                <>
                  <br />
                  Message: {request.message}
                </>
              )}
              <br />
              {/* Every row states its outcome. Without this the employee could
               *  not tell whether the scope they opened or declined had been
               *  recorded — the row looked unanswered, and the review link read
               *  as an instruction to act on a request that was already
               *  resolved. The link stays only where it is actionable. */}
              <strong>{statusLabels[request.status] ?? request.status}</strong>
              {request.status === "PENDING" && (
                <>
                  {" "}
                  <Link to="/month/$month" params={{ month: request.period_start.slice(0, 7) }}>
                    Review {monthTitle(request.period_start.slice(0, 7))} first
                  </Link>
                </>
              )}
              {request.status === "PENDING" && (
                <div className="workspace-actions">
                  <button
                    type="button"
                    className="primary"
                    onClick={() => grant.mutate(request.id)}
                  >
                    Open exactly this scope to {request.requester_principal}
                  </button>
                  <button type="button" onClick={() => decline.mutate(request.id)}>
                    Decline
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
      {notice && (
        <p role="status" className="status-ok">
          {notice}
        </p>
      )}
    </section>
  );
}

function HrRequests() {
  const queryClient = useQueryClient();
  const [employeeId, setEmployeeId] = useState("");
  const [month, setMonth] = useState("");
  const [message, setMessage] = useState("");
  const [view, setView] = useState<DisclosureView | null>(null);
  const requests = useQuery({ queryKey: ["hr-access-requests"], queryFn: fetchHrAccessRequests });
  const create = useMutation({
    mutationFn: () =>
      createHrAccessRequest({
        employee_external_id: employeeId,
        purpose: "RANDOM_CHECK",
        month,
        message: message || undefined,
      }),
    onSuccess: () => {
      setEmployeeId("");
      setMessage("");
      void queryClient.invalidateQueries({ queryKey: ["hr-access-requests"] });
    },
  });
  const read = useMutation({ mutationFn: (id: string) => fetchDisclosure(id), onSuccess: setView });
  // The primary states its own requirement instead of being inert: an empty
  // employee ID or an incomplete month was previously blocked by native
  // validation only, which fires no request and shows no message at all
  // (acceptance E3). `toIsoMonth` is the field's own parser, so the app and the
  // field agree on what "complete" means.
  const employeeMissing = employeeId.trim() === "";
  const monthMissing = toIsoMonth(month) === null;
  const requirement = employeeMissing
    ? "Enter the employee's external ID."
    : monthMissing
      ? "Enter the month as MM.YYYY — for example 10.2026."
      : null;
  const valid = requirement === null;
  return (
    <section className="hr-request-panel">
      <h2>HR request creation</h2>
      <p>Creating a request reveals no time data — the employee decides what to open.</p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (valid) create.mutate();
        }}
      >
        <label>
          Employee ID
          <input
            value={employeeId}
            onChange={(event) => setEmployeeId(event.target.value)}
            required
            aria-invalid={employeeMissing}
          />
        </label>
        <label>
          Month
          <MonthField value={month} onChange={setMonth} required aria-invalid={monthMissing} />
        </label>
        <label>
          Message
          <input
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            maxLength={500}
          />
        </label>
        <button
          type="submit"
          className="primary"
          disabled={create.isPending || !valid}
          aria-describedby={valid ? undefined : "hr-request-requirement"}
        >
          Request random check
        </button>
        {requirement && (
          <p className="field-requirement" id="hr-request-requirement" role="status">
            {requirement}
          </p>
        )}
      </form>
      {create.isError && (
        <p role="status" className="status-error">
          {refusalText(create.error)}
        </p>
      )}
      <h3>Your requests</h3>
      {requests.isPending ? (
        <p role="status">Loading your requests…</p>
      ) : requests.isError ? (
        <p className="status-error">Could not load your requests.</p>
      ) : requests.data.length === 0 ? (
        <p className="empty-state">
          No requests yet. After the employee opens a scope, the granted view appears here.
        </p>
      ) : (
        <ul className="package-list">
          {requests.data.map((request) => (
            <li key={request.id}>
              {purposeLabels[request.purpose] ?? request.purpose} ·{" "}
              {isoToGermanDate(request.period_start)}–{isoToGermanDate(request.period_end)} ·{" "}
              {statusLabels[request.status] ?? request.status}
              {request.grant_id && (
                <div className="workspace-actions">
                  <button
                    type="button"
                    onClick={() => read.mutate(request.grant_id as string)}
                    disabled={read.isPending}
                  >
                    Open the granted view
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
      {read.isError && (
        <p role="status" className="status-error">
          {refusalText(read.error)}
        </p>
      )}
      {view && <FrozenSnapshot view={view} />}
    </section>
  );
}

export function AccessRequestsPage() {
  const identity = useQuery({ queryKey: ["identity"], queryFn: fetchIdentity });
  if (identity.isPending)
    return (
      <section className="access-requests-page">
        <p role="status">Loading access requests…</p>
      </section>
    );
  if (identity.isError)
    return (
      <section className="access-requests-page">
        <p className="status-error">Could not identify this account.</p>
      </section>
    );
  const capabilities = new Set(identity.data.capabilities);
  const canCreateRequests = capabilities.has("ACCESS_REQUEST_CREATE");
  const canGrantRequests = capabilities.has("OWN_DISCLOSURE_GRANT");
  return (
    <section className="access-requests-page">
      <p className="eyebrow">Scoped access only</p>
      <h1>Access Requests</h1>
      {canCreateRequests ? (
        <HrRequests />
      ) : canGrantRequests ? (
        <EmployeeRequests />
      ) : (
        <p className="status-error">This account cannot create or respond to access requests.</p>
      )}
    </section>
  );
}
