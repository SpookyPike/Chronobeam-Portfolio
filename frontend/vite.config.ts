import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";
import type { IncomingMessage } from "node:http";

/**
 * The only subjects `CHRONOBEAM_DEV_PRINCIPAL` may bind an origin to — the
 * presentation personas `just demo` starts. The seam is dev-only and the
 * backend already rejects any subject it does not know with 401
 * AUTH_UNKNOWN_PRINCIPAL (`app/auth/dev_principals.py` is the authority), so
 * this is a second, earlier gate: an environment variable can never point the
 * dev proxy at an arbitrary subject, and an unrecognised value falls back to
 * the plain fixture subject rather than being forwarded.
 */
const DEMO_PRINCIPALS = [
  "demo-employee",
  "demo-hr",
  "demo-admin",
  // The lifecycle/acceptance employee: `demo-clean` is the seeded persona whose
  // September 2026 facts work exactly to requirement, so its ledger delta is zero
  // and a cutover in a later month opens on the seeded carry alone.
  "demo-clean",
  // The recipient side of the disclosure boundary. `demo-hr` holds the
  // random-check requester role; this is the other half (SUBMISSION_RECIPIENT),
  // so the demo can show who may read a submitted package.
  "demo-recipient",
  // The onboarding persona: the only demo employee seeded with NO facts, so its
  // start-state form is available to demonstrate (every other persona is onboarded
  // in September with a populated schedule, which the clean-cutover guard correctly
  // refuses to combine with an opening balance). Adding it here as well as in `app/auth/dev_principals.py`
  // and `scripts/demo/demo.sh` is the whole registration: this list is a SECOND,
  // earlier gate, and a subject missing from it falls back to the fixture
  // silently rather than failing.
  "demo-onboarding",
] as const;

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Dev-only proxy to the local backend; no production traffic flows here.
    proxy: {
      "/health": "http://127.0.0.1:8000",
      // Development-only principal seam. Production uses the configured OIDC
      // boundary and never receives this header (DP-AUTH-001/002). A browser-
      // supplied principal header wins (PHASE-11 demo tooling drives persona
      // switches against the real backend); otherwise the default fixture
      // subject applies, so plain `pnpm dev` keeps behaving as before.
      //
      // `CHRONOBEAM_DEV_PRINCIPAL` overrides that default for one dev server,
      // which is how `just demo` binds a persona to an origin: one server per
      // presentation persona, so each persona is a separate origin with its own
      // identity and no tab can leak into another. It is restricted to
      // `DEMO_PRINCIPALS`, and unset (plain `pnpm dev`, `vite build`) the
      // default is unchanged.
      "/api": {
        target: "http://127.0.0.1:8000",
        configure: (proxy) => {
          proxy.on("proxyReq", (proxyReq, req: IncomingMessage) => {
            const header = req.headers["x-chronobeam-dev-principal"];
            const subject = Array.isArray(header) ? header[0] : header;
            const configured = process.env.CHRONOBEAM_DEV_PRINCIPAL;
            const bound = DEMO_PRINCIPALS.find((principal) => principal === configured);
            proxyReq.setHeader("X-Chronobeam-Dev-Principal", subject || bound || "employee");
          });
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    // Playwright specs are collected by Playwright, not Vitest.
    exclude: ["e2e/**", "demo/**", "node_modules/**", "dist/**"],
  },
});
