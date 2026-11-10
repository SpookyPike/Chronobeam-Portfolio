import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import {
  fetchIdentity,
  fetchRecipientSubmission,
  fetchRecipientSubmissions,
  type SubmissionPurpose,
} from "../lib/api";
import { submissionStatusLabels } from "../lib/labels";
import { isoToGermanDate } from "../lib/inputs";
import { HumanPreview } from "./submissions";

const titles: Record<SubmissionPurpose, string> = {
  SHIFT_REPORT: "Shift reports",
  OCD_COMPENSATION: "OCD compensation",
  OVERTIME_PAID: "Overtime — pay",
  OVERTIME_TIME_COMP: "Overtime — time compensation",
};

export function RecipientQueuePage() {
  const identity = useQuery({ queryKey: ["identity"], queryFn: fetchIdentity });
  const canReadPackages = identity.data?.capabilities.includes("SUBMISSION_READ_ASSIGNED") ?? false;
  const queue = useQuery({
    queryKey: ["recipient-submissions"],
    queryFn: fetchRecipientSubmissions,
    enabled: canReadPackages,
  });
  const [active, setActive] = useState<SubmissionPurpose | "ALL">("ALL");
  const [selected, setSelected] = useState<string | null>(null);
  const detail = useQuery({
    queryKey: ["recipient-submission", selected],
    queryFn: () => fetchRecipientSubmission(selected ?? ""),
    enabled: canReadPackages && selected !== null,
  });
  const visible = (queue.data ?? []).filter((item) => active === "ALL" || item.purpose === active);
  // A filter that excludes the open package must take its detail with it:
  // leaving it on screen pairs a shift report with the "Overtime — pay" tab.
  const selectedIsVisible = visible.some((item) => item.id === selected);
  if (identity.isPending) return <p role="status">Checking package access…</p>;
  if (identity.isError || !canReadPackages)
    return (
      <section className="recipient-queue">
        <h1>Submission queue</h1>
        <p className="status-error">This account is not assigned to read submission packages.</p>
      </section>
    );
  return (
    <section className="recipient-queue">
      <p className="eyebrow">Purpose-scoped recipient queue</p>
      <h1>Submission queue</h1>
      <p>This queue contains packages assigned to you, not an employee ledger.</p>
      <div className="workspace-actions">
        <button type="button" aria-pressed={active === "ALL"} onClick={() => setActive("ALL")}>
          All
        </button>
        {(Object.keys(titles) as SubmissionPurpose[]).map((purpose) => (
          <button
            key={purpose}
            type="button"
            aria-pressed={active === purpose}
            onClick={() => setActive(purpose)}
          >
            {titles[purpose]}
          </button>
        ))}
        {/* UI-020: the filter row says how many packages it is filtering. */}
        {queue.data ? (
          <span className="status-muted">
            {visible.length} {visible.length === 1 ? "package" : "packages"}
          </span>
        ) : null}
      </div>
      {queue.isPending ? (
        <p role="status">Loading assigned packages…</p>
      ) : queue.isError ? (
        <p className="status-error">Could not load assigned packages.</p>
      ) : visible.length === 0 ? (
        <p className="empty-state">
          No packages assigned to you
          {queue.data && queue.data.length > 0 ? " for this filter" : " yet"}.
        </p>
      ) : (
        <ul className="package-list queue-list">
          {/* UI-020: purpose, period, reference, status and action sit in aligned
           * columns instead of one prose line, so a queue of several packages can
           * be scanned down a column rather than read across each row. */}
          {visible.map((item) => (
            <li key={item.id} className="queue-row">
              <strong className="queue-purpose">{titles[item.purpose]}</strong>
              <span className="queue-period">
                {item.period_start === item.period_end
                  ? isoToGermanDate(item.period_start)
                  : `${isoToGermanDate(item.period_start)}–${isoToGermanDate(item.period_end)}`}
              </span>
              <span className="queue-reference">
                Employee reference: {item.employee_identifier}
              </span>
              <span className="queue-status">
                {submissionStatusLabels[item.status] ?? item.status} · Version {item.version}
              </span>
              <button type="button" onClick={() => setSelected(item.id)}>
                Open package
              </button>
            </li>
          ))}
        </ul>
      )}
      {detail.isPending && selectedIsVisible && <p role="status">Opening the package…</p>}
      {detail.data?.payload && selectedIsVisible && (
        <section className="recipient-detail">
          <h2>What you will receive</h2>
          <HumanPreview
            purpose={detail.data.purpose}
            preview={detail.data.payload}
            audience="recipient"
          />
        </section>
      )}
    </section>
  );
}
