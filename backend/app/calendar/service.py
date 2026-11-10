"""The ONE calendar writer: publish an immutable, effective-dated edition.

Calendar classification is effective-dated institutional data (EINV-10). The
engine consumes classifications and never knows *why* a date is free, so adding
or changing a free day is a data publication, never a code change.

Model (EINV-21/EINV-22, and the overlap contract):

- **Insert-only.** A published edition's range and its day rows are never
  updated or deleted. Any change — including *un*-classifying a day — is a new
  edition. Narrowing a published ``valid_to`` is forbidden in particular: under
  fail-closed coverage it would punch a coverage gap and turn a local-looking
  edit into a global 409.
- **Overlap is precedence, never composition.** For a ``(calendar_key, day)``
  the resolving edition is the one with the greatest ``valid_from`` among those
  whose inclusive ``[valid_from, valid_to]`` covers the day;
  ``uq_calendar_version_effective_from`` makes that choice unique. A bounded
  *corrective overlay* therefore takes precedence inside its governed range and
  the predecessor resumes outside it.
- **A row that can never win is refused.** A day row at or after a later
  edition's ``valid_from`` is shadowed and would be a silent no-op.
- **The governed range** of a published edition is
  ``[valid_from, min(valid_to, next_valid_from - 1)]`` — verbatim the rule
  ``identity/service.py::_schedule_range_end`` already applies to the same
  ``(employee_id, valid_from)`` uniqueness.
- **Idempotence.** Re-publishing the same ``(calendar_key, version)`` with a
  byte-identical day set is a no-op success; any difference is refused naming
  the divergent days.

Concurrency (EINV-15): this is a *global* mutation of a calculation input, so
``lock_calendar_coordination`` is the FIRST statement and is held for the whole
publication. The publication takes **no** employee fact lock — its only
employee-row write is ``REVIEWED -> OPEN``, already serialized by the
coordination lock against the only competing transition, and convergent with
the only other writer of that column. Taking an employee lock here would put an
employee lock inside the global section and create the very inversion the
protocol exists to avoid.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit.models import record_audit
from app.calendar.models import CALENDAR_KEY, CalendarDay, CalendarVersion
from app.db import lock_calendar_coordination
from app.enums import CalendarDayClass

#: An overlay (an edition that starts inside an existing edition's range) must
#: be bounded, so a correction cannot accidentally blanket the future.
UNBOUNDED_OVERLAY_MESSAGE = (
    "a calendar edition that starts inside an existing edition's range must be bounded "
    "(--to is required): an open-ended overlay would silently supersede every later day"
)


class CalendarEditionRefused(ValueError):
    """A publication the loader refuses; the message names the offending data."""


@dataclass(frozen=True)
class CalendarEditionPreview:
    """What ``publish_calendar_edition`` would do. No locks, no writes."""

    calendar_key: str
    version: str
    source: str
    valid_from: date
    valid_to: date | None
    governed_to: date | None
    added_days: tuple[tuple[date, str], ...]
    removed_days: tuple[date, ...]
    months: tuple[date, ...]
    reviewed_periods: int
    no_op: bool


@dataclass(frozen=True)
class _Edition:
    """Minimal in-memory edition shape used by the pure resolution helpers."""

    id: uuid.UUID | None
    valid_from: date
    valid_to: date | None
    version: str
    source: str


def _existing_editions(session: Session, calendar_key: str) -> list[_Edition]:
    rows = session.scalars(
        select(CalendarVersion).where(CalendarVersion.calendar_key == calendar_key)
    ).all()
    return [_Edition(row.id, row.valid_from, row.valid_to, row.version, row.source) for row in rows]


def _governing(editions: list[_Edition], day: date) -> _Edition | None:
    """The resolving edition for one day: greatest ``valid_from`` covering it."""
    for edition in sorted(editions, key=lambda item: item.valid_from, reverse=True):
        if edition.valid_from <= day and (edition.valid_to is None or edition.valid_to >= day):
            return edition
    return None


def _exception_rows(session: Session, calendar_key: str) -> dict[tuple[uuid.UUID, date], str]:
    """Every exception row of the key, keyed by (edition id, day)."""
    rows = session.execute(
        select(CalendarDay.calendar_version_id, CalendarDay.day, CalendarDay.day_class)
        .join(CalendarVersion, CalendarVersion.id == CalendarDay.calendar_version_id)
        .where(CalendarVersion.calendar_key == calendar_key)
    ).all()
    return {(row[0], row[1]): row[2].value for row in rows}


def _resolved_class(
    editions: list[_Edition],
    exceptions: dict[tuple[uuid.UUID, date], str],
    day: date,
) -> str | None:
    """The exception class a calendar state declares for one day (None = ordinary).

    An edition enumerates *exceptions*: an ordinary working day is the declared
    default **within** an edition, and a day row belonging to any other edition
    is ignored for that day.
    """
    edition = _governing(editions, day)
    if edition is None or edition.id is None:
        return None
    return exceptions.get((edition.id, day))


def _governed_to(valid_from: date, valid_to: date | None, later: list[date]) -> date | None:
    """``[valid_from, min(valid_to, next_valid_from - 1)]`` (None = open-ended)."""
    candidates = [valid_to]
    if later:
        candidates.append(min(later) - timedelta(days=1))
    bounded = [value for value in candidates if value is not None]
    return min(bounded) if bounded else None


def _merged_coverage(editions: Sequence[CalendarVersion]) -> list[tuple[date, date | None]]:
    """The published editions' inclusive ranges merged into disjoint ascending spans.

    Coverage is the UNION of what was published: an edition covers every day from
    its ``valid_from`` to its inclusive ``valid_to`` (``None`` = open-ended), and
    the resolution rule above decides *which class* applies to a covered day —
    never *whether* the day is covered at all. Overlaps and adjacencies therefore
    collapse, and a corrective overlay only ever ADDS coverage.
    """
    merged: list[tuple[date, date | None]] = []
    for edition in sorted(editions, key=lambda row: row.valid_from):
        start, end = edition.valid_from, edition.valid_to
        if merged:
            span_start, span_end = merged[-1]
            if span_end is None:
                break  # already covered to the end of time; no later edition can add
            if start <= span_end + timedelta(days=1):
                merged[-1] = (span_start, end if end is None else max(span_end, end))
                continue
        merged.append((start, end))
    return merged


def first_uncovered_month(editions: Sequence[CalendarVersion], from_month: date) -> date | None:
    """First month at or after ``from_month`` with a day no edition covers.

    The operator-facing form of the fail-closed coverage rule
    (``policy/service.py::require_calendar_coverage``): the month view asks
    "is every day of this month covered?", so ONE uncovered day inside a month
    answers 409 ``CALENDAR_BLOCKED`` for that whole month. The returned month is
    therefore exactly the first month whose view starts failing, and ``None``
    means "no gap from here on" — which an open-ended edition legitimately
    produces. That is a statement about the published data, not a claim that
    coverage is required to be infinite.

    Bounded and deterministic: O(number of editions), no day walk and no
    configured horizon. A coverage span that lapsed in the past cannot make the
    CURRENT month look covered (``max`` keeps the cursor from moving backwards).
    """
    cursor = from_month.replace(day=1)
    for start, end in _merged_coverage(editions):
        if start > cursor:
            return cursor
        if end is None:
            return None
        cursor = max(cursor, (end + timedelta(days=1)).replace(day=1))
    return cursor


def _validate(
    *,
    calendar_key: str,
    version: str,
    source: str,
    valid_from: date,
    valid_to: date | None,
    days: Mapping[date, CalendarDayClass],
    existing: list[_Edition],
    exceptions: dict[tuple[uuid.UUID, date], str],
) -> None:
    for label, value in (("calendar_key", calendar_key), ("version", version), ("source", source)):
        if not value or not value.strip():
            raise CalendarEditionRefused(f"{label} must not be blank")
    if valid_to is not None and valid_to < valid_from:
        raise CalendarEditionRefused(
            f"valid_to ({valid_to.isoformat()}) is before valid_from ({valid_from.isoformat()})"
        )
    outside = sorted(
        day for day in days if day < valid_from or (valid_to is not None and day > valid_to)
    )
    if outside:
        raise CalendarEditionRefused(
            "calendar day rows fall outside the edition's own range "
            f"[{valid_from.isoformat()}, {valid_to.isoformat() if valid_to else 'open'}]: "
            + ", ".join(day.isoformat() for day in outside[:10])
        )
    if any(edition.valid_from >= valid_from for edition in existing) and valid_to is None:
        raise CalendarEditionRefused(UNBOUNDED_OVERLAY_MESSAGE)
    shadowing: list[str] = []
    for day in sorted(days):
        winner = _governing([e for e in existing if e.valid_from > valid_from], day)
        if winner is not None:
            shadowing.append(f"{day.isoformat()} (shadowed by {winner.version!r})")
    if shadowing:
        raise CalendarEditionRefused(
            "a calendar day row that can never win is refused: " + ", ".join(shadowing[:10])
        )


def _plan(
    *,
    calendar_key: str,
    version: str,
    source: str,
    valid_from: date,
    valid_to: date | None,
    days: Mapping[date, CalendarDayClass],
    existing: list[_Edition],
    exceptions: dict[tuple[uuid.UUID, date], str],
) -> tuple[date | None, tuple[date, ...], tuple[date, ...]]:
    """Return ``(governed_to, affected_days, removed_days)`` for the publication.

    The affected set is exact, not an over-approximation: it is every day inside
    the new edition's governed range whose **resolved class actually changes**.
    A day that was an exception and remains one reopens nothing.
    """
    governed_to = _governed_to(
        valid_from, valid_to, [e.valid_from for e in existing if e.valid_from > valid_from]
    )
    governed_end = governed_to if governed_to is not None else date.max

    removed: set[date] = set()
    for (edition_id, day), _day_class in exceptions.items():
        if day < valid_from or day > governed_end:
            continue
        owner = next((e for e in existing if e.id == edition_id), None)
        if owner is None or owner.valid_from >= valid_from:
            # A row of a later/equal edition is not this edition's to erase.
            continue
        if day not in days:
            removed.add(day)

    new_edition = _Edition(None, valid_from, valid_to, version, source)
    after_editions = [*existing, new_edition]
    affected: set[date] = set()
    for day in set(days) | removed:
        before = _resolved_class(existing, exceptions, day)
        after = days[day].value if day in days else _resolved_class(after_editions, exceptions, day)
        if before != after:
            affected.add(day)
    return governed_to, tuple(sorted(affected)), tuple(sorted(removed))


def _months_of(days: tuple[date, ...]) -> tuple[date, ...]:
    return tuple(sorted({day.replace(day=1) for day in days}))


def _reviewed_period_count(session: Session, months: tuple[date, ...]) -> int:
    if not months:
        return 0
    from app.enums import PeriodStatus
    from app.worktime.models import WorkPeriod

    return int(
        session.scalar(
            select(func.count())
            .select_from(WorkPeriod)
            .where(
                WorkPeriod.period_month.in_(months),
                WorkPeriod.status == PeriodStatus.REVIEWED,
            )
        )
        or 0
    )


def _existing_same_label(
    session: Session, calendar_key: str, version: str
) -> CalendarVersion | None:
    return session.scalar(
        select(CalendarVersion).where(
            CalendarVersion.calendar_key == calendar_key, CalendarVersion.version == version
        )
    )


def preview_calendar_edition(
    session: Session,
    *,
    calendar_key: str = CALENDAR_KEY,
    version: str,
    valid_from: date,
    valid_to: date | None,
    source: str,
    days: Mapping[date, CalendarDayClass],
) -> CalendarEditionPreview:
    """Validate and describe a publication without writing anything.

    Deliberately takes **no** lock: a read-only preview must neither block nor be
    blocked by a review. It is a best-effort snapshot — the real run re-validates
    everything under the coordination lock.
    """
    existing = _existing_editions(session, calendar_key)
    exceptions = _exception_rows(session, calendar_key)
    same_label = _existing_same_label(session, calendar_key, version)
    if same_label is not None:
        current = {
            day: day_class
            for (edition_id, day), day_class in exceptions.items()
            if edition_id == same_label.id
        }
        requested = {day: day_class.value for day, day_class in days.items()}
        divergent = sorted(set(current) ^ set(requested)) + sorted(
            day for day in set(current) & set(requested) if current[day] != requested[day]
        )
        if divergent:
            raise CalendarEditionRefused(
                f"edition {version!r} already exists with a different day set; "
                "refusing to rewrite a published edition. Divergent days: "
                + ", ".join(day.isoformat() for day in sorted(set(divergent))[:10])
            )
        if (same_label.valid_from, same_label.valid_to) != (valid_from, valid_to):
            # Narrowing (or otherwise re-pointing) a published range is how a
            # coverage gap would be punched by a local-looking edit. There is no
            # update path, so the only way to express a change is a new edition.
            raise CalendarEditionRefused(
                f"edition {version!r} already exists with range "
                f"[{same_label.valid_from.isoformat()}, "
                f"{same_label.valid_to.isoformat() if same_label.valid_to else 'open'}]; "
                "a published edition's range is immutable — publish a new edition instead"
            )
        return CalendarEditionPreview(
            calendar_key=calendar_key,
            version=version,
            source=same_label.source,
            valid_from=same_label.valid_from,
            valid_to=same_label.valid_to,
            governed_to=_governed_to(
                same_label.valid_from,
                same_label.valid_to,
                [e.valid_from for e in existing if e.valid_from > same_label.valid_from],
            ),
            added_days=(),
            removed_days=(),
            months=(),
            reviewed_periods=0,
            no_op=True,
        )
    _validate(
        calendar_key=calendar_key,
        version=version,
        source=source,
        valid_from=valid_from,
        valid_to=valid_to,
        days=days,
        existing=existing,
        exceptions=exceptions,
    )
    governed_to, affected, removed = _plan(
        calendar_key=calendar_key,
        version=version,
        source=source,
        valid_from=valid_from,
        valid_to=valid_to,
        days=days,
        existing=existing,
        exceptions=exceptions,
    )
    months = _months_of(affected)
    return CalendarEditionPreview(
        calendar_key=calendar_key,
        version=version,
        source=source,
        valid_from=valid_from,
        valid_to=valid_to,
        governed_to=governed_to,
        added_days=tuple(sorted((day, day_class.value) for day, day_class in days.items())),
        removed_days=removed,
        months=months,
        reviewed_periods=_reviewed_period_count(session, months),
        no_op=False,
    )


def publish_calendar_edition(
    session: Session,
    *,
    calendar_key: str = CALENDAR_KEY,
    version: str,
    valid_from: date,
    valid_to: date | None,
    source: str,
    days: Mapping[date, CalendarDayClass],
    actor_principal: str,
) -> CalendarVersion:
    """Publish one immutable calendar edition and reopen what it changes.

    Returns the (existing or newly inserted) edition. Callers: ``app.calendar_admin``,
    ``dev_seed`` and ``demo_seed`` — all through this one writer, so they inherit
    the coordination lock and the reopen rather than taking their own advisory key.
    """
    # First statement, before anything is read: the validation below and the
    # write it guards are both inside the coordination section (EINV-15).
    lock_calendar_coordination(session, calendar_key)

    preview = preview_calendar_edition(
        session,
        calendar_key=calendar_key,
        version=version,
        valid_from=valid_from,
        valid_to=valid_to,
        source=source,
        days=days,
    )
    if preview.no_op:
        existing = _existing_same_label(session, calendar_key, version)
        if existing is None:  # pragma: no cover - preview.no_op implies it exists
            raise RuntimeError("calendar edition vanished between preview and publish")
        return existing

    edition = CalendarVersion(
        calendar_key=calendar_key,
        version=version,
        valid_from=valid_from,
        valid_to=valid_to,
        source=source.strip(),
    )
    session.add(edition)
    session.flush()
    for day in sorted(days):
        session.add(CalendarDay(calendar_version_id=edition.id, day=day, day_class=days[day]))
    session.flush()

    from app.worktime.service import reopen_reviewed_months_in_publication

    reopen_reviewed_months_in_publication(session, preview.months)
    record_audit(
        session,
        actor_principal=actor_principal,
        action="CALENDAR_VERSION_PUBLISH",
        outcome="SUCCESS",
        object_type="CALENDAR_VERSION",
        object_id=edition.id,
    )
    return edition
