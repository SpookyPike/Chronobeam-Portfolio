import { describe, expect, it } from "vitest";

import {
  AUTHORITATIVE_SOURCES,
  decisionMakerRoleOther,
  decisionMakerRoles,
  planSourceLabel,
} from "./labels";

describe("planSourceLabel", () => {
  it("names the employee's own record as a planned shift", () => {
    expect(planSourceLabel("EMPLOYEE_RECORDED")).toBe("Planned shift");
  });

  it("calls a genuinely authoritative source an approved plan", () => {
    // The set must not be empty, or the loop below would pass vacuously.
    expect(AUTHORITATIVE_SOURCES.size).toBeGreaterThan(0);
    expect(AUTHORITATIVE_SOURCES.has("INSTITUTIONAL_APPROVAL")).toBe(true);
    for (const source of AUTHORITATIVE_SOURCES) {
      expect(planSourceLabel(source)).toBe("Approved plan");
    }
  });

  // The point of this test. `source` is a free-text `Text` column, so "not
  // EMPLOYEE_RECORDED" is NOT the test for "approved": the fixture's
  // `TEST_SEED`, a future adapter's own code, a near-miss spelling or an absent
  // value would all fall into a naive `else` branch and be shown to the employee
  // as institutionally approved — an approval claim nobody made. A naive
  // if/else passes the two cases above and fails every case below.
  it.each([
    "TEST_SEED",
    "SOME_FUTURE_ADAPTER",
    "INSTITUTIONAL_APPROVAL_V2",
    "employee_recorded",
    "",
  ])("never claims approval for the unrecognised source %j", (source) => {
    expect(planSourceLabel(source)).toBe("Planned shift");
    expect(planSourceLabel(source)).not.toBe("Approved plan");
  });

  it("never claims approval when the source is absent", () => {
    expect(planSourceLabel(undefined)).toBe("Planned shift");
    expect(planSourceLabel(null)).toBe("Planned shift");
  });

  it("keeps the employee's own provenance OUT of the authoritative set", () => {
    // Recording your own plan is not self-authorisation (ACCESS-017). If this
    // ever fails, the backend set and this mirror have drifted apart.
    expect(AUTHORITATIVE_SOURCES.has("EMPLOYEE_RECORDED")).toBe(false);
  });
});

describe("decisionMakerRoles (D22)", () => {
  it("offers only the source-backed decision roles plus the escape hatch", () => {
    // "Local Contact" and "Works Council" are not cancellation decision-makers
    // in POL-02; offering them would imply an authority the policy does not
    // grant.
    expect(decisionMakerRoles).toEqual([
      "Director",
      "Department Head",
      "Supervisor / Group Leader",
      "Person nominated by the Management Board",
      decisionMakerRoleOther,
    ]);
  });
});
