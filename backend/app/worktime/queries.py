"""Current-version predicate and range queries.

A superseding row chain is stored with ``supersedes_id``; the CURRENT version
of a logical interval is the row no other row supersedes. This predicate is
the single source of truth for reads, the overlap check, and PATCH/DELETE
targeting — defined here so all call sites agree.
"""

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from uuid import UUID

from app.worktime.models import BreakInterval, WorkInterval
from app.worktime.walltime import BERLIN, local_date
from sqlalchemy import exists, select
from sqlalchemy.orm import Session, aliased


def current_intervals(
    session: Session,
    *,
    employee_id: UUID,
    intersects_from: datetime | None = None,
    intersects_to: datetime | None = None,
    linked_shift_assignment_id: UUID | None = None,
) -> Sequence[WorkInterval]:
    """Current (non-superseded) intervals of one employee, optional window.

    Window semantics: ``start_at < intersects_to AND end_at > intersects_from``
    (instant intersection — M5). Ordering by start_at makes day/month views
    deterministic.
    """
    child = aliased(WorkInterval)
    query = select(WorkInterval).where(
        WorkInterval.employee_id == employee_id,
        ~exists(select(child.id).where(child.supersedes_id == WorkInterval.id)),
    )
    if intersects_from is not None:
        query = query.where(WorkInterval.end_at > intersects_from)
    if intersects_to is not None:
        query = query.where(WorkInterval.start_at < intersects_to)
    if linked_shift_assignment_id is not None:
        query = query.where(WorkInterval.linked_shift_assignment_id == linked_shift_assignment_id)
    return session.scalars(query.order_by(WorkInterval.start_at)).all()


def current_breaks(
    session: Session,
    *,
    employee_id: UUID,
    intersects_from: datetime | None = None,
    intersects_to: datetime | None = None,
    linked_shift_assignment_id: UUID | None = None,
) -> Sequence[BreakInterval]:
    """Current (non-superseded) explicit breaks of one employee, optional window.

    Same current-version predicate and window semantics as
    ``current_intervals``: a break is a versioned factual row and the two
    readers must agree on what "current" means.
    """
    child = aliased(BreakInterval)
    query = select(BreakInterval).where(
        BreakInterval.employee_id == employee_id,
        ~exists(select(child.id).where(child.supersedes_id == BreakInterval.id)),
    )
    if intersects_from is not None:
        query = query.where(BreakInterval.end_at > intersects_from)
    if intersects_to is not None:
        query = query.where(BreakInterval.start_at < intersects_to)
    if linked_shift_assignment_id is not None:
        query = query.where(BreakInterval.linked_shift_assignment_id == linked_shift_assignment_id)
    return session.scalars(query.order_by(BreakInterval.start_at)).all()


def month_bounds(month: date) -> tuple[datetime, datetime]:
    """Berlin instants for [first midnight of month, first midnight of next)."""
    start = datetime(month.year, month.month, 1, tzinfo=BERLIN)
    if month.month == 12:
        end = datetime(month.year + 1, 1, 1, tzinfo=BERLIN)
    else:
        end = datetime(month.year, month.month + 1, 1, tzinfo=BERLIN)
    return start, end


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=BERLIN)
    return start, start + timedelta(days=1)


def local_dates(start_at: datetime, end_at: datetime) -> list[date]:
    """Berlin calendar dates an instant span touches (cross-midnight spans 2+).

    The single definition of the half-open local-day rule, usable before an
    interval row exists (a correction must reopen the months of the NEW span as
    well as the old one). An interval crossing midnight is one canonical fact
    (INV-02); it is shown in the day view of every local date it intersects
    (DATA-007/008).

    Days are HALF-OPEN [midnight, next midnight): a span ending exactly at
    local midnight touches no instant of the end date, so that date is not
    spanned (month-summary buckets must agree with day detail).
    """
    first = local_date(start_at)
    last = local_date(end_at)
    if end_at == datetime.combine(last, time.min, tzinfo=BERLIN):
        last -= timedelta(days=1)
    dates: list[date] = []
    current = first
    while current <= last:
        dates.append(current)
        current += timedelta(days=1)
    return dates


def spanned_local_dates(interval: WorkInterval) -> list[date]:
    """The local dates of a stored interval (see ``local_dates``)."""
    return local_dates(interval.start_at, interval.end_at)
