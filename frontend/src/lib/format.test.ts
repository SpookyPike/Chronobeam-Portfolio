import { describe, expect, it } from "vitest";

import { berlinRange } from "./format";

describe("berlinRange", () => {
  it("drops the repeated date for a range inside one Berlin day", () => {
    expect(berlinRange("2026-07-21T04:00:00Z", "2026-07-21T12:00:00Z")).toBe(
      "21 Jul 2026, 06:00 → 14:00",
    );
  });

  it("repeats the end date when the range crosses midnight", () => {
    // INV-02: "19:00 → 07:00" does not say whether 07:00 is that morning or
    // the next one, so the end date has to be stated.
    expect(berlinRange("2026-07-19T17:00:00Z", "2026-07-20T05:00:00Z")).toBe(
      "19 Jul 2026, 19:00 → 20 Jul 2026, 07:00",
    );
  });

  it("degrades to the placeholder instead of throwing on a missing end", () => {
    // A partial payload must not break the page (see berlinTime's contract).
    expect(berlinRange("2026-07-19T17:00:00Z", "undefined")).toBe("19 Jul 2026, 19:00 → —");
    expect(berlinRange("2026-07-19T17:00:00Z", "")).toBe("19 Jul 2026, 19:00 → —");
  });
});
