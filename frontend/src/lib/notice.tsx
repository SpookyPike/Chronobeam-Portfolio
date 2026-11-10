/* One feedback channel per action.
 *
 * Acceptance findings E4/C9: a card could show a sticky success line AND a
 * separate failure line at the same time, neither associated with the action that
 * produced it, and the failure was the unactionable "Request validation failed.".
 * The contract here is:
 *   - ONE notice per card: a new attempt replaces the previous one, so a stale
 *     success can never sit beside a current failure;
 *   - an error outranks a success and is announced (`role="alert"`), while a
 *     success is a status (`role="status"`) — an alarm for good news trains
 *     people to ignore alarms;
 *   - a rejected field's message sits beside that field, in the form's own words
 *     (WCAG 2.2 SC 3.3.1 Error Identification / 3.3.3 Error Suggestion).
 *
 * No toast system, no queue: the notice belongs to the card the employee acted
 * on, and it disappears with it. */

import { ApiError, fieldIssueFor } from "./api";

export type Notice = { kind: "ok" | "error"; text: string } | null;

/** The notice for a failed action. A 422 is summarised in product words and its
 *  detail is left to the fields it names — the server's raw sentence
 *  ("Request validation failed.") names nothing and cannot be acted on. */
export function errorNotice(error: unknown): Notice {
  if (error instanceof ApiError && error.fieldIssues.length > 0) {
    return { kind: "error", text: "Some details could not be accepted — check the marked fields." };
  }
  return { kind: "error", text: refusalText(error) };
}

/** The product sentence for a failed action, for the call sites that keep their
 *  own notice shape instead of using `errorNotice`.
 *
 *  An `ApiError`'s `message` is human-readable by contract (BACKEND_SPEC §11),
 *  so it is shown. Anything else is a thrown client/network fault whose
 *  `message` is developer text ("Failed to fetch", "Cannot read properties of
 *  undefined") — showing it tells the employee nothing and leaks internals
 *  (review R6). Every employee-facing error path must go through this. */
export function refusalText(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  return "Something went wrong and nothing was changed. Try again.";
}

export function okNotice(text: string): Notice {
  return { kind: "ok", text };
}

export function ActionNotice({ notice }: { notice: Notice }) {
  if (!notice) return null;
  return notice.kind === "error" ? (
    <p className="status-error" role="alert">
      {notice.text}
    </p>
  ) : (
    <p className="status-ok" role="status">
      {notice.text}
    </p>
  );
}

/** The message the server attached to one field of a rejected request, rendered
 *  where that field is. Place it as a SIBLING of the field's `<label>`, not
 *  inside it: text inside a wrapping label becomes part of the control's
 *  accessible name, which would turn the field's name into its error message.
 *  The field itself carries `aria-describedby` pointing at this element. */
export function FieldIssue({ error, field, id }: { error: unknown; field: string; id: string }) {
  const message = fieldIssueFor(error, field);
  if (!message) return null;
  return (
    <span className="field-error" id={id} role="alert">
      {message}
    </span>
  );
}
