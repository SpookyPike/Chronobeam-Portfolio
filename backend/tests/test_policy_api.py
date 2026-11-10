"""Employee-only calculation integration tests."""

from datetime import date

from app.calendar.service import publish_calendar_edition
from app.enums import CalendarDayClass, MasterDataSource
from app.identity.models import Employee, EmploymentContract, WorkScheduleVersion
from app.policy.service import calculate_employee_period
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from .conftest import DEVELOPER_PRINCIPAL_HEADER, HR_HEADER


def test_ACCESS_T11_private_findings_never_materialize_for_hr(db_client: TestClient) -> None:
    employee = db_client.get(
        "/api/v1/me/findings?period=2026-07", headers=DEVELOPER_PRINCIPAL_HEADER
    )
    hr = db_client.get("/api/v1/me/findings?period=2026-07", headers=HR_HEADER)
    assert employee.status_code == 200
    assert set(employee.json()) == {
        "period_month",
        "opening_balance_minutes",
        "closing_balance_minutes",
        "findings",
    }
    assert hr.status_code == 403
    assert "findings" not in hr.text


def test_WT_FLEX_001_service_uses_effective_schedule_and_calendar_version(
    db_session: Session,
) -> None:
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    schedule = db_session.scalar(
        select(WorkScheduleVersion).where(WorkScheduleVersion.employee_id == employee.id)
    )
    assert schedule is not None
    schedule.valid_to = date(2026, 6, 30)
    db_session.add(
        WorkScheduleVersion(
            employee_id=employee.id,
            valid_from=date(2026, 7, 1),
            valid_to=None,
            weekly_required_minutes=351 * 5,  # weekday sum == weekly
            mon_required_minutes=351,
            tue_required_minutes=351,
            wed_required_minutes=351,
            thu_required_minutes=351,
            fri_required_minutes=351,
            sat_required_minutes=0,
            sun_required_minutes=0,
            mon_typical_start=None,
            mon_typical_end=None,
            tue_typical_start=None,
            tue_typical_end=None,
            wed_typical_start=None,
            wed_typical_end=None,
            thu_typical_start=None,
            thu_typical_end=None,
            fri_typical_start=None,
            fri_typical_end=None,
            sat_typical_start=None,
            sat_typical_end=None,
            sun_typical_start=None,
            sun_typical_end=None,
            source=schedule.source,
            version=2,
        )
    )
    # F5: the calendar is published through the ONE writer. The retired
    # placeholder edition used to be mutated directly here; a bounded overlay is
    # now the only way to classify a day (EINV-21: editions are insert-only).
    publish_calendar_edition(
        db_session,
        version="july-holiday-v1",
        valid_from=date(2026, 7, 1),
        valid_to=date(2026, 7, 1),
        source="TEST",
        days={date(2026, 7, 1): CalendarDayClass.PUBLIC_HOLIDAY},
        actor_principal="TEST",
    )
    result = calculate_employee_period(db_session, employee, date(2026, 7, 1))
    assert result.daily_results[0].required_minutes == 0
    assert result.daily_results[1].required_minutes == 351


def test_WT_FLEX_003_004_threshold_uses_mid_month_effective_schedule(
    db_session: Session,
) -> None:
    """The weekly threshold follows the effective employee schedule.

    Mid-month schedule change (1740 -> 870 on 16 July) must be weighted by the
    days each schedule governs, like the previous contract-based rule it
    replaced (decision log 2026-09-11).
    """
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    schedule = db_session.scalar(
        select(WorkScheduleVersion).where(WorkScheduleVersion.employee_id == employee.id)
    )
    assert schedule is not None
    schedule.valid_to = date(2026, 7, 15)
    db_session.add(
        WorkScheduleVersion(
            employee_id=employee.id,
            valid_from=date(2026, 7, 16),
            valid_to=None,
            weekly_required_minutes=870,
            mon_required_minutes=174,
            tue_required_minutes=174,
            wed_required_minutes=174,
            thu_required_minutes=174,
            fri_required_minutes=174,
            sat_required_minutes=0,
            sun_required_minutes=0,
            mon_typical_start=None,
            mon_typical_end=None,
            tue_typical_start=None,
            tue_typical_end=None,
            wed_typical_start=None,
            wed_typical_end=None,
            thu_typical_start=None,
            thu_typical_end=None,
            fri_typical_start=None,
            fri_typical_end=None,
            sat_typical_start=None,
            sat_typical_end=None,
            sun_typical_start=None,
            sun_typical_end=None,
            source=MasterDataSource.EMPLOYEE,
            version=2,
        )
    )
    db_session.flush()

    from app.policy.service import _weekly_required_minutes

    assert (
        _weekly_required_minutes(db_session, employee, date(2026, 7, 1), date(2026, 8, 1)) == 1290
    )


def test_WT_FLEX_003_004_threshold_falls_back_to_contract_without_schedule(
    db_session: Session,
) -> None:
    """Without an effective schedule the contract weekly value is the fallback."""
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    schedule = db_session.scalar(
        select(WorkScheduleVersion).where(WorkScheduleVersion.employee_id == employee.id)
    )
    assert schedule is not None
    schedule.valid_to = date(2026, 6, 30)  # no schedule effective during July
    contract = db_session.scalar(
        select(EmploymentContract).where(EmploymentContract.employee_id == employee.id)
    )
    assert contract is not None
    contract.valid_to = date(2026, 7, 15)
    db_session.add(
        EmploymentContract(
            employee_id=employee.id,
            valid_from=date(2026, 7, 16),
            valid_to=None,
            fte=contract.fte,
            weekly_required_minutes=870,
            pay_group_bucket=contract.pay_group_bucket,
            employment_start=contract.employment_start,
            employment_end=contract.employment_end,
            source=contract.source,
            source_ref=contract.source_ref,
            version=2,
        )
    )
    db_session.flush()

    from app.policy.service import _weekly_required_minutes

    assert (
        _weekly_required_minutes(db_session, employee, date(2026, 7, 1), date(2026, 8, 1)) == 1290
    )


def test_WT_FLEX_001_calendar_versions_can_change_inside_month(db_session: Session) -> None:
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    # F5: two published editions straddle the month through the ONE writer — a
    # bounded first edition and its open-ended successor — instead of mutating a
    # published range (EINV-21).
    first = publish_calendar_edition(
        db_session,
        version="mid-july-first-v1",
        valid_from=date(2026, 7, 1),
        valid_to=date(2026, 7, 15),
        source="TEST",
        days={date(2026, 7, 14): CalendarDayClass.PUBLIC_HOLIDAY},
        actor_principal="TEST",
    )
    second = publish_calendar_edition(
        db_session,
        version="mid-july-second-v1",
        valid_from=date(2026, 7, 16),
        valid_to=None,
        source="TEST",
        days={date(2026, 7, 16): CalendarDayClass.EMPLOYER_FREE},
        actor_principal="TEST",
    )

    result = calculate_employee_period(db_session, employee, date(2026, 7, 1))
    daily = {item.day: item for item in result.daily_results}
    assert daily[date(2026, 7, 14)].required_minutes == 0
    assert daily[date(2026, 7, 16)].required_minutes == 0
    # Both resolving editions are recorded, and the seeded dev edition no longer
    # resolves for any July day (a later edition covers them all).
    assert set(result.calendar_version_ids) == {str(first.id), str(second.id)}
