"""Two-session travel-mutation serialization regressions.

Every travel mutation is a read-then-write (currentness check, idempotent
client_request_id replay, passive-overlap scan) followed by an insert.  Two
concurrent requests could both pass the read and then both write: two current
chain heads, or overlapping passive segments.  The fix serializes all travel
mutations per employee with a transaction-scoped PostgreSQL advisory lock, so
these tests are genuinely two-session races against the real database.

Unlike the fixture-transaction tests, these tests COMMIT their own rows (the
advisory lock is released only at transaction end), so they use 2031 dates
and remove their rows in a finally block.
"""

import threading
import uuid
from collections.abc import Callable
from datetime import datetime

import pytest
from app.identity.models import Employee
from app.travel.models import (
    BusinessTrip,
    TravelSegment,
    TravelSegmentClassification,
    TripStartOrigin,
)
from app.travel.schemas import SegmentIn, SegmentPatch, TripIn
from app.travel.service import create_segment, create_trip, patch_segment
from app.worktime.models import Correction
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

TRIP_START = "2031-06-01T06:00:00"
TRIP_END = "2031-06-10T22:00:00"


@pytest.fixture()
def committed_employee(db_engine: Engine) -> Employee:
    """Resolve the seeded synthetic employee on a committed connection."""
    with Session(db_engine) as session:
        employee = session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    return employee


def _start_race(
    engine: Engine, barrier: threading.Barrier, fn: Callable[[Session], object]
) -> tuple[threading.Thread, list[tuple[str, object]]]:
    """Start ``fn(session)`` in its own committed thread racing a sibling.

    The barrier sits INSIDE the worker thread so both sessions enter the
    service call simultaneously; outcomes record (result, error-code-or-None).
    The return of (thread, outcomes) lets the caller start BOTH racers before
    joining either — a join before the second start would deadlock the barrier.
    """
    outcomes: list[tuple[str, object]] = []

    def _target() -> None:
        with Session(engine) as session:
            barrier.wait()
            try:
                result = fn(session)
                session.commit()
                outcomes.append(("ok", result))
            except HTTPException as exc:
                session.rollback()
                detail = exc.detail
                code = detail.get("code") if isinstance(detail, dict) else str(detail)
                outcomes.append(("error", code))
            except Exception as exc:  # pragma: no cover - unexpected failure
                session.rollback()
                outcomes.append(("error", f"{type(exc).__name__}: {exc}"))

    thread = threading.Thread(target=_target)
    thread.start()
    return thread, outcomes


def _seed_trip(engine: Engine, employee: Employee) -> uuid.UUID:
    with Session(engine) as session, session.begin():
        trip, _ = create_trip(
            session,
            employee,
            TripIn(
                start_local=datetime.fromisoformat(TRIP_START),
                end_local=datetime.fromisoformat(TRIP_END),
                start_origin=TripStartOrigin.WORKPLACE,
                reference_label=None,
                client_request_id=uuid.uuid4(),
            ),
        )
        trip_id = trip.id
    return trip_id


def _cleanup_trip(engine: Engine, trip_id: uuid.UUID) -> None:
    """Remove the race's committed rows completely.

    The race transactions commit (the advisory lock is released only at
    transaction end), so Corrections written by the winning mutation must be
    removed too — later tests assert on the whole Correction table.
    """
    with Session(engine) as session, session.begin():
        segment_ids = session.scalars(
            select(TravelSegment.id).where(TravelSegment.trip_id == trip_id)
        ).all()
        session.execute(
            delete(Correction).where(
                Correction.entity_type.in_(["BUSINESS_TRIP", "TRAVEL_SEGMENT"]),
                Correction.entity_id.in_([trip_id, *segment_ids]),
            )
        )
        session.execute(delete(TravelSegment).where(TravelSegment.trip_id == trip_id))
        session.execute(delete(BusinessTrip).where(BusinessTrip.id == trip_id))


def _passive(
    session: Session, employee: Employee, trip_id: uuid.UUID, start: str, end: str
) -> uuid.UUID:
    segment, _ = create_segment(
        session,
        employee,
        trip_id,
        SegmentIn(
            start_local=datetime.fromisoformat(start),
            end_local=datetime.fromisoformat(end),
            classification=TravelSegmentClassification.PASSIVE_TRAVEL,
        ),
    )
    return segment.id


def test_concurrent_passive_segment_creates_serialize(
    db_engine: Engine, committed_employee: Employee
) -> None:
    """Two sessions creating OVERLAPPING passive segments: exactly one wins.

    Without the advisory lock both reads can pass the overlap scan and both
    rows commit.  With it the loser waits for the winner's commit and is then
    rejected by VALIDATION_TRAVEL_OVERLAP.
    """
    trip_id = _seed_trip(db_engine, committed_employee)
    barrier = threading.Barrier(2)
    try:
        first_thread, first = _start_race(
            db_engine,
            barrier,
            lambda session: _passive(
                session, committed_employee, trip_id, "2031-06-02T08:00:00", "2031-06-02T10:00:00"
            ),
        )
        second_thread, second = _start_race(
            db_engine,
            barrier,
            lambda session: _passive(
                session, committed_employee, trip_id, "2031-06-02T09:00:00", "2031-06-02T11:00:00"
            ),
        )
        first_thread.join()
        second_thread.join()
        outcomes = first + second
        assert sum(1 for item in outcomes if item[0] == "ok") == 1, outcomes
        assert sum(1 for item in outcomes if item == ("error", "VALIDATION_TRAVEL_OVERLAP")) == 1
        with Session(db_engine) as session:
            count = len(
                session.scalars(
                    select(TravelSegment).where(
                        TravelSegment.trip_id == trip_id,
                        TravelSegment.classification == "PASSIVE_TRAVEL",
                    )
                ).all()
            )
        assert count == 1
    finally:
        _cleanup_trip(db_engine, trip_id)


def test_concurrent_segment_corrections_yield_single_head(
    db_engine: Engine, committed_employee: Employee
) -> None:
    """Two sessions correcting the SAME segment version: one wins, one 409s.

    Without the advisory lock both corrections pass the version check and
    create two children of the same parent — two current heads.  With it the
    loser sees the new head and gets CONFLICT_VERSION.
    """
    trip_id = _seed_trip(db_engine, committed_employee)
    with Session(db_engine) as session, session.begin():
        segment_id = _passive(
            session, committed_employee, trip_id, "2031-06-03T08:00:00", "2031-06-03T10:00:00"
        )
    barrier = threading.Barrier(2)
    try:
        pairs: list[tuple[threading.Thread, list[tuple[str, object]]]] = []
        for end in ("2031-06-03T12:00:00", "2031-06-03T14:00:00"):
            thread, outcome = _start_race(
                db_engine,
                barrier,
                lambda session, end=end: (
                    patch_segment(
                        session,
                        committed_employee,
                        segment_id,
                        SegmentPatch(
                            version=1,
                            reason="concurrent correction",
                            start_local=datetime.fromisoformat("2031-06-03T08:00:00"),
                            end_local=datetime.fromisoformat(end),
                            classification=TravelSegmentClassification.PASSIVE_TRAVEL,
                        ),
                    ).version
                ),
            )
            pairs.append((thread, outcome))
        for thread, _outcome in pairs:
            thread.join()
        outcomes = [item for _thread, outcome in pairs for item in outcome]
        assert sum(1 for item in outcomes if item == ("ok", 2)) == 1, outcomes
        # The loser's patch targets the superseded id after the winner's
        # commit; _current_segment then resolves it as not-found (the codebase
        # treats stale ids as 404, matching the single-session API tests),
        # so exactly one current head can ever exist.
        assert sum(1 for item in outcomes if item[0] == "error") == 1, outcomes
        with Session(db_engine) as session:
            heads = session.scalars(
                select(TravelSegment).where(TravelSegment.supersedes_id == segment_id)
            ).all()
        assert len(heads) == 1, f"one current head expected, found {len(heads)}"
    finally:
        _cleanup_trip(db_engine, trip_id)
