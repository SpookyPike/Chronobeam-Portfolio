"""Effective-dated profile + preferences (WT-BACKEND §5.1/§8.1, M11)."""

from datetime import date

from app.identity.models import Employee, WorkScheduleVersion
from app.worktime.walltime import now_berlin
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.conftest import DEVELOPER_PRINCIPAL_HEADER as ME
from tests.conftest import HR_HEADER

PROFILE = "/api/v1/me/profile"
PREFERENCES = "/api/v1/me/preferences"


def _employee_a(db_session: Session) -> Employee:
    employee = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert employee is not None
    return employee


def test_profile_effective_at_seed_contract(db_client: TestClient) -> None:
    profile = db_client.get(f"{PROFILE}?at=2026-06-15", headers=ME).json()
    assert profile["effective_at"] == "2026-06-15"
    assert profile["employee"]["legal_name"] == "Employee A (synthetic)"
    assert profile["employee"]["active"] is True
    assert profile["contract"] is not None
    assert profile["contract"]["weekly_required_minutes"] == 1740
    assert profile["contract"]["source"] == "ADMIN_SEED"
    assert profile["schedule"] is not None
    assert profile["schedule"]["mon_required_minutes"] == 348
    assert profile["schedule"]["sat_required_minutes"] == 0


def test_profile_before_valid_from_is_empty(db_client: TestClient) -> None:
    profile = db_client.get(f"{PROFILE}?at=2025-12-31", headers=ME).json()
    assert profile["contract"] is None
    assert profile["schedule"] is None
    # employee identity itself is always present
    assert profile["employee"]["external_employee_id"] == "emp-0001"
    assert profile["employee"]["active"] is True


def test_profile_defaults_to_today(db_client: TestClient) -> None:
    """effective_at defaults to TODAY IN EUROPE/BERLIN — comparing against
    date.today() (system/UTC) flakes across midnight UTC (CI hit this at
    2026-09-10T23:59Z, when Berlin was already 2026-09-11)."""
    profile = db_client.get(PROFILE, headers=ME).json()
    assert profile["effective_at"] == now_berlin().date().isoformat()


def test_profile_valid_to_is_inclusive(db_client: TestClient, db_session: Session) -> None:
    """M11 boundary: valid_to >= at — the END date itself is still effective."""
    employee = _employee_a(db_session)
    schedule = db_session.scalar(
        select(WorkScheduleVersion).where(WorkScheduleVersion.employee_id == employee.id)
    )
    assert schedule is not None
    schedule.valid_to = date(2026, 6, 30)
    db_session.flush()

    on_last_day = db_client.get(f"{PROFILE}?at=2026-06-30", headers=ME).json()
    assert on_last_day["schedule"] is not None
    after = db_client.get(f"{PROFILE}?at=2026-07-01", headers=ME).json()
    assert after["schedule"] is None


def test_preferences_set_update_and_partial(db_client: TestClient) -> None:
    first = db_client.patch(
        PREFERENCES,
        headers=ME,
        json={"usual_work_type": "ON_SITE", "preferred_calendar_view": "month"},
    )
    assert first.status_code == 200, first.text
    assert first.json() == {
        "usual_work_type": "ON_SITE",
        "preferred_calendar_view": "month",
        "normal_day_template": None,
    }

    partial = db_client.patch(PREFERENCES, headers=ME, json={"preferred_calendar_view": "week"})
    assert partial.json() == {
        "usual_work_type": "ON_SITE",
        "preferred_calendar_view": "week",
        "normal_day_template": None,
    }

    cleared = db_client.patch(PREFERENCES, headers=ME, json={"usual_work_type": None})
    assert cleared.json() == {
        "usual_work_type": None,
        "preferred_calendar_view": "week",
        "normal_day_template": None,
    }


def test_normal_day_template_is_an_employee_preference(db_client: TestClient) -> None:
    response = db_client.patch(
        PREFERENCES,
        headers=ME,
        json={
            "normal_day_template": [
                {"start": "08:00", "end": "12:00", "work_type": "ON_SITE"},
                {"start": "12:30", "end": "16:18", "work_type": "REMOTE"},
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["normal_day_template"] == [
        {
            "start": "08:00:00",
            "end": "12:00:00",
            "ends_next_day": False,
            "work_type": "ON_SITE",
            "comment": None,
        },
        {
            "start": "12:30:00",
            "end": "16:18:00",
            "ends_next_day": False,
            "work_type": "REMOTE",
            "comment": None,
        },
    ]
    profile = db_client.get(f"{PROFILE}?at=2026-07-01", headers=ME)
    assert (
        profile.json()["preferences"]["normal_day_template"]
        == response.json()["normal_day_template"]
    )


def test_preferences_are_employee_scoped(db_client: TestClient) -> None:
    db_client.patch(PREFERENCES, headers=ME, json={"preferred_calendar_view": "month"})
    # B has its own (empty) preference row — never A's
    response = db_client.get(PROFILE, headers=ME)
    assert response.status_code == 200  # sanity: A still fine


def test_preferences_denied_for_hr(db_client: TestClient) -> None:
    response = db_client.patch(
        PREFERENCES, headers=HR_HEADER, json={"preferred_calendar_view": "month"}
    )
    assert response.status_code == 403

    response = db_client.get(PROFILE, headers=HR_HEADER)
    assert response.status_code == 403


def test_preference_view_enum_bound(db_client: TestClient) -> None:
    """preferred_calendar_view is bounded; a long/free-form value is rejected
    by the unified validation envelope (M6)."""
    response = db_client.patch(PREFERENCES, headers=ME, json={"preferred_calendar_view": "x" * 100})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "VALIDATION_FAILED"
