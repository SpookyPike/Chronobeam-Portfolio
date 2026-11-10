"""The Schleswig-Holstein statutory public holidays, derived from the statute.

``EINV-10`` makes calendar classification effective-dated institutional *data*:
``app.calendar.service.publish_calendar_edition`` is the only writer and
``app.calendar_admin`` is the only production entry point. This module exists
because the *statutory* half of that day set is not an institutional choice — it
is fixed by public law (``SFTG``, Gesetz über Sonn- und Feiertage des Landes
Schleswig-Holstein of 28 June 2004, §2(1); the Reformationstag entry was added by
the 2018 amendment). Deriving it from the statute is provenance, not a guess, and
it takes the hand-made CSV off the critical path for the one day set that has a
public, checkable source.

What this module deliberately does NOT do:

- **It never emits ``EMPLOYER_FREE``.** Employer-free days — a recurring winter
  closure, for example — are an employer decision, not a statutory one. They stay
  operator-supplied and are composed with this day set at publication time, where
  ``--source`` names both halves.
- **It adds no second calendar mechanism.** It computes a day set and hands it to
  the ONE existing writer, inheriting the coordination lock, the insert-only
  contract, the idempotence check and the review reopen.
- **It does not list ``Ostersonntag``/``Pfingstsonntag``.** §2(1) does not: those
  two are protected as *Sundays* by §1, and the engine's own rule
  (``WT-FLEX-001``) is defined against the schedule, which already gives every
  Sunday a zero requirement. The ten days below are the complete statutory list.
"""

from datetime import date, timedelta

from app.enums import CalendarDayClass

#: ``SFTG`` §2(1) — the six statutory holidays fixed to a calendar date.
FIXED_STATUTORY_HOLIDAYS: tuple[tuple[int, int], ...] = (
    (1, 1),  # Neujahrstag
    (5, 1),  # 1. Mai
    (10, 3),  # Tag der Deutschen Einheit
    (10, 31),  # Reformationstag (statutory in SH since the 2018 amendment)
    (12, 25),  # 1. Weihnachtstag
    (12, 26),  # 2. Weihnachtstag
)

#: ``SFTG`` §2(1) — the four Easter-derived holidays, as offsets from Easter Sunday.
EASTER_STATUTORY_OFFSETS: tuple[int, ...] = (
    -2,  # Karfreitag
    1,  # Ostermontag
    39,  # Himmelfahrtstag
    50,  # Pfingstmontag
)

#: The demo horizon: the range the
#: statutory set is published for, starting on 1 January so that *every* month of
#: every year in it is classified. The demo database runs on exactly this range
#: (``app.demo_seed``), which is what makes an October demo month show the real
#: 3 October and Reformationstag holidays rather than ordinary working days.
SH_HORIZON_FROM = date(2026, 1, 1)
SH_HORIZON_TO = date(2035, 12, 31)

#: Where the *dev/test* seed starts its statutory overlay, which is deliberately
#: NOT ``SH_HORIZON_FROM``. The pytest fixture already owns 2026-01-01 with its
#: open-ended, exception-free coverage edition and
#: ``uq_calendar_version_effective_from`` permits one edition per effective date, so
#: the dev path publishes a bounded *overlay* from 1 October instead of replacing
#: it — leaving 2026-01-01..2026-09-30 as the coverage edition's ordinary days. The
#: demo database has no such constraint (``just demo-reset`` gives it a fresh
#: schema), so it publishes the full horizon from ``SH_HORIZON_FROM``.
DEV_OVERLAY_FROM = date(2026, 10, 1)


def easter_sunday(year: int) -> date:
    """Gregorian Easter Sunday — the Meeus/Jones/Butcher anonymous computus."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day + 1)


def statutory_holidays(from_year: int, to_year: int) -> dict[date, CalendarDayClass]:
    """Every ``SFTG`` §2(1) statutory holiday in the inclusive year range."""
    if to_year < from_year:
        raise ValueError(f"to_year ({to_year}) is before from_year ({from_year})")
    days: dict[date, CalendarDayClass] = {}
    for year in range(from_year, to_year + 1):
        for month, day in FIXED_STATUTORY_HOLIDAYS:
            days[date(year, month, day)] = CalendarDayClass.PUBLIC_HOLIDAY
        easter = easter_sunday(year)
        for offset in EASTER_STATUTORY_OFFSETS:
            days[easter + timedelta(days=offset)] = CalendarDayClass.PUBLIC_HOLIDAY
    return days


def statutory_days(valid_from: date, valid_to: date) -> dict[date, CalendarDayClass]:
    """The derived day set restricted to ``[valid_from, valid_to]``.

    The writer refuses a day row outside its own edition's range, so the
    restriction lives here rather than being repeated at each call site.
    """
    return {
        day: day_class
        for day, day_class in statutory_holidays(valid_from.year, valid_to.year).items()
        if valid_from <= day <= valid_to
    }


def statutory_version(valid_from: date, valid_to: date) -> str:
    """The edition label an operator recognises for this range."""
    return f"sh-statutory-{valid_from.year}-{valid_to.year}-v1"


def statutory_source(valid_from: date, valid_to: date) -> str:
    """Provenance for the edition, naming the statute the day set was derived from."""
    return (
        f"SH_STATUTORY_{valid_from.year}-{valid_to.year} (SFTG SH 2004 §2(1)); "
        "employer-free days not included"
    )
