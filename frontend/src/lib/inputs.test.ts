import { describe, expect, it } from "vitest";

import { momentDisplay, nextDate, resolveMoment } from "./inputs";

/* The contract `MomentField` (the shift card's five moment fields) rests on: a
 * bare "HH:MM" is enough for a moment on the row's own day, and a stated date
 * always wins. The ROLL rule is the load-bearing half — it is what puts a night
 * shift's post-midnight break on the next morning without a second control. */

const SHIFT_DAY = "2026-07-31"; // a night shift starting 19:00
const SHIFT_START = "19:00";

describe("resolveMoment", () => {
  it("resolves a bare time against the row's own day", () => {
    expect(resolveMoment("07:00", SHIFT_DAY)).toBe("2026-07-31T07:00");
    // Loose entry is folded, not refused: "8:30" is "08:30".
    expect(resolveMoment("8:30", SHIFT_DAY)).toBe("2026-07-31T08:30");
    expect(resolveMoment("0830", SHIFT_DAY)).toBe("2026-07-31T08:30");
  });

  it("rolls a time before the shift start to the next day", () => {
    // 00:30 on a 19:00->07:00 shift is the NEXT morning, not 19 hours earlier.
    expect(resolveMoment("00:30", SHIFT_DAY, SHIFT_START)).toBe("2026-08-01T00:30");
    // The boundary is strict: the shift's own start time stays on its own day.
    expect(resolveMoment("19:00", SHIFT_DAY, SHIFT_START)).toBe("2026-07-31T19:00");
    expect(resolveMoment("18:59", SHIFT_DAY, SHIFT_START)).toBe("2026-08-01T18:59");
    // Without a roll bound (the arbitrary-instant fields) nothing moves.
    expect(resolveMoment("00:30", SHIFT_DAY)).toBe("2026-07-31T00:30");
  });

  it("lets a stated date win, in either accepted form", () => {
    expect(resolveMoment("02.08.2026 06:00", SHIFT_DAY, SHIFT_START)).toBe("2026-08-02T06:00");
    expect(resolveMoment("2026-08-02T06:00", SHIFT_DAY)).toBe("2026-08-02T06:00");
  });

  it("keeps a mid-typing fragment raw rather than guessing", () => {
    for (const fragment of ["", "0", "07", "07:", "07:3", "07:60", "25:00"]) {
      expect(resolveMoment(fragment, SHIFT_DAY)).toBeNull();
    }
  });
});

describe("momentDisplay", () => {
  it("shows a bare time only for the day its own rule derives", () => {
    expect(momentDisplay("2026-07-31T19:00", SHIFT_DAY)).toBe("19:00");
    expect(momentDisplay("2026-08-01T00:30", SHIFT_DAY, SHIFT_START)).toBe("00:30");
  });

  it("shows the whole datetime when the value carries another day", () => {
    // A date the employee STATED must never be quietly reduced to a time.
    expect(momentDisplay("2026-08-02T06:00", SHIFT_DAY)).toBe("02.08.2026 06:00");
    expect(momentDisplay("2026-08-02T06:00", SHIFT_DAY, SHIFT_START)).toBe("02.08.2026 06:00");
  });

  it("passes an unparseable value through unchanged", () => {
    expect(momentDisplay("07:3", SHIFT_DAY)).toBe("07:3");
    expect(momentDisplay("", SHIFT_DAY)).toBe("");
  });
});

describe("nextDate", () => {
  it("advances one calendar day, across month and year ends", () => {
    expect(nextDate("2026-07-31")).toBe("2026-08-01");
    expect(nextDate("2026-12-31")).toBe("2027-01-01");
    expect(nextDate("2028-02-28")).toBe("2028-02-29"); // leap year
  });
});
