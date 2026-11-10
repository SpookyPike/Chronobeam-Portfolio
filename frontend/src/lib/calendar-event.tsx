/* Calendar event bars — the shared visual language for "what is on this day".
 *
 * Extracted so the month grid, the day panel's Planned assignments section and
 * any future calendar or group-planning surface read one component and one token
 * set instead of each inventing its own chip. The category accent comes from
 * `data-cal-kind`, and every tint, line and phone fill is derived from it in
 * `index.css` with `color-mix`, so adding a category is one token plus one row —
 * never a second copy of the presentation rules.
 *
 * ACCESSIBILITY CONTRACT (WCAG 2.2). The bars are PRESENTATIONAL and carry
 * `aria-hidden`: a date is already a single `<button>` whose `aria-label`
 * REPLACES its children for assistive technology. A name on each bar would
 * therefore be unreachable, and leaving the visible labels exposed would announce
 * the same event twice. So the day button's own accessible name is the single
 * carrier of every event phrase — including the events the overflow chip hides —
 * and that is what keeps one date exactly one keyboard stop. Any caller that
 * renders these bars MUST therefore put each event's `spoken` phrase into the
 * surrounding control's accessible name.
 *
 * Category meaning is never carried by colour alone at either density: the
 * desktop bar shows the category word, the phone bar shows a two-letter glyph
 * taken from that same word, and both are backed by the spoken phrase. */

import { calendarEventLabels, type CalendarEventKind } from "./labels";

export type CalendarEvent = {
  kind: CalendarEventKind;
  /** Overrides the category's own visible label. Used by RETAINED HISTORY, which
   *  needs a state word the category map cannot carry: a cancelled assignment is
   *  still a Shift by category, but calling its bar "Shift" would present it as a
   *  plan the employee holds. */
  label?: string;
  /** Trailing detail a narrow cell may drop — a start time, or a half-day
   *  extent. The category word is the meaning and always survives; this is what
   *  truncates first, because a truncated time is recoverable from the day panel
   *  while a truncated category word is not. */
  detail?: string;
  /** The full phrase the surrounding control announces for this event. */
  spoken: string;
  /** `cancelled` is RETAINED HISTORY (a shift the employee no longer holds),
   *  rendered muted with a dashed stripe so it can never read as a live plan. */
  state?: "active" | "cancelled";
};

/** The bar stack for one day. `max` is the number of bars a cell can carry before
 *  the overflow chip takes over; the default of two is what fits a two-column
 *  desktop cell (about 80px wide) alongside the date number, the delta and the
 *  finding without the row growing taller than its neighbours. */
export function CalendarEventBars({ events, max = 2 }: { events: CalendarEvent[]; max?: number }) {
  if (events.length === 0) return null;
  const shown = events.slice(0, max);
  const hidden = events.length - shown.length;
  return (
    <span className="cal-events">
      {shown.map((event, index) => (
        <CalendarEventBar key={`${event.kind}-${event.state ?? "active"}-${index}`} event={event} />
      ))}
      {/* A count, not a category: it takes the muted text colour so it never
       *  competes with the bars for hue. The hidden events are still spoken,
       *  because the caller's accessible name lists every event. */}
      {hidden > 0 && <span className="cal-events-more">{`+${hidden} more`}</span>}
    </span>
  );
}

function CalendarEventBar({ event }: { event: CalendarEvent }) {
  const { label: categoryLabel, glyph } = calendarEventLabels[event.kind];
  return (
    <span
      className="cal-bar"
      data-cal-kind={event.kind}
      data-cal-state={event.state ?? "active"}
      aria-hidden="true"
    >
      <span className="cal-bar-glyph">{glyph}</span>
      <span className="cal-bar-label">{event.label ?? categoryLabel}</span>
      {event.detail && <span className="cal-bar-detail">{event.detail}</span>}
    </span>
  );
}
