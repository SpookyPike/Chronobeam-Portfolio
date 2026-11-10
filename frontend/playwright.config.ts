import { defineConfig, devices } from "@playwright/test";

/** Minimal E2E configuration: the critical journeys only, run locally and in
 *  CI (`e2e` job). The deterministic gates stay in pytest/Vitest. */
export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  fullyParallel: false,
  use: {
    baseURL: "http://127.0.0.1:5173",
    trace: "retain-on-failure",
    // PHASE-11 F09: en-GB so native date/time controls render 24-hour in
    // test sessions, never US AM/PM. The launch --lang (not just the context
    // locale) drives Chromium's internal form-control localization.
    locale: "en-GB",
    launchOptions: { args: ["--lang=en-GB"] },
  },
  projects: [
    {
      name: "desktop",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
    {
      name: "desktop-sm",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } },
    },
    // Keep one installed Chromium engine while exercising the required phone
    // dimensions; an iPhone device preset would switch to WebKit unnecessarily.
    {
      name: "mobile",
      use: { ...devices["Desktop Chrome"], viewport: { width: 390, height: 844 }, isMobile: true },
    },
    {
      name: "mobile-sm",
      use: { ...devices["Desktop Chrome"], viewport: { width: 360, height: 800 }, isMobile: true },
    },
  ],
  webServer: {
    // Bind the exact address `url` polls. vite's default host is `localhost`,
    // which Node may resolve to `::1` — on a runner that makes the IPv4 poll
    // fail for the whole timeout. `--strictPort` turns a port clash into a
    // named failure instead of vite silently moving and the poll timing out.
    // The timeout is generous because a cold runner has no vite dep cache.
    command: "pnpm dev --host 127.0.0.1 --port 5173 --strictPort",
    url: "http://127.0.0.1:5173",
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
