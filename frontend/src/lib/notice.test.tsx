import { render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import { ApiError, request } from "./api";
import { ActionNotice, errorNotice, FieldIssue, okNotice, refusalText } from "./notice";

function validationResponse(details: Array<{ loc: string[]; msg: string }>): Response {
  return new Response(
    JSON.stringify({
      detail: { code: "VALIDATION_FAILED", message: "Request validation failed.", details },
    }),
    { status: 422, headers: { "Content-Type": "application/json" } },
  );
}

afterEach(() => vi.unstubAllGlobals());

async function rejected(details: Array<{ loc: string[]; msg: string }>): Promise<ApiError> {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => validationResponse(details)),
  );
  try {
    await request("/api/v1/me/ocd-calls", { method: "POST" });
  } catch (error) {
    return error as ApiError;
  }
  throw new Error("expected the request to be rejected");
}

it("turns a 422's field detail into product words", async () => {
  const error = await rejected([
    { loc: ["body", "call_start_local"], msg: "String should match pattern 'x'" },
  ]);
  expect(error.fieldIssues).toEqual([
    {
      field: "call_start_local",
      label: "Call start",
      message: "Enter the call start as DD.MM.YYYY HH:MM.",
    },
  ]);
});

it("uses the generic sentence for a field the form does not know", async () => {
  const error = await rejected([{ loc: ["body", "some_new_field"], msg: "Field required" }]);
  expect(error.fieldIssues).toEqual([
    {
      field: "some_new_field",
      label: "",
      message: "Some details could not be accepted — check the form.",
    },
  ]);
});

it("never renders the raw loc array or the backend's English msg", async () => {
  const error = await rejected([
    { loc: ["body", "noticed_or_decided_local"], msg: "Value error, not a datetime" },
  ]);
  render(
    <>
      <ActionNotice notice={errorNotice(error)} />
      <FieldIssue error={error} field="noticed_or_decided_local" id="issue-1" />
    </>,
  );
  const text = document.body.textContent ?? "";
  // The raw path element, the Pydantic sentence and the server's own envelope
  // message all stay out of the DOM.
  expect(text).not.toContain("noticed_or_decided_local");
  expect(text).not.toContain("Value error, not a datetime");
  expect(text).not.toContain("Request validation failed.");
  // What the employee reads is the summary plus the field's own instruction.
  expect(text).toContain("Some details could not be accepted — check the marked fields.");
  expect(text).toContain("Use the format DD.MM.YYYY HH:MM");
});

it("announces a failure as an alert and a success as a status, never both", () => {
  const { unmount } = render(<ActionNotice notice={okNotice("Call recorded.")} />);
  expect(screen.getByRole("status")).toHaveTextContent("Call recorded.");
  expect(screen.queryByRole("alert")).toBeNull();
  unmount();

  render(<ActionNotice notice={errorNotice(new ApiError("Refused.", 409))} />);
  expect(screen.getByRole("alert")).toHaveTextContent("Refused.");
  expect(screen.queryByRole("status")).toBeNull();
});

it("keeps the server's own sentence when it is not a validation failure", () => {
  // A 409 (a stale fingerprint, say) has no per-field detail, so its message IS
  // the actionable text and must survive.
  expect(errorNotice(new ApiError("The preview is out of date.", 409))).toEqual({
    kind: "error",
    text: "The preview is out of date.",
  });
});

/**
 * The envelope's `message` is human-readable BY CONTRACT (BACKEND_SPEC §11), so
 * an ApiError's message is the designed thing to show. Anything that is NOT an
 * ApiError is a thrown client/network fault whose message is developer text
 * ("Failed to fetch"), which tells the employee nothing and leaks internals.
 */
it("never shows a thrown client error's own message", () => {
  const network = new TypeError("Failed to fetch");
  expect(errorNotice(network)).toEqual({
    kind: "error",
    text: "Something went wrong and nothing was changed. Try again.",
  });
});

it("shows the server's own sentence for a refusal that names no field", async () => {
  const error = new ApiError("Trip changed; refresh and retry.", 409, "CONFLICT_VERSION");
  expect(errorNotice(error)).toEqual({ kind: "error", text: "Trip changed; refresh and retry." });
});

/**
 * `refusalText` is the single legal place a raw `message` is read. An `ApiError`'s
 * message is human-readable by contract (BACKEND_SPEC §11); anything else is a
 * thrown client/network fault whose message is developer text (review R6).
 */
it("refusalText shows an ApiError's sentence and hides a thrown fault's", () => {
  expect(
    refusalText(new ApiError("Trip changed; refresh and retry.", 409, "CONFLICT_VERSION")),
  ).toBe("Trip changed; refresh and retry.");
  expect(refusalText(new TypeError("Failed to fetch"))).toBe(
    "Something went wrong and nothing was changed. Try again.",
  );
  expect(refusalText("a bare string")).toBe(
    "Something went wrong and nothing was changed. Try again.",
  );
});
