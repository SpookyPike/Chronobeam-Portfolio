"""P2 — statutory holiday calendar readiness.

Two oracles, deliberately independent of the implementation:

- the **statutory list** is fixed by ``LAW-06``, the state public-holiday statute
  (Gesetz über Sonn- und Feiertage des Landes Schleswig-Holstein, SFTG §2(1)): ten
  days, no more and no fewer, and the Reformation-Day entry only exists because of
  the 2018 amendment;
- the **Easter dates** for 2026-2035 are the published Gregorian computus table,
  so a broken computus cannot validate itself.

The end-to-end half runs the real engine over the seeded dev/demo calendar, so
"present and usable" is demonstrated as *effect* — a classified day, a zero
requirement, a covered demo month — rather than as a row count.
"""

from datetime import date, timedelta

import pytest
from app.calendar.holidays import (
    DEV_OVERLAY_FROM,
    SH_HORIZON_FROM,
    SH_HORIZON_TO,
    easter_sunday,
    statutory_days,
    statutory_holidays,
    statutory_source,
    statutory_version,
)
from app.calendar.models import CalendarDay, CalendarVersion
from app.calendar_admin import main as calendar_admin_main
from app.calendar_admin import publish as calendar_admin_publish
from app.dev_seed import DEV_CALENDAR_VERSION, publish_dev_calendar
from app.enums import CalendarDayClass
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.conftest import DEVELOPER_PRINCIPAL_HEADER as ME
from tests.conftest import OTHER_EMPLOYEE_HEADER as OTHER

MONTHS = "/api/v1/me/months"

#: The published Gregorian Easter dates 2026-2035 (independent oracle).
EASTER_TABLE = {
    2026: date(2026, 4, 5),
    2027: date(2027, 3, 28),
    2028: date(2028, 4, 16),
    2029: date(2029, 4, 1),
    2030: date(2030, 4, 21),
    2031: date(2031, 4, 13),
    2032: date(2032, 3, 28),
    2033: date(2033, 4, 17),
    2034: date(2034, 4, 9),
    2035: date(2035, 3, 25),
}

#: ``SFTG`` §2(1) for 2026, written out independently of the generator.
SFTG_2026 = {
    date(2026, 1, 1),  # Neujahrstag
    date(2026, 4, 3),  # Karfreitag
    date(2026, 4, 6),  # Ostermontag
    date(2026, 5, 1),  # 1. Mai
    date(2026, 5, 14),  # Himmelfahrtstag
    date(2026, 5, 25),  # Pfingstmontag
    date(2026, 10, 3),  # Tag der Deutschen Einheit
    date(2026, 10, 31),  # Reformationstag
    date(2026, 12, 25),  # 1. Weihnachtstag
    date(2026, 12, 26),  # 2. Weihnachtstag
}

FIXED_DAYS = ((1, 1), (5, 1), (10, 3), (10, 31), (12, 25), (12, 26))


@pytest.fixture()
def statutory_seed(db_session: Session) -> None:
    """Publish the dev/demo calendar (coverage edition + statutory overlay).

    In-transaction: the per-test transaction is rolled back, so the statutory
    overlay never leaks into another test.
    """
    publish_dev_calendar(db_session, statutory_sh=True)


def _edition(session: Session, valid_from: date) -> CalendarVersion | None:
    return session.scalar(select(CalendarVersion).where(CalendarVersion.valid_from == valid_from))


def _day_rows(session: Session, edition: CalendarVersion) -> list[tuple[date, str]]:
    rows = session.execute(
        select(CalendarDay.day, CalendarDay.day_class).where(
            CalendarDay.calendar_version_id == edition.id
        )
    ).all()
    return [(row[0], row[1].value) for row in rows]


def _month_row(client: TestClient, month: str, day: date, headers: dict | None = None) -> dict:
    response = client.get(f"{MONTHS}/{month}", headers=headers or ME)
    assert response.status_code == 200, response.text
    return next(
        item for item in response.json()["daily_results"] if item["date"] == day.isoformat()
    )


# ---------------------------------------------------------------------------
# The derived day set — pure, against the statute and the Easter table
# ---------------------------------------------------------------------------


def test_the_easter_computus_matches_the_published_table() -> None:
    assert {year: easter_sunday(year) for year in EASTER_TABLE} == EASTER_TABLE


def test_the_statutory_set_is_exactly_the_sftg_2_1_list() -> None:
    """Ten days. An extra day would invent a holiday; a missing one would not."""
    assert set(statutory_holidays(2026, 2026)) == SFTG_2026


def test_the_fixed_date_holidays_recur_on_the_same_day_every_year() -> None:
    days = statutory_holidays(2026, 2035)
    for year in range(2026, 2036):
        for month, day in FIXED_DAYS:
            assert date(year, month, day) in days, (year, month, day)


def test_the_movable_holidays_follow_easter() -> None:
    days = statutory_holidays(2026, 2035)
    for year, easter in EASTER_TABLE.items():
        for offset in (-2, 1, 39, 50):
            assert easter + timedelta(days=offset) in days, (year, offset)


def test_reformationstag_is_statutory_while_other_laender_holidays_are_not() -> None:
    """The SH-specific day, and the days SH deliberately does NOT have."""
    days = statutory_holidays(2026, 2035)
    assert all(date(year, 10, 31) in days for year in range(2026, 2036))
    absent = {
        "Buß- und Bettag": [date(year, 11, 18) for year in range(2026, 2036)],
        "Fronleichnam": [date(year, 6, 4) for year in range(2026, 2036)],
        "Allerheiligen": [date(year, 11, 1) for year in range(2026, 2036)],
        "Mariä Himmelfahrt": [date(year, 8, 15) for year in range(2026, 2036)],
        "Internationaler Frauentag": [date(year, 3, 8) for year in range(2026, 2036)],
        "Ostersonntag": [EASTER_TABLE[year] for year in range(2026, 2036)],
    }
    for label, candidates in absent.items():
        assert not (set(candidates) & set(days)), label


def test_every_year_in_the_horizon_yields_ten_distinct_days() -> None:
    days = statutory_holidays(2026, 2035)
    assert len(days) == 100
    for year in range(2026, 2036):
        assert len([day for day in days if day.year == year]) == 10


def test_the_derived_set_contains_no_employer_free_day() -> None:
    """``OPEN-12``: the employer-free days stay operator-supplied.

    If the generator ever emitted one, the recurring Christmas closure would be
    invented rather than supplied by the employer.
    """
    assert set(statutory_holidays(2026, 2035).values()) == {CalendarDayClass.PUBLIC_HOLIDAY}


def test_the_year_range_is_validated() -> None:
    with pytest.raises(ValueError, match="before from_year"):
        statutory_holidays(2027, 2026)


def test_the_horizon_restriction_drops_days_outside_the_edition_range() -> None:
    """The writer refuses an out-of-range day row, so the cut happens here."""
    assert statutory_days(date(2026, 10, 1), date(2026, 12, 31)) == {
        date(2026, 10, 3): CalendarDayClass.PUBLIC_HOLIDAY,
        date(2026, 10, 31): CalendarDayClass.PUBLIC_HOLIDAY,
        date(2026, 12, 25): CalendarDayClass.PUBLIC_HOLIDAY,
        date(2026, 12, 26): CalendarDayClass.PUBLIC_HOLIDAY,
    }


def test_the_horizon_and_the_dev_overlay_start_where_they_must() -> None:
    """Two different start dates, each load-bearing — pinned deliberately.

    ``SH_HORIZON_FROM`` is the MVP horizon start and the range the *demo* database
    publishes from, so every month of every horizon year is classified.
    ``DEV_OVERLAY_FROM`` is later because the dev/test fixture already owns
    2026-01-01 with its open-ended coverage edition, and
    ``uq_calendar_version_effective_from`` permits one edition per effective date.
    """
    assert date(2026, 1, 1) == SH_HORIZON_FROM
    assert date(2026, 10, 1) == DEV_OVERLAY_FROM
    assert date(2035, 12, 31) == SH_HORIZON_TO
    assert statutory_version(SH_HORIZON_FROM, SH_HORIZON_TO) == "sh-statutory-2026-2035-v1"
    assert "SFTG SH 2004 §2(1)" in statutory_source(SH_HORIZON_FROM, SH_HORIZON_TO)


# ---------------------------------------------------------------------------
# The operator CLI
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        # no day-set source at all
        ["publish", "--from", "2026-10-01", "--to", "2035-12-31"],
        # both sources
        [
            "publish",
            "--file",
            "days.csv",
            "--statutory-sh",
            "--from",
            "2026-10-01",
            "--to",
            "2035-12-31",
            "--version",
            "v",
            "--source",
            "s",
        ],
        # --file still has to name its provenance and label
        ["publish", "--file", "days.csv", "--from", "2026-10-01"],
        # a derived set is published bounded, so --to is mandatory
        ["publish", "--statutory-sh", "--from", "2026-10-01"],
    ],
)
def test_the_cli_refuses_an_ambiguous_or_incomplete_day_set_source(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        calendar_admin_main(argv)
    assert excinfo.value.code == 2


def test_the_cli_dry_run_reports_the_derived_set(db_session: Session, capsys) -> None:
    """The operator path derives the day set and writes nothing."""
    assert (
        calendar_admin_publish(
            db_session,
            path=None,
            statutory_sh=True,
            version=None,
            valid_from=DEV_OVERLAY_FROM,
            valid_to=SH_HORIZON_TO,
            source=None,
            dry_run=True,
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "94 exception day(s) added" in printed
    assert "SFTG SH 2004 §2(1)" in printed
    assert "dry-run: nothing written" in printed
    assert _edition(db_session, DEV_OVERLAY_FROM) is None


# ---------------------------------------------------------------------------
# End to end — the seeded dev/demo calendar actually classifies days
# ---------------------------------------------------------------------------


def test_the_seeded_dev_calendar_publishes_the_statutory_overlay(
    db_session: Session, statutory_seed: None
) -> None:
    coverage = _edition(db_session, date(2026, 1, 1))
    overlay = _edition(db_session, DEV_OVERLAY_FROM)
    assert coverage is not None and coverage.version == DEV_CALENDAR_VERSION
    assert overlay is not None
    assert overlay.valid_to == SH_HORIZON_TO
    assert _day_rows(db_session, overlay) == sorted(
        statutory_days(DEV_OVERLAY_FROM, SH_HORIZON_TO).items(), key=lambda item: item[0]
    )


def test_the_overlay_classifies_the_october_holidays_the_demo_shows(
    db_client: TestClient, statutory_seed: None
) -> None:
    """October 2026 is the demo month: both SH holidays must be classified.

    Neither falls on a scheduled weekday (3 Oct and 31 Oct 2026 are Saturdays),
    so the requirement is zero either way — the *classification* is the observable
    effect, and the negative control is an ordinary Monday.
    """
    assert date(2026, 10, 3).weekday() == 5 and date(2026, 10, 31).weekday() == 5
    for holiday in (date(2026, 10, 3), date(2026, 10, 31)):
        row = _month_row(db_client, "2026-10", holiday)
        assert row["calendar_class"] == "PUBLIC_HOLIDAY", holiday
        assert row["required_minutes"] == 0, holiday
    assert _month_row(db_client, "2026-10", date(2026, 10, 5))["calendar_class"] == "WEEKDAY"


def test_the_overlay_zeroes_required_time_on_a_weekday_holiday(
    db_client: TestClient, statutory_seed: None
) -> None:
    """2026-12-25 is a Friday: the discriminating case for ``WT-FLEX-001``."""
    assert date(2026, 12, 25).weekday() == 4
    holiday = _month_row(db_client, "2026-12", date(2026, 12, 25))
    assert holiday["calendar_class"] == "PUBLIC_HOLIDAY"
    assert holiday["required_minutes"] == 0
    ordinary = _month_row(db_client, "2026-12", date(2026, 12, 24))
    assert ordinary["calendar_class"] == "WEEKDAY"
    assert ordinary["required_minutes"] == 348


def test_a_holiday_on_a_weekend_keeps_its_class(
    db_client: TestClient, statutory_seed: None
) -> None:
    """Weekend interaction: a Sunday holiday is still classified, and adds nothing.

    ``2027-10-03`` is a Sunday, so its requirement is zero from the schedule
    alone; the classification must survive anyway, because ``ALLOW-006`` and
    ``TRAVEL-LOCAL-001`` read the class rather than the requirement.
    """
    assert date(2027, 10, 3).weekday() == 6
    row = _month_row(db_client, "2027-10", date(2027, 10, 3))
    assert row["calendar_class"] == "PUBLIC_HOLIDAY"
    assert row["required_minutes"] == 0
    assert _month_row(db_client, "2027-10", date(2027, 10, 4))["calendar_class"] == "WEEKDAY"


def test_the_seed_keeps_the_demo_month_covered(db_client: TestClient, statutory_seed: None) -> None:
    """Coverage is fail-closed: the overlay must not remove the July 2026 month.

    ``seed_demo`` publishes the calendar precisely so the July demo month is
    computable; an overlay that started the calendar at 2026-10-01 without the
    open-ended coverage edition would turn the demo month into 409
    ``CALENDAR_BLOCKED``.
    """
    response = db_client.get(f"{MONTHS}/2026-07", headers=ME)
    assert response.status_code == 200, response.text
    assert response.json()["daily_results"]


def test_the_year_boundary_and_the_horizon_end_resolve_as_declared(
    db_client: TestClient, statutory_seed: None
) -> None:
    """Year transitions, and the honest edge of a bounded horizon.

    ``2026-12-31`` (Thursday) is ordinary while ``2027-01-01`` (Friday) is a
    holiday: adjacent days on either side of a year boundary resolve from the same
    edition. ``2036-01-01`` falls past ``SH_HORIZON_TO``, so the coverage edition
    resumes and the day is ordinary rather than silently still classified.
    """
    assert date(2026, 12, 31).weekday() == 3 and date(2027, 1, 1).weekday() == 4
    assert _month_row(db_client, "2026-12", date(2026, 12, 31))["calendar_class"] == "WEEKDAY"
    new_year = _month_row(db_client, "2027-01", date(2027, 1, 1))
    assert new_year["calendar_class"] == "PUBLIC_HOLIDAY"
    assert new_year["required_minutes"] == 0

    assert _month_row(db_client, "2035-12", date(2035, 12, 26))["calendar_class"] == (
        "PUBLIC_HOLIDAY"
    )
    assert date(2036, 1, 1).weekday() == 1
    assert _month_row(db_client, "2036-01", date(2036, 1, 1))["calendar_class"] == "WEEKDAY"


def test_a_second_work_calendar_gets_the_same_holiday_effect(
    db_client: TestClient, statutory_seed: None
) -> None:
    """The classification is institutional, the requirement is the employee's own.

    Employee B runs a different (part-time) schedule, so the discriminating
    assertion is that the same holiday zeroes *their* weekday requirement while the
    adjacent ordinary day keeps it.
    """
    holiday = _month_row(db_client, "2026-12", date(2026, 12, 25), headers=OTHER)
    assert holiday["calendar_class"] == "PUBLIC_HOLIDAY"
    assert holiday["required_minutes"] == 0
    ordinary = _month_row(db_client, "2026-12", date(2026, 12, 24), headers=OTHER)
    assert ordinary["required_minutes"] == 174


def test_publishing_the_statutory_overlay_twice_is_a_no_op(
    db_session: Session, statutory_seed: None
) -> None:
    """No duplicate holiday effect: one edition, one row per day."""
    before = _edition(db_session, DEV_OVERLAY_FROM)
    assert before is not None
    publish_dev_calendar(db_session, statutory_sh=True)
    after = _edition(db_session, DEV_OVERLAY_FROM)
    assert after is not None and after.id == before.id
    rows = _day_rows(db_session, after)
    assert len(rows) == len(set(rows)) == 94
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(CalendarVersion)
            .where(CalendarVersion.valid_from == DEV_OVERLAY_FROM)
        )
        == 1
    )


def test_the_statutory_edition_records_its_statutory_provenance(
    db_session: Session, statutory_seed: None
) -> None:
    overlay = _edition(db_session, DEV_OVERLAY_FROM)
    assert overlay is not None
    assert overlay.source == statutory_source(DEV_OVERLAY_FROM, SH_HORIZON_TO)
    assert "SFTG SH 2004 §2(1)" in overlay.source
    assert "employer-free days not included" in overlay.source


def test_the_default_seed_stays_exception_free(db_session: Session) -> None:
    """The pytest fixture keeps its exception-free edition.

    This is load-bearing for the rest of the suite: ``tests/test_travel_api.py``
    works on 2026-05-01 (Tag der Arbeit), so a statutory holiday there would add
    TRAVEL-LOCAL-001 findings as a side effect of a fixture.
    """
    publish_dev_calendar(db_session)
    coverage = _edition(db_session, date(2026, 1, 1))
    assert coverage is not None and coverage.version == DEV_CALENDAR_VERSION
    assert _day_rows(db_session, coverage) == []
    assert _edition(db_session, DEV_OVERLAY_FROM) is None
