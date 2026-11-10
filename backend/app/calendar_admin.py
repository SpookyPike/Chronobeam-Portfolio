"""Operator CLI for publishing an institutional calendar edition.

Calendar classification is effective-dated institutional data (EINV-10). There is
deliberately no calendar-admin UI and no HTTP endpoint: publishing an edition is
an operator action, exactly like ``app.admin_bootstrap`` and
``app.identity_admin``.

The CLI is a thin argument/CSV parser over the ONE writer
(``app.calendar.service.publish_calendar_edition``): it takes no advisory lock of
its own and implements no validation of its own, so the coordination protocol and
the EINV-21/EINV-22 rules have exactly one implementation.

Usage::

    python -m app.calendar_admin publish --statutory-sh \\
        --from 2026-10-01 --to 2035-12-31 --dry-run
    python -m app.calendar_admin publish --file days.csv --version sh-2026-v1 \\
        --from 2026-01-01 --to 2026-12-31 \\
        --source "SH_STATUTORY_2026 + EMPLOYER_FREE_2026"

The day set comes from exactly one of two sources:

- ``--statutory-sh``: the Schleswig-Holstein statutory holidays, *derived* from
  ``SFTG`` §2(1) by ``app.calendar.holidays`` for the years the edition covers.
  This is the recommended default for the statutory half, because the statute is
  public and checkable, so no hand-made CSV sits between the law and the data.
- ``--file``: an operator-supplied CSV with a header row and the columns
  ``day,day_class`` where ``day`` is ``YYYY-MM-DD`` and ``day_class`` is
  ``PUBLIC_HOLIDAY`` or ``EMPLOYER_FREE``. A date with no row is an ordinary
  working day *inside* the edition's governed range. Still the path for
  employer-free days and for a correction, and then ``--source`` stays mandatory
  because nothing else can name where the day set came from.

No date is embedded in code either way, and no recurring employer-free day (the
Christmas closure in particular) is guessed.
"""

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from app.calendar.holidays import statutory_days, statutory_source, statutory_version
from app.calendar.models import CALENDAR_KEY
from app.calendar.service import (
    CalendarEditionRefused,
    preview_calendar_edition,
    publish_calendar_edition,
)
from app.config import get_settings
from app.db import get_session_factory
from app.enums import CalendarDayClass

ACTOR_PRINCIPAL = "CALENDAR_CLI"

STATUTORY_NEEDS_END = (
    "--statutory-sh requires --to: the derived day set is published as a bounded edition, "
    "so it can overlay an edition that already covers the earlier range"
)


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"not a real YYYY-MM-DD date: {value!r}") from exc


def read_day_file(path: Path) -> dict[date, CalendarDayClass]:
    """Parse the day file; refuses a duplicate day or an unknown class."""
    days: dict[date, CalendarDayClass] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or {"day", "day_class"} - set(reader.fieldnames):
            raise ValueError("the day file needs a header row with columns day,day_class")
        for number, row in enumerate(reader, start=2):
            raw_day = (row.get("day") or "").strip()
            raw_class = (row.get("day_class") or "").strip()
            if not raw_day:
                continue
            try:
                day = _parse_day(raw_day)
            except ValueError as exc:
                raise ValueError(f"row {number}: {exc}") from None
            try:
                day_class = CalendarDayClass(raw_class)
            except ValueError:
                raise ValueError(
                    f"row {number}: day_class must be one of "
                    + ", ".join(member.value for member in CalendarDayClass)
                ) from None
            if day in days:
                raise ValueError(f"row {number}: duplicate day {day.isoformat()}")
            days[day] = day_class
    return days


def publish(
    session: Session,
    *,
    path: Path | None,
    statutory_sh: bool,
    version: str | None,
    valid_from: date,
    valid_to: date | None,
    source: str | None,
    dry_run: bool,
) -> int:
    """Publish one edition from either day-set source, through the ONE writer.

    ``--statutory-sh`` derives the day set and its provenance from ``SFTG`` §2(1),
    so both are optional there and an explicit ``--source`` *replaces* the derived
    string (e.g. to name the employer-free half as well). ``--file`` names neither,
    so both stay mandatory.
    """
    if statutory_sh:
        if valid_to is None:
            raise ValueError(STATUTORY_NEEDS_END)
        days = statutory_days(valid_from, valid_to)
        resolved_version = version or statutory_version(valid_from, valid_to)
        resolved_source = source or statutory_source(valid_from, valid_to)
    else:
        if path is None or version is None or source is None:
            raise ValueError("--file requires --version and --source")
        days = read_day_file(path)
        resolved_version = version
        resolved_source = source
    preview = preview_calendar_edition(
        session,
        calendar_key=CALENDAR_KEY,
        version=resolved_version,
        valid_from=valid_from,
        valid_to=valid_to,
        source=resolved_source,
        days=days,
    )
    if preview.no_op:
        print(
            f"unchanged: edition {resolved_version!r} already exists with an identical day set "
            f"(nothing written, nothing reopened)"
        )
        return 0
    governed = preview.governed_to.isoformat() if preview.governed_to else "open-ended"
    print(
        f"edition {resolved_version!r} for {CALENDAR_KEY!r}: valid "
        f"[{valid_from.isoformat()}, {valid_to.isoformat() if valid_to else 'open'}] "
        f"governed to {governed}; source={resolved_source!r}"
    )
    print(f"  {len(preview.added_days)} exception day(s) added")
    if preview.removed_days:
        print(
            "  classification REMOVED on "
            + ", ".join(day.isoformat() for day in preview.removed_days[:20])
        )
    print(
        f"  months reopened: {', '.join(f'{month:%Y-%m}' for month in preview.months) or 'none'}"
        f" ({preview.reviewed_periods} REVIEWED period(s) to reopen)"
    )
    if dry_run:
        print("dry-run: nothing written")
        return 0
    publish_calendar_edition(
        session,
        calendar_key=CALENDAR_KEY,
        version=resolved_version,
        valid_from=valid_from,
        valid_to=valid_to,
        source=resolved_source,
        days=days,
        actor_principal=ACTOR_PRINCIPAL,
    )
    session.commit()
    print(f"published edition {resolved_version!r} (env={get_settings().env})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Publish an institutional calendar edition (operator CLI only).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("publish", help="Publish one immutable edition")
    command.add_argument("--file", default=None, help="CSV with day,day_class")
    command.add_argument(
        "--statutory-sh",
        action="store_true",
        help="Derive the SFTG SH §2(1) statutory day set for the edition's years instead of --file",
    )
    command.add_argument(
        "--version", default=None, help="Institutional edition label (required with --file)"
    )
    command.add_argument("--from", dest="valid_from", required=True, help="YYYY-MM-DD")
    command.add_argument("--to", dest="valid_to", default=None, help="YYYY-MM-DD (inclusive)")
    command.add_argument(
        "--source",
        default=None,
        help=(
            "Where the day set came from, recorded as provenance (required with --file; "
            "with --statutory-sh it defaults to the derived statutory citation)"
        ),
    )
    command.add_argument(
        "--dry-run", action="store_true", help="Validate and report, write nothing"
    )
    args = parser.parse_args(argv)

    if bool(args.file) == bool(args.statutory_sh):
        parser.error("choose exactly one day-set source: --file or --statutory-sh")
    version = (args.version or "").strip() or None
    source = (args.source or "").strip() or None
    path = Path(args.file) if args.file else None
    if args.statutory_sh:
        # A derived day set is published bounded, so it can overlay an edition
        # that already governs the earlier range.
        if args.valid_to is None:
            parser.error(STATUTORY_NEEDS_END)
    else:
        if version is None:
            parser.error("--version is required with --file")
        if source is None:
            parser.error(
                "--source is required with --file: an edition must name its day-set source"
            )
        if path is None or not path.is_file():
            parser.error(f"no such day file: {path}")

    try:
        valid_from = _parse_day(args.valid_from)
        valid_to = _parse_day(args.valid_to) if args.valid_to else None
    except ValueError as exc:
        parser.error(str(exc))

    with get_session_factory()() as session:
        try:
            return publish(
                session,
                path=path,
                statutory_sh=args.statutory_sh,
                version=version,
                valid_from=valid_from,
                valid_to=valid_to,
                source=source,
                dry_run=args.dry_run,
            )
        except (CalendarEditionRefused, ValueError) as exc:
            session.rollback()
            print(f"publish refused: {exc}", file=sys.stderr)
            return 2


if __name__ == "__main__":
    sys.exit(main())
