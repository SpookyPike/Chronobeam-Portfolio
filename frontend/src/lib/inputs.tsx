/* Deterministic European date/time fields.
 *
 * Native <input type="date|time|month|datetime-local"> renders in the
 * browser's UI locale — "08:00 AM" / "mm/dd/yyyy" on en-US Chromium, which
 * is wrong for a Europe/Berlin product and impossible to override reliably
 * in captures. These plain formatted text fields keep 24-hour entry and
 * ISO display consistent everywhere (precedent: the day-editor
 * TimeField). Values stay identical to the previous native fields
 * ("HH:MM", "YYYY-MM-DD", "YYYY-MM-DDTHH:MM", "YYYY-MM"). */

import type { FocusEvent, InputHTMLAttributes } from "react";

/** A masked field is prefilled to exactly its `maxLength`, so a click that lands
 *  the caret anywhere drops every keystroke: the value cannot be changed by
 *  clicking and typing, only by clearing it first. Selecting on focus makes
 *  "click and type" mean "replace", which is what a prefilled date field is
 *  expected to do — and what the browser's own `type="date"` does. Every masked
 *  field below shares that trait, so the behaviour is defined once here rather
 *  than rediscovered per page. */
function selectOnFocus(event: FocusEvent<HTMLInputElement>) {
  event.currentTarget.select();
}

/** Fold loose time input into progressive "H"/"HH"/"HH:MM" state. "8",
 *  "8:30", "0830" and "08:30" all arrive at "08:30"; mid-typing states such
 *  as "08:3" stay visible until the field is complete. */
export function normalizeTime(raw: string): string {
  if (/^\d{1,2}:\d{0,2}$/.test(raw)) {
    const [hours, minutes] = raw.split(":");
    return `${hours.padStart(2, "0")}:${minutes ?? ""}`;
  }
  const digits = raw.replace(/\D/g, "").slice(0, 4);
  if (digits.length <= 2) return digits;
  if (digits.length === 3 && Number(digits.slice(0, 2)) > 23)
    return `${digits[0]}:${digits.slice(1)}`;
  return `${digits.slice(0, 2)}:${digits.slice(2)}`;
}

/** "2026-7-2" → "2026-07-02"; keeps a typing-friendly ISO shape. */
export function normalizeDate(raw: string): string {
  const value = raw.trim();
  const match = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(value);
  if (!match) return value;
  return `${match[1]}-${match[2].padStart(2, "0")}-${match[3].padStart(2, "0")}`;
}

/** "2026-7" → "2026-07". */
export function normalizeMonth(raw: string): string {
  const value = raw.trim();
  const match = /^(\d{4})-(\d{1,2})$/.exec(value);
  if (!match) return value;
  return `${match[1]}-${match[2].padStart(2, "0")}`;
}

/** "2026-07-22" → "22.07.2026"; unparseable fragments stay as typed. */
export function isoToGermanDate(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  return match ? `${match[3]}.${match[2]}.${match[1]}` : value;
}

/** "HH:MM" with a real 24-hour clock reading. */
function isClockTime(value: string): boolean {
  return /^([01]\d|2[0-3]):[0-5]\d$/.test(value);
}

/** "22.07.2026" / "22.7.2026" / "2026-07-22" → "2026-07-22"; null when the
 *  text is still an incomplete fragment (kept raw for typing). */
export function toIsoDate(text: string): string | null {
  const trimmed = text.trim();
  const german = /^(\d{1,2})\.(\d{1,2})\.(\d{4})$/.exec(trimmed);
  if (german) return `${german[3]}-${german[2].padStart(2, "0")}-${german[1].padStart(2, "0")}`;
  return /^\d{4}-\d{2}-\d{2}$/.test(trimmed) ? trimmed : null;
}

/** "2026-07" → "07.2026" (German month convention, MM.YYYY). */
export function isoToGermanMonth(value: string): string {
  const match = /^(\d{4})-(\d{2})$/.exec(value);
  return match ? `${match[2]}.${match[1]}` : value;
}

/** "07.2026" / "7.2026" / "2026-07" → "2026-07"; null for fragments. */
export function toIsoMonth(text: string): string | null {
  const trimmed = text.trim();
  const german = /^(\d{1,2})\.(\d{4})$/.exec(trimmed);
  if (german) return `${german[2]}-${german[1].padStart(2, "0")}`;
  return /^\d{4}-\d{2}$/.test(trimmed) ? trimmed : null;
}

/** "2026-07-19T23:42" → "19.07.2026 23:42". */
export function isoToGermanDateTime(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
  return match ? `${match[3]}.${match[2]}.${match[1]} ${match[4]}:${match[5]}` : value;
}

/** "19.07.2026 23:42" / ISO → ISO; null for fragments. */
export function toIsoDateTime(text: string): string | null {
  const trimmed = text.trim();
  const german = /^(\d{1,2})\.(\d{1,2})\.(\d{4})[ T](\d{1,2}):(\d{2})$/.exec(trimmed);
  if (german) {
    return `${german[3]}-${german[2].padStart(2, "0")}-${german[1].padStart(2, "0")}T${german[4].padStart(2, "0")}:${german[5]}`;
  }
  return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(trimmed) ? trimmed : null;
}

type FieldProps = Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "value" | "onChange"> & {
  value: string;
  onChange: (value: string) => void;
};

export function TimeField({ value, onChange, ...rest }: FieldProps) {
  return (
    <input
      type="text"
      inputMode="numeric"
      autoComplete="off"
      maxLength={5}
      pattern="([01][0-9]|2[0-3]):[0-5][0-9]"
      placeholder="08:00"
      className={`time-field${rest.className ? ` ${rest.className}` : ""}`}
      value={value}
      onChange={(event) => onChange(normalizeTime(event.target.value))}
      onFocus={selectOnFocus}
      {...rest}
    />
  );
}

/** German date entry (dd.mm.yyyy). State stays ISO; both formats accepted;
 *  mid-typing fragments pass through. */
export function DateField({ value, onChange, ...rest }: FieldProps) {
  return (
    <input
      type="text"
      inputMode="numeric"
      autoComplete="off"
      maxLength={10}
      pattern="\d{2}\.\d{2}\.\d{4}"
      placeholder="05.12.2026"
      className={`date-field${rest.className ? ` ${rest.className}` : ""}`}
      value={isoToGermanDate(value)}
      onChange={(event) => onChange(toIsoDate(event.target.value) ?? event.target.value)}
      onFocus={selectOnFocus}
      {...rest}
    />
  );
}

/** German month entry (MM.YYYY). State stays ISO ("YYYY-MM"). */
export function MonthField({ value, onChange, ...rest }: FieldProps) {
  return (
    <input
      type="text"
      inputMode="numeric"
      autoComplete="off"
      maxLength={7}
      pattern="\d{2}\.\d{4}"
      placeholder="12.2026"
      className={`month-field${rest.className ? ` ${rest.className}` : ""}`}
      value={isoToGermanMonth(value)}
      onChange={(event) => onChange(toIsoMonth(event.target.value) ?? event.target.value)}
      onFocus={selectOnFocus}
      {...rest}
    />
  );
}

/** German date-time entry (dd.mm.yyyy HH:MM). State stays ISO
 *  ("YYYY-MM-DDTHH:MM"). */
export function DateTimeField({ value, onChange, ...rest }: FieldProps) {
  return (
    <input
      type="text"
      inputMode="numeric"
      autoComplete="off"
      maxLength={16}
      pattern="\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}"
      placeholder="05.12.2026 18:00"
      className={`datetime-field${rest.className ? ` ${rest.className}` : ""}`}
      value={isoToGermanDateTime(value)}
      onChange={(event) => onChange(toIsoDateTime(event.target.value) ?? event.target.value)}
      onFocus={selectOnFocus}
      {...rest}
    />
  );
}

/** The day after `date` (ISO in, ISO out). */
export function nextDate(date: string): string {
  const next = new Date(`${date}T00:00:00Z`);
  next.setUTCDate(next.getUTCDate() + 1);
  return next.toISOString().slice(0, 10);
}

/** Resolve a typed moment against the row's own day.
 *
 *  "07:00" is enough; a stated date always wins. A time strictly BEFORE
 *  `rollBefore` belongs to the NEXT day — that is what puts a night shift's
 *  post-midnight fact on the right date with no second control, and it resolves
 *  a break that straddles midnight into two different days correctly. Returns
 *  null for a fragment the employee is still typing (kept raw, like every other
 *  field here). */
export function resolveMoment(
  text: string,
  anchorDate: string,
  rollBefore?: string,
): string | null {
  const trimmed = text.trim();
  // Digits and colons only, and short: a TIME attempt, folded exactly like the
  // day editor's `TimeField` (so "8:30" and "0830" both land on 08:30). Anything
  // with a date separator is a datetime attempt instead.
  if (/^[\d:]{1,5}$/.test(trimmed)) {
    const time = normalizeTime(trimmed);
    if (!isClockTime(time)) return null;
    const date = rollBefore !== undefined && time < rollBefore ? nextDate(anchorDate) : anchorDate;
    return `${date}T${time}`;
  }
  return toIsoDateTime(trimmed);
}

/** "07:00" when the value sits on the day this field's own rule would choose for
 *  that time, else the full German datetime — so a date the employee STATED is
 *  always visible, and only a date the field DERIVED is implied. */
export function momentDisplay(value: string, anchorDate: string, rollBefore?: string): string {
  const iso = toIsoDateTime(value);
  if (iso === null) return value;
  const time = iso.slice(11, 16);
  const derived = resolveMoment(time, anchorDate, rollBefore)?.slice(0, 10) ?? anchorDate;
  return iso.slice(0, 10) === derived ? time : isoToGermanDateTime(iso);
}

/** A moment on the row's own day. Type "07:00"; state the whole datetime only
 *  when the fact falls on another date.
 *
 *  Deliberately ONE input and nothing beside it. A visible "resolved day" would
 *  have to sit inside the caller's `<label>`, and label text IS the field's
 *  accessible name — "Break start 31.07.2026" is not a name. The derived day is
 *  not hidden either: the card states the shift's own window directly above, and
 *  the recorded breaks are listed with their full datetimes the moment they are
 *  saved. What makes a bare time safe is the ROLL rule in ``resolveMoment``,
 *  which puts the in-shift cases on the right day without being asked. */
export function MomentField({
  value,
  onChange,
  anchorDate,
  rollBefore,
  ...rest
}: FieldProps & { anchorDate: string; rollBefore?: string }) {
  const text = momentDisplay(value, anchorDate, rollBefore);
  return (
    <input
      type="text"
      inputMode="numeric"
      autoComplete="off"
      maxLength={16}
      pattern="\d{2}:\d{2}|\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}"
      placeholder="07:00"
      className={`moment-field${rest.className ? ` ${rest.className}` : ""}`}
      value={text}
      onChange={(event) => {
        const raw = event.target.value;
        onChange(resolveMoment(raw, anchorDate, rollBefore) ?? raw);
      }}
      onFocus={selectOnFocus}
      {...rest}
    />
  );
}
