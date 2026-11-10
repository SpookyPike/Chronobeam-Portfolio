import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ConfirmAction } from "./confirm";

/** Acceptance finding A5 / WCAG 2.2 SC 2.4.3 + SC 4.1.3: the inline two-step
 *  confirm must announce its armed state and manage focus, and must give focus
 *  back to the resting control on cancel. */

describe("ConfirmAction", () => {
  it("stays unarmed and unannounced until it is clicked", () => {
    render(
      <ConfirmAction label="Delete" confirmLabel="Delete this interval?" onConfirm={vi.fn()} />,
    );
    expect(screen.getByRole("button", { name: "Delete" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  });

  it("announces the armed state and moves focus to the armed control", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(
      <ConfirmAction label="Delete" confirmLabel="Delete this interval?" onConfirm={onConfirm} />,
    );

    await user.click(screen.getByRole("button", { name: "Delete" }));

    const armed = screen.getByRole("button", { name: "Delete this interval?" });
    expect(screen.getByRole("status")).toHaveTextContent("Confirm: Delete this interval?");
    expect(document.activeElement).toBe(armed);
    // arming alone must not perform the action
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("returns focus to the resting control on cancel", async () => {
    const user = userEvent.setup();
    render(
      <ConfirmAction label="Delete" confirmLabel="Delete this interval?" onConfirm={vi.fn()} />,
    );

    await user.click(screen.getByRole("button", { name: "Delete" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    const resting = screen.getByRole("button", { name: "Delete" });
    expect(document.activeElement).toBe(resting);
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
    expect(screen.queryByRole("button", { name: "Delete this interval?" })).toBeNull();
  });

  it("keeps the state change non-silent when the armed control is disabled", async () => {
    const user = userEvent.setup();
    // `busy` mirrors a sibling confirmation's mutation being in flight, which
    // disables the armed control; a disabled button cannot take focus, so the
    // signal must fall back to Cancel rather than disappearing.
    render(
      <ConfirmAction
        label="Delete"
        confirmLabel="Delete this interval?"
        onConfirm={vi.fn()}
        busy
      />,
    );
    await user.click(screen.getByRole("button", { name: "Delete" }));

    expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Cancel" }));
  });
});
