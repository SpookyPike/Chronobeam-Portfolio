import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { fetchIdentity, fetchMonth, fetchOnboarding, firstErrorCode } from "../lib/api";
import { coverageBlockedMessage } from "../lib/labels";
import { currentBerlinMonth, minutes } from "./month";

/** Role-aware home: a truthful start page per work area —
 *  never phase language, never backend-service diagnostics. */

/** Calendar month (YYYY-MM) offset by N months — TZ-safe. */
function adjacentMonth(month: string, offset: number): string {
  const [year, monthNumber] = month.split("-").map(Number);
  const next = new Date(Date.UTC(year, monthNumber - 1 + offset, 1));
  return `${next.getUTCFullYear()}-${String(next.getUTCMonth() + 1).padStart(2, "0")}`;
}

function monthTitle(month: string): string {
  return new Intl.DateTimeFormat("en", { month: "long", year: "numeric", timeZone: "UTC" }).format(
    new Date(`${month}-01T00:00:00Z`),
  );
}

export function DashboardPage() {
  const identity = useQuery({ queryKey: ["identity"], queryFn: fetchIdentity });
  const capabilities = new Set(identity.data?.capabilities ?? []);
  const isEmployee = capabilities.has("OWN_LEDGER_READ");
  const canCreateRequests = capabilities.has("ACCESS_REQUEST_CREATE");
  const canReadPackages = capabilities.has("SUBMISSION_READ_ASSIGNED");
  const canAdminister = capabilities.has("ACCESS_ADMIN") || capabilities.has("SYSTEM_STATUS_READ");
  const currentMonth = currentBerlinMonth();
  // Walk back (bounded) to the latest month with recorded facts: an empty
  // current month projects a full month of required time as debt, which
  // contradicts the carried-forward balance the month page shows.
  const monthBalance = useQuery({
    // Own cache namespace: the month workspace (and review page) cache the
    // FULL month payload under ["month", m]; this dashboard query resolves
    // to a lean {month, summary} projection and must never collide with it,
    // or the workspace would render an incompatible shape and crash.
    queryKey: ["home-month-summary", currentMonth],
    queryFn: async () => {
      let month = currentMonth;
      for (let lookback = 0; lookback < 12; lookback += 1) {
        try {
          const summary = await fetchMonth(month);
          if (summary.interval_count > 0) return { month, summary, boundary: null };
          month = adjacentMonth(month, -1);
        } catch (error) {
          // A coverage boundary is a PERMANENT fact about the data, not a
          // transient fault (acceptance F5). The ledger starts at the cutover
          // month and earlier months stay in the Excel archive by design
          // (INV-13), so walking into one must STOP the walk rather than fail
          // the card with "unavailable right now" — which was a lie, and would
          // have invited the employee to retry something that cannot change.
          const code = firstErrorCode([{ error }]);
          if (coverageBlockedMessage(code, monthTitle(month))) {
            return { month, summary: null, boundary: code };
          }
          throw error;
        }
      }
      return { month: currentMonth, summary: await fetchMonth(currentMonth), boundary: null };
    },
    enabled: isEmployee,
    retry: 1,
  });
  // The same cached key the Profile page uses, so the truthful sentence below
  // costs nothing extra when both pages are visited.
  const onboarding = useQuery({
    queryKey: ["onboarding"],
    queryFn: () => fetchOnboarding(),
    enabled: isEmployee,
    retry: 1,
  });

  if (!identity.data) {
    return (
      <section aria-label="Home">
        <h1>Chronobeam</h1>
        <p role="status">Checking your account…</p>
      </section>
    );
  }

  if (isEmployee) {
    return (
      <section aria-label="Home">
        <p className="eyebrow">Private — visible only to you</p>
        <h1>Welcome back, {identity.data.display_name.split(" ")[0]}</h1>
        <p className="private-note">
          Nothing is shared with HR unless you explicitly open a defined scope.
        </p>
        <div className="workspace-grid dashboard-grid">
          <section className="card dashboard-balance-card">
            <h2>{monthTitle(currentMonth)}</h2>
            {monthBalance.isPending && (
              <p role="status" className="balance-placeholder">
                Loading your balance…
              </p>
            )}
            {monthBalance.isError && (
              <p className="status-muted">
                {coverageBlockedMessage(firstErrorCode([monthBalance]), monthTitle(currentMonth)) ??
                  "Balance unavailable right now."}
              </p>
            )}
            {monthBalance.isSuccess &&
              (() => {
                const { month, summary, boundary } = monthBalance.data;
                if (summary === null) {
                  // The walk stopped at a coverage boundary. Say which one, and —
                  // when the start month is known — say the truthful thing rather
                  // than leaving the employee to guess why the balance is absent.
                  const startMonth = onboarding.data?.opening?.cutover_month;
                  return (
                    <>
                      <p className="status-muted">
                        {coverageBlockedMessage(boundary, monthTitle(month))}
                      </p>
                      {startMonth && (
                        <p className="private-note">
                          Your ledger starts in {monthTitle(startMonth)}.
                        </p>
                      )}
                    </>
                  );
                }
                return (
                  <>
                    {/* UI-024: name the month the number belongs to in BOTH
                        branches. "Balance at month end" made the reader connect
                        the figure to the heading themselves — the exact ambiguity
                        the review flagged. */}
                    {summary.interval_count > 0 ? (
                      <p className="balance">
                        Balance at the end of {monthTitle(month)}:{" "}
                        {minutes(summary.closing_balance_minutes)}
                        {month === currentMonth ? "" : " · nothing recorded since"}
                      </p>
                    ) : (
                      <p className="balance">
                        Nothing recorded yet — your balance appears once facts exist.
                      </p>
                    )}
                    {/* UI-024: a minus balance was the app's largest emotional
                        spike, and the only thing explaining it lived inside a
                        single day cell. Say what the number means, right next to
                        the number. */}
                    {summary.closing_balance_minutes < 0 && (
                      <p className="private-note">
                        A minus balance means less time recorded than required.
                      </p>
                    )}
                    <p className="private-note">
                      {month !== currentMonth
                        ? "Nothing recorded in this month yet — your balance carries forward from earlier months."
                        : "Derived from your recorded facts."}
                    </p>
                  </>
                );
              })()}
            <div className="workspace-actions">
              <Link to="/month/$month" params={{ month: currentMonth }} className="primary-link">
                Open this month’s workspace
              </Link>
              <Link to="/month/$month/review" params={{ month: currentMonth }}>
                Review the month
              </Link>
            </div>
          </section>
          {/* UI-024: the four-link "Your private areas" row that duplicated the
              header navigation was removed here and its links were not re-created
              elsewhere. Do not add an explanatory card in its place. */}
        </div>
      </section>
    );
  }

  if (canReadPackages || canCreateRequests || canAdminister) {
    return (
      <section aria-label="Home">
        <h1>Welcome, {identity.data.display_name}</h1>
        <div className="workspace-actions">
          {canReadPackages && <Link to="/recipient-queue">Open submission queue</Link>}
          {canCreateRequests && <Link to="/access-requests">Access Requests</Link>}
          {canAdminister && <Link to="/admin">Administration</Link>}
        </div>
      </section>
    );
  }

  return (
    <section aria-label="Home">
      <h1>Welcome, {identity.data.display_name}</h1>
      <p className="private-note">
        No work area is assigned to this account. Contact your administrator if you expect access.
      </p>
    </section>
  );
}
