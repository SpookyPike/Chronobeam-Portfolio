import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";

import { DashboardPage } from "./dashboard";

// The page renders `<Link>`, which needs a router context. Replacing it keeps the
// page component under test directly (the idiom `month.test.tsx` uses).
vi.mock("@tanstack/react-router", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@tanstack/react-router")>()),
  Link: ({ children }: { children: ReactNode }) => <>{children}</>,
}));

/** Acceptance finding F5: Home reported a PERMANENT coverage boundary as a
 *  transient fault — "Balance unavailable right now." — which is both untrue and
 *  an invitation to retry something that cannot change. The ledger starts at the
 *  cutover month; earlier months stay in the Excel archive by design (INV-13). */

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderHome(): void {
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <DashboardPage />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.unstubAllGlobals());

it("names the coverage boundary instead of reporting a transient failure", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/v1/me")) {
        return response({
          subject: "emp-2001",
          display_name: "Martin Weber",
          capabilities: ["OWN_LEDGER_READ"],
        });
      }
      if (url.includes("/api/v1/me/onboarding")) {
        return response({
          state: "COMPLETED",
          month: "2026-10",
          default_start_month: "2026-10",
          opening: {
            cutover_month: "2026-10",
            minutes: 924,
            seed_minutes: 924,
            occurred_on: "2026-10-01",
            previous_period: "2026-09",
            confirmed_by: "emp-2001",
            revision: 1,
            corrected_at: null,
          },
          blocked_reason: null,
          blocked_month: null,
          has_facts_in_month: true,
          correctable: false,
          in_use_reason: null,
          schedule: null,
        });
      }
      if (url.includes("/api/v1/me/months/")) {
        // Every month the walk-back reaches is outside the covered rules: the
        // boundary is a fact about the data, not a fault.
        return response(
          { detail: { code: "POLICY_BLOCKED", message: "Outside the covered rules." } },
          409,
        );
      }
      return response({}, 404);
    }),
  );

  renderHome();

  // The boundary is named, with its own consequence.
  expect(await screen.findByText(/No working-time rules are installed for/)).toBeInTheDocument();
  // And the truthful sentence about where the ledger actually starts.
  expect(screen.getByText(/Your ledger starts in October 2026/)).toBeInTheDocument();
  // The lie is gone.
  expect(screen.queryByText("Balance unavailable right now.")).not.toBeInTheDocument();
});
