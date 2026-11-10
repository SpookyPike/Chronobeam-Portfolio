/* Compact overflow menu (More…, ⋮): items render only while open and close on
 * selection, outside click, or Escape.
 *
 * Lifted out of `pages/month.tsx` so the shift card's action hierarchy can use the
 * SAME menu the day panel and interval rows already use, rather than growing a
 * second implementation. Native `<details>` was rejected deliberately: it kept its
 * content in jsdom's accessibility tree even while collapsed, which made a closed
 * menu's actions reachable by assistive technology and by tests — the controlled
 * popover is the deterministic choice for both.
 *
 * Accessibility (WCAG 2.2). The toggle is a real `<button>` carrying
 * `aria-expanded`, so its state is programmatic and not merely visual; Escape
 * closes it; and focus never moves into a menu that has been closed. */

import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";

export function OverflowMenu({
  toggleLabel,
  toggleText,
  className,
  popoverClassName,
  items,
}: {
  /** The toggle's accessible name, when `toggleText` is not self-describing. */
  toggleLabel: string;
  toggleText: ReactNode;
  className: string;
  popoverClassName: string;
  items: (close: () => void) => ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [openUp, setOpenUp] = useState(false);
  // Horizontal correction, in px. The popover is anchored `right: 0` to its
  // trigger, so a trigger that has wrapped to the left of a narrow card pushes the
  // popover off the LEFT edge of the viewport — measured at 320px, the shift menu
  // landed at x=-108 and its item labels were unreadable. Applied as a negative
  // `margin-right` rather than a transform so it lands without animating.
  const [shiftX, setShiftX] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);
  const popover = useRef<HTMLDivElement>(null);
  // Decide the direction from the menu's REAL height, measured after it mounts and
  // before the browser paints it. A fixed estimate is wrong here: this menu is
  // 326px when the shift card carries its steering paragraph, so a 170px guess
  // opened it 43px past the bottom of a 1200px viewport with its last item — the
  // destructive one — half off-screen. Runs only when `open` changes, so it cannot
  // oscillate between the two directions.
  useLayoutEffect(() => {
    if (!open) return;
    const pop = popover.current;
    const toggleRect = toggle.current?.getBoundingClientRect();
    if (!pop || !toggleRect) return;
    const margin = 8;
    // Vertical. Below is the default; flip up only when the menu genuinely does
    // not fit and there is more room above than below. When neither fits, the
    // popover's own `max-height` keeps it reachable.
    const spaceBelow = window.innerHeight - toggleRect.bottom;
    const spaceAbove = toggleRect.top;
    setOpenUp(spaceBelow < pop.offsetHeight + margin && spaceAbove > spaceBelow);
    // Horizontal. Clamp the measured box back inside the viewport, in whichever
    // direction it escaped.
    const rect = pop.getBoundingClientRect();
    if (rect.left < margin) setShiftX(margin - rect.left);
    else if (rect.right > window.innerWidth - margin)
      setShiftX(window.innerWidth - margin - rect.right);
    else setShiftX(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reads refs + setters only
  }, [open]);
  // Closing from a menu ITEM or from Escape must return focus to the trigger.
  // The item is unmounted by the close, so focus would otherwise fall to <body>
  // and a keyboard user would lose their place in the card — and the form the item
  // reveals is the next tab stop after the trigger, so this is also the shortest
  // path to it. Closing from an outside click does NOT steal focus: the pointer
  // has already put it where the user aimed.
  const closeAndRestoreFocus = () => {
    setOpen(false);
    toggle.current?.focus();
  };
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeAndRestoreFocus();
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);
  const toggleOpen = () => {
    if (open) {
      setOpen(false);
      // Reset so the next open is measured from the default placement rather than
      // inheriting the previous menu's decision.
      setOpenUp(false);
      setShiftX(0);
      return;
    }
    setOpen(true);
  };
  return (
    <div className={className} ref={root}>
      <button
        type="button"
        ref={toggle}
        className="menu-toggle"
        aria-label={toggleLabel}
        aria-expanded={open}
        onClick={toggleOpen}
      >
        {toggleText}
      </button>
      {open && (
        <div
          ref={popover}
          className={`menu-popover ${popoverClassName}${openUp ? " menu-popover-up" : ""}`}
          style={shiftX ? { marginRight: -shiftX } : undefined}
        >
          {items(closeAndRestoreFocus)}
        </div>
      )}
    </div>
  );
}
