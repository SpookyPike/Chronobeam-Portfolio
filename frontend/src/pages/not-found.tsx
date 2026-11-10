import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { fetchIdentity } from "../lib/api";
import { currentBerlinMonth } from "./month";

/** In-shell 404 (F28): the standard eyebrow + card shell, with a
 *  capability-appropriate way back. TanStack Router renders this for unmatched
 *  routes.
 *
 *  `Back to Home` is the navy primary because it is the one action every reader
 *  can take; the month workspace is the secondary for employees who landed here
 *  mid-task. The heading states the fact rather than repeating the eyebrow. */
export function NotFoundPage() {
  const identity = useQuery({ queryKey: ["identity"], queryFn: fetchIdentity });
  const capabilities = new Set(identity.data?.capabilities ?? []);
  const isEmployee = capabilities.has("OWN_LEDGER_READ");
  const canAdminister = capabilities.has("ACCESS_ADMIN") || capabilities.has("SYSTEM_STATUS_READ");
  const currentMonth = currentBerlinMonth();
  return (
    <section className="workspace" aria-label="Page not found">
      <p className="eyebrow">Page not found</p>
      <div className="standalone-card">
        <h1>This address does not exist</h1>
        <p>It may have moved, or been mistyped.</p>
        <div className="workspace-actions">
          <Link to="/" className="primary-link">
            Back to Home
          </Link>
          {isEmployee && (
            <Link to="/month/$month" params={{ month: currentMonth }}>
              Open this month’s workspace
            </Link>
          )}
          {canAdminister && <Link to="/admin">Administration</Link>}
        </div>
      </div>
    </section>
  );
}
