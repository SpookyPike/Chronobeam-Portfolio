/* Two-step inline confirmation for consequential actions.
 *
 * Nothing that destroys a work-time fact or retracts a disclosure may happen on
 * a single click (review finding UI-006): a mis-click used to discard a recorded
 * interval, a break, a day-status fact or a whole trip, and to withdraw a
 * disclosure package. The pattern is deliberately inline rather than a modal —
 * the row keeps its context, and there is no focus trap to get wrong.
 *
 * Lifted out of `pages/admin-users.tsx`, which had the only safe implementation,
 * so every destructive control in the app can share it.
 *
 * Accessibility (acceptance finding A5, WCAG 2.2). Arming used to render a bare
 * `<span class="confirm-row">`: nothing told a screen-reader or keyboard user
 * that the control had changed state, so the second click could land on an armed
 * control they did not know about. Three requirements are met here:
 *   - SC 2.4.3 Focus Order — arming moves focus to the confirm control, so the
 *     next Tab and the next Enter both start from the new state instead of being
 *     stranded on a button that has just been replaced.
 *   - SC 4.1.3 Status Messages — the armed state is additionally stated in words
 *     in a `role="status"` region (implicit `aria-live="polite"` + `aria-atomic`),
 *     so it is announced even where a newly focused control's own name is not
 *     read out. That region is rendered in BOTH states and only its *text*
 *     changes: a live region mounted with its content already present is not
 *     reliably announced — which is precisely why the bare span was silent.
 *   - SC 2.4.3 again on cancel — the armed controls are unmounted, so focus is
 *     returned to the resting control rather than falling back to `<body>`.
 * The inline pattern is deliberate: no `role="dialog"`, no focus trap, no modal.
 *
 * `useConfirmArming` + `ConfirmStatus` are that behaviour, factored out so a
 * confirm whose resting control is not a single button (a select + button, e.g.
 * role assignment) reuses it rather than growing a second, silently-divergent
 * copy — which is how the original defect spread in the first place. */

import { useEffect, useRef, useState } from "react";

/**
 * The announcement + focus behaviour every inline two-step confirm needs,
 * regardless of the shape of its resting control. Extracted so a second confirm
 * implementation (a select + button, e.g. role assignment) gets the identical
 * behaviour instead of a second, silently-divergent copy of it.
 *
 * `arming` is the caller's own state; this hook owns the three refs the
 * behaviour touches and the two transitions that need them.
 */
export function useConfirmArming(arming: boolean, setArming: (next: boolean) => void) {
  const armedRef = useRef<HTMLButtonElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const restingRef = useRef<HTMLButtonElement>(null);
  const restoreFocus = useRef(false);

  useEffect(() => {
    if (arming) {
      // A disabled button cannot take focus (the armed confirm is disabled while
      // another mutation is in flight), so fall back to Cancel: the state change
      // must never be silent, and Cancel is always reachable.
      if (armedRef.current && !armedRef.current.disabled) armedRef.current.focus();
      else cancelRef.current?.focus();
    } else if (restoreFocus.current) {
      restoreFocus.current = false;
      restingRef.current?.focus();
    }
  }, [arming]);

  return {
    armedRef,
    cancelRef,
    restingRef,
    arm: () => setArming(true),
    /** Cancel, returning focus to the resting control (which re-mounts). */
    cancel: () => {
      restoreFocus.current = true;
      setArming(false);
    },
  };
}

/** The persistent live region that states an armed confirm in words. Render it in
 *  BOTH states; only its *text* changes — a live region mounted with its content
 *  already present is not reliably announced, which is why the bare
 *  `<span class="confirm-row">` was silent. */
export function ConfirmStatus({ arming, confirmLabel }: { arming: boolean; confirmLabel: string }) {
  return (
    <span className="confirm-status" role="status">
      {arming ? `Confirm: ${confirmLabel}` : ""}
    </span>
  );
}

export function ConfirmAction({
  label,
  confirmLabel,
  onConfirm,
  busy = false,
  disabled = false,
  className,
  confirmClassName = "danger",
  ariaLabel,
  title,
}: {
  /** Text on the resting control ("Delete", "Remove", "Withdraw"). */
  label: string;
  /** Text on the armed control; name the thing being destroyed. */
  confirmLabel: string;
  onConfirm: () => void;
  /** The mutation is in flight — the armed control shows "Saving…". */
  busy?: boolean;
  /** The action is impossible right now, so it cannot even be armed. */
  disabled?: boolean;
  className?: string;
  /** Inside `.row-menu-inline` this must stay `menu-danger`: a bare `danger`
   *  loses to that container's element-qualified button rule. */
  confirmClassName?: string;
  ariaLabel?: string;
  title?: string;
}) {
  const [arming, setArming] = useState(false);
  const { armedRef, cancelRef, restingRef, arm, cancel } = useConfirmArming(arming, setArming);

  return (
    <>
      <ConfirmStatus arming={arming} confirmLabel={confirmLabel} />
      {arming ? (
        <span className="confirm-row">
          <button
            ref={armedRef}
            type="button"
            className={confirmClassName}
            disabled={busy || disabled}
            onClick={onConfirm}
          >
            {busy ? "Saving…" : confirmLabel}
          </button>
          <button ref={cancelRef} type="button" onClick={cancel}>
            Cancel
          </button>
        </span>
      ) : (
        <button
          ref={restingRef}
          type="button"
          className={className}
          aria-label={ariaLabel}
          title={title}
          disabled={disabled}
          onClick={arm}
        >
          {label}
        </button>
      )}
    </>
  );
}
