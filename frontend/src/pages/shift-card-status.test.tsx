import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";

import { ShiftPlanActualCard } from "./shift-card";
import type { Shift } from "../lib/api";

/* Plan §10.5 test 63 / §6.2.1.
 *
 * The three non-active statuses are all READ-ONLY, and the gate that makes them
 * read-only is a POSITIVE test on `PLANNED` (it used to be `!== "SUPERSEDED"`,
 * which left a WITHDRAWN plan showing the confirm, deviation and paid-break
 * controls). Being read-only is not the same as being interchangeable, though:
 * a withdrawal is the employee's own retraction of a mistaken entry and carries
 * NO entitlement, a cancellation is the operational Company event that PRESERVES
 * the plan and may carry allowances, and a supersession is lineage. One shared
 * "not active" sentence would misreport the employee's own record back to them,
 * so each status must own its words — and this file pins all three. */

/** No findings: the findings `<details>` carries a `<summary>`, which maps to the
 *  `button` role in the accessibility tree. The zero-button assertion below is
 *  meant to be about the card's ACTIONS, so the fixture removes the ambiguity
 *  rather than filtering around it. */
const base = {
  id: "shift-1",
  shift_kind: "NIGHT",
  plan_source: "EMPLOYEE_RECORDED",
  approved_revision: {
    id: "revision-1",
    revision_no: 1,
    planned_start_at: "2026-07-31T17:00:00Z",
    planned_end_at: "2026-08-01T05:00:00Z",
    communicated_at: null,
  },
  actual_start_at: null,
  actual_end_at: null,
  deviation: null,
  findings: [],
  paid_break_status: "EXPLICIT_FACT_REQUIRED" as const,
  paid_breaks: [],
};

/** The three statuses, each with the dependencies a careless gate would miss:
 *  `CANCELLED` carries a deviation (which used to auto-open the deviation form),
 *  and `SUPERSEDED` carries one too, so a gate that only hid the BUTTONS while
 *  still rendering the FORM fails here. */
const nonActive: Array<[Shift, string]> = [
  [{ ...base, status: "WITHDRAWN" }, "Withdrawn"],
  [
    {
      ...base,
      status: "CANCELLED",
      deviation: {
        id: "deviation-1",
        deviation_type: "CANCELLED",
        actual_start_at: null,
        actual_end_at: null,
        noticed_or_decided_at: "2026-07-30T08:00:00Z",
        decision_maker_type: "Department Head",
        reason: null,
        version: 1,
      },
    },
    "Cancelled",
  ],
  [
    {
      ...base,
      status: "SUPERSEDED",
      deviation: {
        id: "deviation-2",
        deviation_type: "SHORTENED",
        actual_start_at: "2026-07-31T17:05:00Z",
        actual_end_at: "2026-08-01T05:00:00Z",
        noticed_or_decided_at: null,
        decision_maker_type: null,
        reason: null,
        version: 1,
      },
    },
    "Superseded by a later revision",
  ],
];

function renderCard(shift: Shift) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <ShiftPlanActualCard shift={shift} />
    </QueryClientProvider>,
  );
}

afterEach(cleanup);

it("gives every non-active status its own words, and no two are the same", () => {
  const seen = new Set<string>();
  for (const [shift, copy] of nonActive) {
    renderCard(shift);
    expect(screen.getByText(copy)).toBeInTheDocument();
    cleanup();
    expect(seen.has(copy)).toBe(false);
    seen.add(copy);
  }
  // The distinction is the point: sharing one sentence would make these three
  // assertions pass individually while the three statuses became synonyms.
  expect(seen.size).toBe(3);
});

it("renders no action button at all for a non-active status", () => {
  for (const [shift] of nonActive) {
    renderCard(shift);
    // Not "the destructive one is hidden": NOTHING actionable is offered, and the
    // deviation FORM stays closed even though `CANCELLED`/`SUPERSEDED` arrive with
    // a deviation attached (which used to open it on mount).
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(screen.queryByRole("heading", { name: "Actual difference" })).toBeNull();
    expect(screen.queryByRole("heading", { name: "Shift cancellation" })).toBeNull();
    expect(screen.queryByText("Save deviation")).toBeNull();
    cleanup();
  }
});

it("still states the plan's times and provenance on a non-active card", () => {
  // Read-only is not the same as empty: the employee keeps their own history, so
  // the retained plan stays readable rather than collapsing to a status line.
  renderCard(nonActive[0][0]);
  expect(screen.getByText("Planned shift")).toBeInTheDocument();
  expect(screen.getByText(/31 Jul 2026, 19:00/)).toBeInTheDocument();
  expect(screen.queryByText("Approved plan")).toBeNull();
});
