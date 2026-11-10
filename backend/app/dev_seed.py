"""Synthetic dev/test employee seeding.

There is deliberately NO startup seeding (a lifespan hook would turn every
TestClient test into a DB test and write rows on every --reload). Seeding is
an explicit developer action: ``just seed-dev`` runs ``python -m app.dev_seed``
which REFUSES unless CHRONOBEAM_ENV is exactly ``dev`` (never test, never
production). Tests seed themselves (session-scoped fixture).

Data is synthetic and clearly labeled (legal_name "... (synthetic)");
human-readable synthetic names are used instead of real employee names.
"""

import sys
from datetime import date, time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session_factory
from app.enums import MasterDataSource, PayGroupBucket
from app.identity.models import Employee, EmploymentContract, WorkScheduleVersion

#: The **test fixture** calendar edition: synthetic and self-identifying on
#: purpose. The version label and the source both state plainly that this dataset
#: declares NO exception days, so the difference from the retired placeholder is
#: that a *declared* absence of exceptions inside a governed edition is a data
#: statement, whereas the placeholder made "coverage missing" undetectable.
#:
#: Deliberately NOT the real Schleswig-Holstein holidays: ``tests/test_travel_api.py``
#: works on 2026-05-01 (Tag der Arbeit), so a seeded statutory holiday would add
#: TRAVEL-LOCAL-001 findings as a side effect of a *fixture*. The pytest fixture
#: (``tests/conftest.py``) therefore keeps this edition, while the developer/demo
#: seed publishes the real statutory calendar on top — see ``publish_dev_calendar``.
DEV_CALENDAR_VERSION = "dev-no-exceptions"
DEV_CALENDAR_SOURCE = "DEV_SEED"

SEED_EMPLOYEES: list[dict[str, Any]] = [
    {
        "idp_subject": "employee",
        "external_employee_id": "emp-0001",
        "legal_name": "Employee A (synthetic)",
        "weekly_required_minutes": 1740,  # 29:00, synthetic
        "pay_group_bucket": PayGroupBucket.LE_9B,
        "schedule_minutes": 348,  # Mon-Fri 5:48, synthetic
        "typical_start": time(8, 0),
        "typical_end": time(16, 48),
    },
    {
        "idp_subject": "employee-b",
        "external_employee_id": "emp-0002",
        "legal_name": "Employee B (synthetic)",
        "weekly_required_minutes": 870,  # 14:30, synthetic
        "pay_group_bucket": PayGroupBucket.GE_9C,
        "schedule_minutes": 174,  # Mon-Fri 2:54, synthetic
        "typical_start": time(8, 0),
        "typical_end": time(13, 48),
    },
]


def assert_no_range_overlap(
    session: Session,
    employee_id: object,
    model: type,
    valid_from: date,
    valid_to: date | None,
) -> None:
    """Effective-range non-overlap guard for this seed's one row per employee.

    NOTE (F5): this is deliberately NOT the effective-dating model. Resolution is
    one row per day via the greatest ``(valid_from, version)``, and legitimate
    successor rows are written open-ended (``valid_to = NULL``), so a real
    non-overlap constraint would reject every append. The guard passes only
    because this seed writes ONE row per employee; ``app.master_data_admin``
    deliberately does not reuse it and relies on
    ``uq_contract_employee_valid_from`` instead.
    """
    new_end = valid_to or date.max
    for existing in session.scalars(select(model).where(model.employee_id == employee_id)).all():
        existing_end = existing.valid_to or date.max
        if valid_from <= existing_end and existing.valid_from <= new_end:
            raise ValueError(f"overlapping effective range for {model.__name__}")


def _schedule_row(employee_id: object, spec: dict[str, Any]) -> WorkScheduleVersion:
    minutes = int(spec["schedule_minutes"])
    return WorkScheduleVersion(
        employee_id=employee_id,
        valid_from=date(2026, 1, 1),
        valid_to=None,
        # Invariant (DATA-012): weekday sum == weekly target.
        weekly_required_minutes=minutes * 5,
        mon_required_minutes=minutes,
        tue_required_minutes=minutes,
        wed_required_minutes=minutes,
        thu_required_minutes=minutes,
        fri_required_minutes=minutes,
        sat_required_minutes=0,
        sun_required_minutes=0,
        mon_typical_start=spec["typical_start"],
        mon_typical_end=spec["typical_end"],
        tue_typical_start=spec["typical_start"],
        tue_typical_end=spec["typical_end"],
        wed_typical_start=spec["typical_start"],
        wed_typical_end=spec["typical_end"],
        thu_typical_start=spec["typical_start"],
        thu_typical_end=spec["typical_end"],
        fri_typical_start=spec["typical_start"],
        fri_typical_end=spec["typical_end"],
        sat_typical_start=None,
        sat_typical_end=None,
        sun_typical_start=None,
        sun_typical_end=None,
        source=MasterDataSource.ADMIN_SEED,
        version=1,
    )


def publish_dev_calendar(session: Session, *, statutory_sh: bool = False) -> None:
    """Publish the calendar editions this environment should run on, through the ONE writer.

    Both go through ``publish_calendar_edition``, so the seeds exercise the real
    path (the coordination lock and the EINV-15 reopen — a no-op on a fresh
    database) rather than taking an advisory key of their own.

    The open-ended, exception-free coverage edition is always published first,
    because coverage is fail-closed: without an edition governing a month, its view
    answers 409 ``CALENDAR_BLOCKED``, and the demo month (July 2026) needs coverage
    regardless of which edition classifies its days.

    ``statutory_sh=True`` (``just seed-dev``) then publishes the real ``SFTG`` §2(1)
    day set on top, so a developer sees real holidays. It is a *bounded overlay*
    starting ``DEV_OVERLAY_FROM`` (2026-10-01), which is what lets the two coexist:
    ``uq_calendar_version_effective_from`` permits one edition per effective date and
    the coverage edition already owns 2026-01-01. Days from 2026-01-01 to 2026-09-30
    therefore stay ordinary working days in a *dev* database.

    That compromise is deliberately not inherited by the demo: ``just demo-reset``
    gives the demo database a fresh schema, so ``app.demo_seed`` publishes the
    statutory set for the whole horizon from ``SH_HORIZON_FROM`` (2026-01-01) with no
    coverage edition at all. The two seeds must not both run against one database —
    they would claim 2026-01-01 for different editions — and ``publish_demo_calendar``
    refuses that case with a message naming ``just demo-reset``.

    Idempotent either way: re-running with the same labels and identical day sets is
    a no-op success, so this upgrades an already-seeded dev database in place.
    """
    from app.calendar.holidays import (
        DEV_OVERLAY_FROM,
        SH_HORIZON_TO,
        statutory_days,
        statutory_source,
        statutory_version,
    )
    from app.calendar.service import publish_calendar_edition

    publish_calendar_edition(
        session,
        version=DEV_CALENDAR_VERSION,
        valid_from=date(2026, 1, 1),
        valid_to=None,
        source=DEV_CALENDAR_SOURCE,
        days={},
        actor_principal=DEV_CALENDAR_SOURCE,
    )
    if not statutory_sh:
        return
    publish_calendar_edition(
        session,
        version=statutory_version(DEV_OVERLAY_FROM, SH_HORIZON_TO),
        valid_from=DEV_OVERLAY_FROM,
        valid_to=SH_HORIZON_TO,
        source=statutory_source(DEV_OVERLAY_FROM, SH_HORIZON_TO),
        days=statutory_days(DEV_OVERLAY_FROM, SH_HORIZON_TO),
        actor_principal=DEV_CALENDAR_SOURCE,
    )


def seed_dev(session: Session, *, statutory_sh: bool = False) -> tuple[Employee, Employee]:
    """Idempotent upsert of the two synthetic employees + contract/schedule."""
    publish_dev_calendar(session, statutory_sh=statutory_sh)
    seeded: list[Employee] = []
    for spec in SEED_EMPLOYEES:
        employee = session.scalar(
            select(Employee).where(Employee.idp_subject == spec["idp_subject"])
        )
        if employee is None:
            employee = Employee(
                idp_subject=str(spec["idp_subject"]),
                external_employee_id=str(spec["external_employee_id"]),
                legal_name=str(spec["legal_name"]),
                email=None,
                active=True,
            )
            session.add(employee)
            session.flush()
        if not session.scalars(
            select(EmploymentContract).where(EmploymentContract.employee_id == employee.id)
        ).first():
            assert_no_range_overlap(
                session, employee.id, EmploymentContract, date(2026, 1, 1), None
            )
            session.add(
                EmploymentContract(
                    employee_id=employee.id,
                    valid_from=date(2026, 1, 1),
                    valid_to=None,
                    fte=1.0,
                    weekly_required_minutes=int(spec["weekly_required_minutes"]),
                    pay_group_bucket=spec["pay_group_bucket"],
                    employment_start=date(2020, 1, 1),
                    employment_end=None,
                    source=MasterDataSource.ADMIN_SEED,
                    version=1,
                )
            )
        if not session.scalars(
            select(WorkScheduleVersion).where(WorkScheduleVersion.employee_id == employee.id)
        ).first():
            assert_no_range_overlap(
                session, employee.id, WorkScheduleVersion, date(2026, 1, 1), None
            )
            session.add(_schedule_row(employee.id, spec))
        seeded.append(employee)
    session.commit()
    return seeded[0], seeded[1]


def main() -> None:
    settings = get_settings()
    if settings.env != "dev":
        sys.exit(f"refusing to seed: CHRONOBEAM_ENV must be 'dev' (got {settings.env!r})")
    with get_session_factory()() as session:
        # The developer/demo seed publishes the real statutory calendar, so a demo
        # runs on the calendar the pilot will use. The pytest fixture keeps the
        # exception-free placeholder (see ``publish_dev_calendar``).
        seed_dev(session, statutory_sh=True)
    print("dev seed complete (2 synthetic employees; ADMIN_SEED master data; SH calendar)")


if __name__ == "__main__":
    main()
