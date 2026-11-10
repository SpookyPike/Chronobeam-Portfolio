import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";

import { fetchFindings, fetchMonth, firstErrorCode, reviewMonth } from "../lib/api";
import { coverageBlockedMessage } from "../lib/labels";
import { invalidateFactDerived } from "../lib/query";
import { refusalText } from "../lib/notice";
import { FindingList, displayMonth, minutes, MonthContextNav } from "./month";

export function MonthReviewPage({ month }: { month: string }) {
  const queryClient = useQueryClient();
  const [acknowledged, setAcknowledged] = useState(false);
  const monthQuery = useQuery({ queryKey: ["month", month], queryFn: () => fetchMonth(month) });
  const findingsQuery = useQuery({
    queryKey: ["findings", month],
    queryFn: () => fetchFindings(month),
  });
  const review = useMutation({
    mutationFn: (acknowledged: boolean) => reviewMonth(month, acknowledged),
    onSuccess: () => invalidateFactDerived(queryClient),
  });
  if (monthQuery.isPending || findingsQuery.isPending)
    return <p role="status">Loading your private review…</p>;
  // Same permanent-vs-transient distinction as the month workspace: a month
  // outside policy/calendar coverage is a fact about the data, not a fault.
  if (monthQuery.isError || findingsQuery.isError) {
    const blocked = coverageBlockedMessage(
      firstErrorCode([monthQuery, findingsQuery]),
      displayMonth(month),
    );
    return (
      <p className="status-error" role="alert">
        {blocked ?? "Could not load this month review."}
      </p>
    );
  }
  const unresolved = findingsQuery.data.findings.length > 0;
  return (
    <section className="review-page">
      <p className="eyebrow">Private — visible only to you</p>
      <h1>Month review</h1>
      <p className="private-note">Reviewing the month does not resolve these findings.</p>
      <MonthContextNav month={month} current="review" />
      <dl className="review-summary">
        <div>
          <dt>Balance at end of this month</dt>
          <dd>{minutes(monthQuery.data.closing_balance_minutes)}</dd>
        </div>
        <div>
          <dt>Status</dt>
          <dd>{monthQuery.data.status === "REVIEWED" ? "Reviewed" : "Not reviewed"}</dd>
        </div>
        <div>
          <dt>Things to check</dt>
          <dd>{findingsQuery.data.findings.length}</dd>
        </div>
      </dl>
      <FindingList findings={findingsQuery.data.findings} month={month} headingLevel={2} />
      <div className="review-actions">
        {unresolved && (
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={acknowledged}
              onChange={(event) => setAcknowledged(event.target.checked)}
            />
            I have read these. Marking the month reviewed neither resolves nor shares them.
          </label>
        )}
        <button
          type="button"
          className="secondary-action"
          onClick={() => review.mutate(acknowledged)}
          disabled={review.isPending || (unresolved && !acknowledged)}
        >
          Mark month reviewed
        </button>
        {review.isSuccess && (
          <p role="status" className="status-ok">
            Month marked reviewed. No data was shared.
          </p>
        )}
        {review.isError && (
          <p role="status" className="status-error">
            {refusalText(review.error)}
          </p>
        )}
        {/* 9.10: the pair sits in one group, so the link takes the button's
         * exact secondary treatment and hugs its own content. It stays a LINK —
         * it navigates, so middle-click and "open in new tab" must keep working
         * (UI-044). */}
        <Link to="/month/$month" params={{ month }} className="secondary-action">
          Return to month and correct entries
        </Link>
      </div>
    </section>
  );
}
