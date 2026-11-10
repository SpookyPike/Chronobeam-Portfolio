import { useQuery } from "@tanstack/react-query";

import { fetchAdminSystem, type AdminCalendarEdition } from "../lib/api";
import { isoDate, monthTitle } from "../lib/format";

const AUTH_MODE_LABELS: Record<string, string> = {
  DEVELOPMENT_HEADER: "Development identity header (dev/test only)",
  PRODUCTION_OIDC: "Institutional OIDC / SSO",
  PRODUCTION_NOT_CONFIGURED: "Production OIDC not configured yet",
};

const INTEGRATION_LABELS: Record<string, string> = {
  shift_plan: "Shift plan",
  hr_absence: "External HR absence system",
  travel_feed: "External travel feed",
  recipient_registry: "Submission recipients",
};

/** Human statuses for the operator codes; an unknown code stays raw rather
 *  than being silently prettified. */
const INTEGRATION_STATUS_TEXT: Record<string, string> = {
  SEED_SEAM: "test seam",
  BLOCKED_INTEGRATION: "no live connection",
  CONFIGURED: "configured",
  EMPTY_FAIL_CLOSED: "none configured — sharing refused",
  INCONSISTENT: "inconsistent — submissions refused",
  LOADED: "published",
  NOT_LOADED: "not published",
};

const INTEGRATION_NOTES: Record<string, string> = {
  SEED_SEAM: "Controlled test seam — no live adapter",
  BLOCKED_INTEGRATION: "No live connection — facts are employee-declared",
  CONFIGURED: "Every configured subject currently holds the reading role",
  EMPTY_FAIL_CLOSED: "No recipient configured — fail-closed, nothing can be shared",
  INCONSISTENT: "A configured subject holds no reading role — packages to it are refused",
  // States what the code actually means — "an edition exists". It deliberately does
  // NOT claim coverage: the code cannot support that, and a reassurance the data
  // contradicts (a "covers the pilot range" line 40px above a coverage gap) teaches
  // the operator to distrust the card. Coverage is reported by the verdict below it.
  LOADED: "A calendar edition is published",
  NOT_LOADED: "No calendar edition published yet — publish one before recording future-dated facts",
};

/** Codes that mean "an operator has to look at this". */
const INTEGRATION_ATTENTION = new Set([
  "SEED_SEAM",
  "BLOCKED_INTEGRATION",
  "EMPTY_FAIL_CLOSED",
  "INCONSISTENT",
  "NOT_LOADED",
]);

/** The tone an operator code carries. One rule, used by the Integrations list and
 *  by the Calendar card, so a code cannot read as healthy in one place and as a
 *  problem in the other. Error-red stays reserved for a broken service; a loaded
 *  edition is never an error, and an attention code is quiet rather than alarming. */
function integrationTone(status: string): string {
  return INTEGRATION_ATTENTION.has(status) || status.startsWith("BLOCKED")
    ? "status-muted"
    : "status-ok";
}

/** The `calendar` code is rendered by the Calendar card below, which answers the
 *  whole question. Filtering it out of the Integrations list is what keeps the
 *  status from appearing twice with different amounts of context. */
const CALENDAR_CODE = "calendar";

/** "01 Jan 2026 → 31 Dec 2026", or "open-ended" when the edition declares no end.
 *  `valid_to` is inclusive, which is what the domain means by it. */
function editionRange(edition: AdminCalendarEdition): string {
  const end = edition.valid_to ? isoDate(edition.valid_to) : "open-ended";
  return `${isoDate(edition.valid_from)} → ${end}`;
}

/** Human statuses lead; exact codes/revisions stay behind
 *  "Technical details". No fake metrics, no secrets, no employee data
 *  (server remains authoritative). */
export function AdminSystemPage() {
  const system = useQuery({ queryKey: ["admin-system"], queryFn: fetchAdminSystem });

  if (system.isPending) return <p role="status">Loading system status…</p>;
  if (system.isError) return <p className="status-error">Could not load system status.</p>;
  const data = system.data;
  const authCurrent = data.auth.mode === "PRODUCTION_OIDC";
  const calendarStatus = data.integrations[CALENDAR_CODE] ?? "NOT_LOADED";
  const { editions, first_uncovered_month: uncoveredMonth } = data.calendar;
  const integrations = Object.entries(data.integrations).filter(([key]) => key !== CALENDAR_CODE);
  return (
    <div className="admin-system">
      {/* The calendar is institutional data an operator PUBLISHES, not an external
       *  feed, and the question it has to answer — "what is covered, and when does
       *  coverage run out?" — does not fit a one-word code. Everything it needs is
       *  already in the status card: no second request, no second page.
       *
       *  FIRST card, not fifth: it is the only card on this tab that can carry an
       *  operator action. The four below it are always-green service checks, so
       *  ordering by "what needs doing" puts the coverage verdict above the fold
       *  instead of two screens down. */}
      <section className="admin-system-card">
        <h2>Calendar</h2>
        <p className={integrationTone(calendarStatus)}>
          {INTEGRATION_STATUS_TEXT[calendarStatus] ?? calendarStatus}
        </p>
        <p className="private-note">{INTEGRATION_NOTES[calendarStatus] ?? ""}</p>
        {/* The verdict comes BEFORE the evidence: an operator opens this card to ask
         *  "is anything wrong?", and the table below is the record that explains the
         *  answer. It is a live region so the answer is announced when it arrives
         *  rather than only discovered by navigating — the loading placeholder it
         *  replaces is unmounted with the data. */}
        {uncoveredMonth ? (
          <>
            <p className="status-warning coverage-gap" role="status">
              Coverage gap: {monthTitle(uncoveredMonth)} is the first month with no covering
              edition.
            </p>
            {/* Stated as the operator experiences it, not as an HTTP status: the exact
             *  code stays behind "Technical details", the way every other engine code
             *  on this page does. */}
            <p className="private-note">
              Month views, OCD previews, travel summaries and exports need a covering edition —
              those months stay blocked until one is published (
              <code>python -m app.calendar_admin publish</code>).
            </p>
          </>
        ) : (
          <p className="status-ok" role="status">
            No coverage gap from this month onward.
          </p>
        )}
        {editions.length > 0 ? (
          <div className="admin-edition-list-wrap">
            <table className="admin-edition-list">
              <caption>Published editions</caption>
              <thead>
                <tr>
                  <th scope="col">Edition</th>
                  <th scope="col">Effective range</th>
                  <th scope="col">Source</th>
                </tr>
              </thead>
              <tbody>
                {/* One row per published edition, in the order they took effect.
                 *  Overlaps are NOT flagged: a bounded corrective overlay is
                 *  legitimate published data, and resolution between editions is the
                 *  server's rule (greatest effective-from wins), not a defect for
                 *  this table to point at. */}
                {editions.map((edition) => (
                  <tr key={`${edition.version}-${edition.valid_from}`}>
                    <td className="admin-edition-version">{edition.version}</td>
                    <td className="admin-edition-range">{editionRange(edition)}</td>
                    <td className="admin-edition-source">{edition.source}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="status-muted">No edition has been published.</p>
        )}
        <details className="tech-details">
          <summary aria-label="Technical details — Calendar">Technical details</summary>
          <dl className="admin-detail-list">
            <div>
              <dt>Integration code</dt>
              <dd>{calendarStatus}</dd>
            </div>
            <div>
              <dt>First uncovered month</dt>
              <dd>{uncoveredMonth ?? "none"}</dd>
            </div>
          </dl>
        </details>
      </section>
      <section className="admin-system-card">
        <h2>Application</h2>
        <p className="status-ok">Application available</p>
        <details className="tech-details">
          <summary aria-label="Technical details — Application">Technical details</summary>
          <p>
            {data.application.name} {data.application.version}
          </p>
          <p>Build SHA: {data.application.build_sha || "local/unset"}</p>
        </details>
      </section>
      <section className="admin-system-card">
        <h2>Database</h2>
        {data.database.reachable ? (
          <p className="status-ok">Database connected</p>
        ) : (
          <p className="status-error">Database unreachable</p>
        )}
        {!data.database.schema_current && (
          <p className="private-note">Schema is out of date — contact the service owner.</p>
        )}
        <details className="tech-details">
          <summary aria-label="Technical details — Database">Technical details</summary>
          <dl className="admin-detail-list">
            <div>
              <dt>Schema revision</dt>
              <dd>{data.database.schema_revision ?? "unknown"}</dd>
            </div>
            <div>
              <dt>Expected revision</dt>
              <dd>{data.database.expected_revision ?? "unknown"}</dd>
            </div>
          </dl>
        </details>
      </section>
      <section className="admin-system-card">
        <h2>Authentication</h2>
        {authCurrent ? (
          <p className="status-ok">Institutional OIDC / SSO active</p>
        ) : data.auth.mode === "DEVELOPMENT_HEADER" ? (
          <p className="status-muted">Development identity mode (test only)</p>
        ) : (
          <p className="status-muted">Production SSO not configured yet</p>
        )}
        <details className="tech-details">
          <summary aria-label="Technical details — Authentication">Technical details</summary>
          <p>{AUTH_MODE_LABELS[data.auth.mode] ?? data.auth.mode}</p>
        </details>
      </section>
      <section className="admin-system-card">
        <h2>Integrations</h2>
        <ul className="admin-integration-list">
          {integrations.map(([key, status]) => (
            <li key={key}>
              <strong>{INTEGRATION_LABELS[key] ?? key}</strong>
              <span className={integrationTone(status)}>
                {INTEGRATION_STATUS_TEXT[status] ?? status}
              </span>
              <span className="status-muted">{INTEGRATION_NOTES[status] ?? ""}</span>
            </li>
          ))}
        </ul>
        <details className="tech-details">
          <summary aria-label="Technical details — Integrations">Technical details</summary>
          <ul className="admin-integration-list">
            {integrations.map(([key, status]) => (
              <li key={key}>
                <span className="status-muted">
                  {key}: {status}
                </span>
              </li>
            ))}
          </ul>
        </details>
      </section>
    </div>
  );
}
