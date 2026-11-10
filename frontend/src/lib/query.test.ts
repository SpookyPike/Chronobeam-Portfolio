import { QueryClient } from "@tanstack/react-query";
import { expect, it } from "vitest";

import { FACT_DERIVED_QUERY_KEYS, invalidateFactDerived } from "./query";

/** A cache holding one entry per fact-derived family for TWO months, plus a few
 *  caches that are deliberately NOT derived from employee facts. */
const derivedKeys: ReadonlyArray<readonly unknown[]> = [
  ["month", "2026-12"],
  ["month", "2027-01"],
  ["findings", "2026-12"],
  ["day", "2026-12", "2026-12-31"],
  ["day-status", "2026-12"],
  ["shifts", "2026-12"],
  ["ocd", "2026-12"],
  ["travel-summary", "2026-12"],
  ["travel-trips"],
  ["home-month-summary", "2026-12"],
  // The overtime compensation preview renders the ELECTION and the classified
  // components, both recomputed server-side — so choosing PAY/TIME moves it.
  ["overtime", "2026-12"],
  ["overtime-preview", "2026-12"],
  // The submissions scope pickers read the same facts under their own keys.
  ["submission-shifts", "2026-12"],
  ["submission-ocd", "2026-12"],
  ["onboarding"],
  ["onboarding", "2026-09"],
];

const unrelatedKeys: ReadonlyArray<readonly unknown[]> = [
  ["identity"],
  ["admin-users", "", "active"],
  ["admin-user", "user-1"],
  ["submissions"],
  ["recipient-submissions"],
  ["my-access-requests"],
  // NOT derived from the time ledger: the employee's own arrangement is an input
  // to it, and an access request is an authorization event, not a fact.
  ["profile-at", "2026-12-01"],
  ["election", "OVERTIME", "auth-1"],
];

function seeded(): QueryClient {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  for (const key of [...derivedKeys, ...unrelatedKeys]) client.setQueryData(key, { seeded: true });
  return client;
}

const isInvalidated = (client: QueryClient, key: readonly unknown[]): boolean =>
  client.getQueryState(key)?.isInvalidated === true;

it("invalidates every family it declares", () => {
  const client = seeded();
  invalidateFactDerived(client);
  for (const key of derivedKeys) {
    expect(isInvalidated(client, key), `not invalidated: ${JSON.stringify(key)}`).toBe(true);
  }
});

/**
 * The discriminating case: one fact can change more than one month. An interval
 * may cross midnight, a month boundary or a year boundary (the acceptance
 * campaign's own `31 Dec 22:00 → 1 Jan 03:00` interval contributes to both
 * December and January), and the month view derives each month by chaining the
 * earlier ones. A month-scoped implementation would invalidate only the month it
 * was handed; this one cannot be handed a month at all.
 */
it("invalidates the neighbouring month of a fact that crosses the year boundary", () => {
  const client = seeded();
  invalidateFactDerived(client);
  expect(isInvalidated(client, ["month", "2026-12"])).toBe(true);
  expect(isInvalidated(client, ["month", "2027-01"])).toBe(true);
});

it("leaves caches that are not derived from employee facts alone", () => {
  const client = seeded();
  invalidateFactDerived(client);
  for (const key of unrelatedKeys) {
    expect(isInvalidated(client, key), `unexpectedly invalidated: ${JSON.stringify(key)}`).toBe(
      false,
    );
  }
});

it("declares each family exactly once", () => {
  expect([...FACT_DERIVED_QUERY_KEYS].sort()).toEqual(
    [
      "day",
      "day-status",
      "findings",
      "home-month-summary",
      "month",
      "ocd",
      "overtime",
      "overtime-preview",
      "shifts",
      "onboarding",
      "submission-ocd",
      "submission-shifts",
      "travel-summary",
      "travel-trips",
    ].sort(),
  );
});
