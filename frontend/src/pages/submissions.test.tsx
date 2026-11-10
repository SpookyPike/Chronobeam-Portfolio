import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { HumanPreview } from "./submissions";

/** The recipient-visible projection, in words (plan §6.2/§8.10).
 *
 *  `HumanPreview` is the employee's only check on what a disclosure contains and
 *  the recipient's only record of it, so the plan provenance and the two new
 *  plan-entry facts are asserted here rather than only on the card that produced
 *  them: a field that is missing from this component is missing from the package
 *  as the reader sees it. */
describe("HumanPreview plan provenance", () => {
  it("names the plan source and the compensation review state on a Shift Report", () => {
    render(
      <HumanPreview
        purpose="SHIFT_REPORT"
        audience="recipient"
        preview={{
          report_type: "SHIFT_REPORT",
          employee_identifier: "emp-1",
          plan_source: "EMPLOYEE_RECORDED",
          compensation_review_status: "MANUAL_REVIEW_REQUIRED",
        }}
      />,
    );
    // Fail-closed: the employee's own record is never presented as approved.
    expect(screen.getByText("Plan source: Planned shift")).toBeInTheDocument();
    expect(screen.queryByText(/Approved plan/)).not.toBeInTheDocument();
    // What the recipient gets INSTEAD of an entitlement, so a human decides.
    expect(
      screen.getByText("Compensation review: Compensation needs manual review"),
    ).toBeInTheDocument();
  });

  it("keeps an unrecognised source neutral rather than claiming approval", () => {
    render(
      <HumanPreview
        purpose="SHIFT_REPORT"
        audience="recipient"
        preview={{
          report_type: "SHIFT_REPORT",
          employee_identifier: "emp-1",
          plan_source: "SOME_FUTURE_ADAPTER",
          compensation_review_status: "NOT_APPLICABLE",
        }}
      />,
    );
    expect(screen.getByText("Plan source: Planned shift")).toBeInTheDocument();
    expect(screen.queryByText(/Approved plan/)).not.toBeInTheDocument();
    expect(
      screen.getByText("Compensation review: No compensation review needed"),
    ).toBeInTheDocument();
  });

  it("renders the OCD compensation provenance and its reported notice as reported", () => {
    render(
      <HumanPreview
        purpose="OCD_COMPENSATION"
        audience="employee"
        preview={{
          report_type: "OCD_COMPENSATION",
          employee_identifier: "emp-1",
          assignment_start_at: "2026-07-19T19:00:00+02:00",
          assignment_end_at: "2026-07-20T08:00:00+02:00",
          plan_source: "EMPLOYEE_RECORDED",
          reported_notice_at: "2026-06-01T10:00:00+02:00",
        }}
      />,
    );
    expect(screen.getByText("Plan source: Planned on-call duty")).toBeInTheDocument();
    // REPORTED, not authoritative: no classifier reads it, so it decides no
    // allowance and must not read like an institutional notice time.
    expect(screen.getByText(/Notice \(as reported\): 01 Jun 2026, 10:00/)).toBeInTheDocument();
    expect(screen.getByText(/it decides no allowance/)).toBeInTheDocument();
  });

  it("invents no provenance or notice line when the payload carries neither", () => {
    render(
      <HumanPreview
        purpose="OCD_COMPENSATION"
        audience="recipient"
        preview={{
          report_type: "OCD_COMPENSATION",
          employee_identifier: "emp-1",
          assignment_start_at: "2026-07-19T19:00:00+02:00",
          assignment_end_at: "2026-07-20T08:00:00+02:00",
        }}
      />,
    );
    expect(screen.queryByText(/Plan source/)).not.toBeInTheDocument();
    expect(screen.queryByText(/as reported/)).not.toBeInTheDocument();
  });

  it("leaves an overtime package without plan-entry lines", () => {
    render(
      <HumanPreview
        purpose="OVERTIME_PAID"
        audience="employee"
        preview={{ report_type: "OVERTIME", employee_identifier: "emp-1" }}
      />,
    );
    expect(screen.queryByText(/Plan source/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Compensation review/)).not.toBeInTheDocument();
  });
});
