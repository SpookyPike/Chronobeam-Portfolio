import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createRouter } from "@tanstack/react-router";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { PrivacyPage } from "./privacy";
import { routeTree } from "../router";

const JSON_HEADERS = { "Content-Type": "application/json" };

/** DP-RIGHTS-003 / FE-PRIV-006: engineering ships the stable route and the
 *  placeholder component; the legal wording stays DPO-owned. These tests pin
 *  both halves — the route/link exists, and the page does not present
 *  invented legal content as approved. */

function stubShell(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/v1/auth/session")) {
        return new Response(
          JSON.stringify({ authenticated: true, csrf_token: null, oidc_mode: false }),
          { status: 200, headers: JSON_HEADERS },
        );
      }
      if (url.endsWith("/api/v1/me")) {
        return new Response(
          JSON.stringify({
            subject: "demo-employee",
            display_name: "Mara Klein",
            capabilities: ["OWN_LEDGER_READ"],
          }),
          { status: 200, headers: JSON_HEADERS },
        );
      }
      if (url.includes("/api/v1/me/months/")) {
        return new Response(
          JSON.stringify({
            period_month: "2026-09-01",
            status: "OPEN",
            interval_count: 0,
            closing_balance_minutes: 0,
            days: [],
            daily_results: [],
          }),
          { status: 200, headers: JSON_HEADERS },
        );
      }
      return new Response(JSON.stringify({}), { status: 404, headers: JSON_HEADERS });
    }),
  );
}

function renderShell() {
  const router = createRouter({ routeTree });
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

describe("privacy notice placeholder", () => {
  it("marks the whole notice as pending DPO approval", () => {
    render(<PrivacyPage />);
    expect(screen.getByRole("heading", { level: 1, name: "Privacy notice" })).toBeInTheDocument();
    expect(screen.getByText("Text pending DPO approval")).toBeInTheDocument();
    expect(
      screen.getByText(/being finalised with the organisation’s Data Protection Officer/),
    ).toBeInTheDocument();
    // The notice must not explain how the software was organised.
    expect(screen.queryByText(/engineering team/)).not.toBeInTheDocument();
  });

  it("names every required placeholder: purposes, categories, recipients, retention, rights, contact", () => {
    render(<PrivacyPage />);
    for (const title of [
      "Who is responsible",
      "Why your data is processed",
      "What data is processed",
      "Who can receive your data",
      "How long data is kept",
      "Your rights",
      "Contact",
    ]) {
      const section = screen.getByRole("heading", { level: 2, name: title }).closest("section");
      expect(section).not.toBeNull();
      // No topic is presented as approved content.
      expect(within(section!).getByText(/Pending DPO approval/)).toBeInTheDocument();
    }
  });

  it("states the pending marker once, not once per topic", () => {
    render(<PrivacyPage />);
    const card = screen
      .getByRole("heading", { level: 2, name: "Who is responsible" })
      .closest("section");
    expect(card).not.toBeNull();
    expect(within(card!).getAllByText(/Pending DPO approval/)).toHaveLength(1);
  });

  it("credits the developer without claiming a data-protection role", () => {
    render(<PrivacyPage />);
    const responsible = screen
      .getByRole("heading", { level: 2, name: "Who is responsible" })
      .closest("section")!;
    expect(within(responsible).queryByText(/Dmitrii Bespalov/)).not.toBeInTheDocument();
    expect(screen.getByText(/Chronobeam was developed by/)).toHaveTextContent(
      "Chronobeam was developed by Dmitrii Bespalov. Questions about this notice go to the organisation’s Data Protection Officer.",
    );
  });
});

describe("privacy route and shell link", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    stubShell();
    window.history.replaceState({}, "", "/");
  });

  it("serves /privacy instead of the 404 fallback", async () => {
    window.history.replaceState({}, "", "/privacy");
    renderShell();
    expect(
      await screen.findByRole("heading", { level: 1, name: "Privacy notice" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Page not found" })).not.toBeInTheDocument();
  });

  it("links the notice from the shell footer and navigates there", async () => {
    const user = userEvent.setup();
    renderShell();
    const footer = await screen.findByRole("contentinfo");
    const link = within(footer).getByRole("link", { name: "Privacy / Data protection" });
    expect(link).toHaveAttribute("href", "/privacy");

    await user.click(link);
    expect(
      await screen.findByRole("heading", { level: 1, name: "Privacy notice" }),
    ).toBeInTheDocument();
  });

  it("shows the link for accounts with no work-area capability", async () => {
    vi.unstubAllGlobals();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.endsWith("/api/v1/auth/session")) {
          return new Response(
            JSON.stringify({ authenticated: true, csrf_token: null, oidc_mode: false }),
            { status: 200, headers: JSON_HEADERS },
          );
        }
        if (url.endsWith("/api/v1/me")) {
          return new Response(
            JSON.stringify({ subject: "demo-guest", display_name: "Guest", capabilities: [] }),
            { status: 200, headers: JSON_HEADERS },
          );
        }
        return new Response(JSON.stringify({}), { status: 404, headers: JSON_HEADERS });
      }),
    );
    window.history.replaceState({}, "", "/privacy");
    renderShell();
    await screen.findByLabelText("Current account");
    expect(
      within(screen.getByRole("contentinfo")).getByRole("link", {
        name: "Privacy / Data protection",
      }),
    ).toBeInTheDocument();
  });
});
