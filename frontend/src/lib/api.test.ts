import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  MAX_TRANSIENT_RETRIES,
  fetchAuthSession,
  logout,
  request,
  setSessionExpiredHandler,
  shouldRetryQuery,
} from "./api";

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("production auth API state", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setSessionExpiredHandler(undefined);
  });

  it("stores the session CSRF token and attaches it only to unsafe requests", async () => {
    const fetch = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      response({ authenticated: true, csrf_token: "csrf-1", oidc_mode: true }),
    );
    vi.stubGlobal("fetch", fetch);
    await fetchAuthSession();
    await request("/safe");
    await request("/post", { method: "POST" });
    await request("/put", { method: "PUT" });
    await request("/patch", { method: "PATCH" });
    await request("/delete", { method: "DELETE" });
    expect(new Headers(fetch.mock.calls[1][1]?.headers).get("X-CSRF-Token")).toBeNull();
    for (const call of fetch.mock.calls.slice(2)) {
      expect(new Headers(call[1]?.headers).get("X-CSRF-Token")).toBe("csrf-1");
    }
  });

  it("clears CSRF on logout and reports a 401 to the shell", async () => {
    const expired = vi.fn();
    setSessionExpiredHandler(expired);
    const fetch = vi
      .fn(async (_input: RequestInfo | URL, _init?: RequestInit) => response({}))
      .mockResolvedValueOnce(
        response({ authenticated: true, csrf_token: "csrf-1", oidc_mode: true }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(response({}, 401));
    vi.stubGlobal("fetch", fetch);
    await fetchAuthSession();
    await logout();
    await request("/post-after-logout", { method: "POST" }).catch(() => undefined);
    expect(new Headers(fetch.mock.calls[1][1]?.headers).get("X-CSRF-Token")).toBe("csrf-1");
    expect(new Headers(fetch.mock.calls[2][1]?.headers).get("X-CSRF-Token")).toBeNull();
    expect(expired).toHaveBeenCalledOnce();
  });
});

describe("shouldRetryQuery (F7)", () => {
  it("never retries a deterministic refusal", () => {
    for (const status of [400, 401, 403, 404, 409, 422]) {
      const error = new ApiError("refused", status);
      for (let failureCount = 0; failureCount < 5; failureCount += 1) {
        expect(shouldRetryQuery(failureCount, error)).toBe(false);
      }
    }
  });

  it("retries a transient 5xx a bounded number of times", () => {
    const error = new ApiError("boom", 503);
    expect(shouldRetryQuery(0, error)).toBe(true);
    expect(shouldRetryQuery(MAX_TRANSIENT_RETRIES - 1, error)).toBe(true);
    expect(shouldRetryQuery(MAX_TRANSIENT_RETRIES, error)).toBe(false);
  });

  it("retries a network error (which is not an ApiError)", () => {
    const error = new TypeError("Failed to fetch");
    expect(shouldRetryQuery(0, error)).toBe(true);
    expect(shouldRetryQuery(MAX_TRANSIENT_RETRIES, error)).toBe(false);
  });
});
