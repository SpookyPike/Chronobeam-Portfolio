"""F5 calendar resolution, coverage and the ONE writer (EINV-10/EINV-21).

Test names reuse the existing ``WT-FLEX-001`` calendar case rather than inventing
a parallel namespace.

Every test here fails against the pre-F5 behaviour: ``_calendar_classes``
returned ``({}, ())`` when nothing was selected, so every date was an ordinary
working day and a holiday silently added required time (audit B-2).
"""

from datetime import UTC, date, datetime

import pytest
from app.calendar.models import CALENDAR_KEY, CalendarDay, CalendarVersion
from app.calendar.service import (
    CalendarEditionRefused,
    first_uncovered_month,
    preview_calendar_edition,
    publish_calendar_edition,
)
from app.enums import CalendarDayClass, WorkType
from app.errors import CalendarUnavailable
from app.identity.models import Employee
from app.policy.engine import CalendarClass
from app.policy.service import _calendar_classes, calculate_employee_period
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from tests.conftest import DEVELOPER_PRINCIPAL_HEADER as ME

MONTHS = "/api/v1/me/months"
INTERVALS = "/api/v1/me/work-intervals"

JULY = date(2026, 7, 1)
HOLIDAY = date(2026, 7, 10)


@pytest.fixture()
def employee(db_session: Session) -> Employee:
    row = db_session.scalar(select(Employee).where(Employee.idp_subject == "employee"))
    assert row is not None
    return row


def _publish(
    session: Session,
    version: str,
    valid_from: date,
    valid_to: date | None,
    days: dict[date, CalendarDayClass] | None = None,
):
    return publish_calendar_edition(
        session,
        calendar_key=CALENDAR_KEY,
        version=version,
        valid_from=valid_from,
        valid_to=valid_to,
        source="TEST",
        days=days or {},
        actor_principal="TEST",
    )


def _classes(session: Session, start: date, end: date):
    return _calendar_classes(session, start, end)


# ---------------------------------------------------------------------------
# Coverage: fail closed instead of "every day is an ordinary working day"
# ---------------------------------------------------------------------------


def test_WT_FLEX_001_an_uncovered_month_fails_closed_instead_of_all_working(
    db_session: Session, employee: Employee
) -> None:
    # Remove every edition: the pre-F5 code returned ({}, ()) here and treated
    # the whole month as ordinary working days.
    db_session.execute(delete(CalendarDay))
    db_session.execute(delete(CalendarVersion))
    db_session.flush()
    with pytest.raises(CalendarUnavailable) as excinfo:
        calculate_employee_period(db_session, employee, JULY)
    assert excinfo.value.calendar_key == CALENDAR_KEY
    assert excinfo.value.first_uncovered == JULY


def test_WT_FLEX_001_the_uncovered_error_names_the_first_uncovered_day_and_the_chain_month(
    db_client, db_session: Session
) -> None:
    """A chain month that is uncovered must be named, not just the requested one.

    The carry-forward chain re-runs every month between the anchor and the
    requested month, so 2026-09 can be uncovered because 2026-08 is.
    """
    db_session.execute(delete(CalendarDay))
    db_session.execute(delete(CalendarVersion))
    db_session.flush()
    # An interval anchors the chain at 2026-07; only July is then covered.
    _publish(db_session, "july-only-v1", JULY, date(2026, 7, 31))
    response = db_client.post(
        INTERVALS,
        headers=ME,
        json={
            "start_local": "2026-07-06T08:00:00",
            "end_local": "2026-07-06T16:00:00",
            "work_type": WorkType.ON_SITE.value,
        },
    )
    assert response.status_code == 201, response.text
    uncovered = db_client.get(f"{MONTHS}/2026-09", headers=ME)
    assert uncovered.status_code == 409, uncovered.text
    detail = uncovered.json()["detail"]
    assert detail["code"] == "CALENDAR_BLOCKED"
    assert detail["details"]["calendar_key"] == CALENDAR_KEY
    assert detail["details"]["first_uncovered_day"] == "2026-08-01"
    assert detail["details"]["chain_month"] == "2026-08"


def test_WT_FLEX_001_a_covered_edition_without_a_row_for_the_day_is_an_ordinary_working_day(
    db_session: Session, employee: Employee
) -> None:
    """Pins the OTHER direction, so fail-closed cannot be over-applied."""
    classes, ids = _classes(db_session, JULY, date(2026, 8, 1))
    assert classes == {}
    assert len(ids) == 1  # the seeded dev edition still resolves
    result = calculate_employee_period(db_session, employee, JULY)
    daily = {item.day: item for item in result.daily_results}
    assert daily[date(2026, 7, 2)].required_minutes == 348


def test_WT_FLEX_001_holiday_and_employer_free_zero_required_but_keep_worked_minutes(
    db_client, db_session: Session, employee: Employee
) -> None:
    _publish(
        db_session,
        "july-mixed-v1",
        JULY,
        date(2026, 7, 31),
        {
            HOLIDAY: CalendarDayClass.PUBLIC_HOLIDAY,
            date(2026, 7, 11): CalendarDayClass.EMPLOYER_FREE,
        },
    )
    created = db_client.post(
        INTERVALS,
        headers=ME,
        json={
            "start_local": "2026-07-10T08:00:00",
            "end_local": "2026-07-10T16:00:00",
            "work_type": WorkType.ON_SITE.value,
        },
    )
    assert created.status_code == 201, created.text
    result = calculate_employee_period(db_session, employee, JULY)
    daily = {item.day: item for item in result.daily_results}
    assert daily[HOLIDAY].calendar_class is CalendarClass.PUBLIC_HOLIDAY
    assert daily[HOLIDAY].required_minutes == 0
    assert daily[HOLIDAY].worked_minutes == 480  # the work fact is untouched
    assert daily[date(2026, 7, 11)].required_minutes == 0
    assert daily[date(2026, 7, 11)].calendar_class is CalendarClass.EMPLOYER_FREE


# ---------------------------------------------------------------------------
# Edition / version / effective-date semantics
# ---------------------------------------------------------------------------


def test_WT_FLEX_001_greatest_valid_from_wins_and_is_not_a_version_string_tiebreak(
    db_session: Session,
) -> None:
    """Two editions, and the LATER-starting one wins even though its version
    string sorts first. ``ORDER BY valid_from DESC, version DESC`` made "which
    edition applies" string-dependent (EINV-04's class)."""
    _publish(
        db_session,
        "zzz-late",
        JULY,
        date(2026, 7, 31),
        {HOLIDAY: CalendarDayClass.PUBLIC_HOLIDAY},
    )
    _publish(
        db_session,
        "aaa-early",
        date(2026, 7, 5),
        date(2026, 7, 20),
        {HOLIDAY: CalendarDayClass.EMPLOYER_FREE},
    )
    classes, _ids = _classes(db_session, JULY, date(2026, 8, 1))
    assert classes[HOLIDAY] is CalendarClass.EMPLOYER_FREE


def test_WT_FLEX_001_exactly_one_edition_resolves_per_day_under_overlap(
    db_session: Session,
) -> None:
    """Overlap is precedence, never composition."""
    _publish(
        db_session,
        "overlap-base-v1",
        JULY,
        date(2026, 7, 31),
        {HOLIDAY: CalendarDayClass.PUBLIC_HOLIDAY},
    )
    _publish(
        db_session,
        "overlap-overlay-v1",
        date(2026, 7, 5),
        date(2026, 7, 20),
        {HOLIDAY: CalendarDayClass.EMPLOYER_FREE},
    )
    classes, ids = _classes(db_session, JULY, date(2026, 8, 1))
    assert set(classes) == {HOLIDAY}  # exactly one classification for that day
    assert classes[HOLIDAY] is CalendarClass.EMPLOYER_FREE
    assert len(ids) == len(set(ids))


def test_WT_FLEX_001_calendar_version_ids_are_sorted_deduplicated_and_row_order_independent(
    db_session: Session,
) -> None:
    _publish(
        db_session,
        "order-a-v1",
        JULY,
        date(2026, 7, 15),
        {date(2026, 7, 3): CalendarDayClass.PUBLIC_HOLIDAY},
    )
    _publish(db_session, "order-b-v1", date(2026, 7, 16), None)
    _classes_first, ids = _classes(db_session, JULY, date(2026, 8, 1))
    assert list(ids) == sorted(set(ids), key=str)
    # Re-reading the same calendar state yields the identical tuple.
    _classes_second, again = _classes(db_session, JULY, date(2026, 8, 1))
    assert again == ids


def test_WT_FLEX_001_ordinary_days_under_two_editions_still_carry_both_edition_ids(
    db_session: Session,
) -> None:
    """The governed-range identity case.

    A month straddling two editions where NEITHER edition lists an exception day
    must still resolve each day to its own edition and record BOTH ids in
    ``calendar_version_ids`` — and therefore in the input fingerprint. A naive
    "only record editions that contributed an exception row" implementation
    would silently drop the identity, which is exactly how the retired
    placeholder's divergence becomes undetectable.
    """
    first = _publish(db_session, "identity-a-v1", JULY, date(2026, 7, 15))
    second = _publish(db_session, "identity-b-v1", date(2026, 7, 16), None)
    classes, ids = _classes(db_session, JULY, date(2026, 8, 1))
    assert classes == {}  # no exception rows at all
    assert set(ids) == {first.id, second.id}


def test_WT_FLEX_001_a_month_straddling_two_editions_records_both_ids(db_session: Session) -> None:
    first = _publish(
        db_session,
        "straddle-a-v1",
        JULY,
        date(2026, 7, 15),
        {date(2026, 7, 3): CalendarDayClass.PUBLIC_HOLIDAY},
    )
    second = _publish(
        db_session,
        "straddle-b-v1",
        date(2026, 7, 16),
        None,
        {date(2026, 7, 20): CalendarDayClass.EMPLOYER_FREE},
    )
    classes, ids = _classes(db_session, JULY, date(2026, 8, 1))
    assert set(ids) == {first.id, second.id}
    assert classes[date(2026, 7, 3)] is CalendarClass.PUBLIC_HOLIDAY
    assert classes[date(2026, 7, 20)] is CalendarClass.EMPLOYER_FREE


def test_WT_FLEX_001_an_overlay_edition_un_classifies_a_day_without_mutating_the_predecessor(
    db_session: Session,
) -> None:
    predecessor = _publish(
        db_session,
        "overlay-base-v2",
        JULY,
        date(2026, 7, 31),
        {HOLIDAY: CalendarDayClass.PUBLIC_HOLIDAY},
    )
    _publish(db_session, "overlay-correction-v2", HOLIDAY, HOLIDAY)
    classes, _ids = _classes(db_session, JULY, date(2026, 8, 1))
    assert HOLIDAY not in classes  # the day is ordinary again
    # The predecessor's row is still there, untouched: editions are insert-only.
    row = db_session.scalar(
        select(CalendarDay).where(
            CalendarDay.calendar_version_id == predecessor.id, CalendarDay.day == HOLIDAY
        )
    )
    assert row is not None


def test_WT_FLEX_001_an_unbounded_overlay_is_refused(db_session: Session) -> None:
    _publish(db_session, "later-edition-v1", date(2026, 8, 1), None)
    with pytest.raises(CalendarEditionRefused) as excinfo:
        _publish(db_session, "unbounded-overlay-v1", JULY, None)
    assert "--to is required" in str(excinfo.value)


def test_WT_FLEX_001_an_edition_with_a_day_outside_its_range_is_refused(
    db_session: Session,
) -> None:
    with pytest.raises(CalendarEditionRefused) as excinfo:
        _publish(
            db_session,
            "outside-range-v1",
            JULY,
            date(2026, 7, 31),
            {date(2026, 8, 5): CalendarDayClass.PUBLIC_HOLIDAY},
        )
    assert "outside the edition's own range" in str(excinfo.value)


def test_WT_FLEX_001_republishing_an_identical_edition_is_a_no_op(db_session: Session) -> None:
    first = _publish(
        db_session,
        "idempotent-v1",
        JULY,
        date(2026, 7, 31),
        {HOLIDAY: CalendarDayClass.PUBLIC_HOLIDAY},
    )
    second = _publish(
        db_session,
        "idempotent-v1",
        JULY,
        date(2026, 7, 31),
        {HOLIDAY: CalendarDayClass.PUBLIC_HOLIDAY},
    )
    assert second.id == first.id
    assert (
        db_session.scalar(select(CalendarDay.id).where(CalendarDay.calendar_version_id == first.id))
        is not None
    )
    assert preview_calendar_edition(
        db_session,
        version="idempotent-v1",
        valid_from=JULY,
        valid_to=date(2026, 7, 31),
        source="TEST",
        days={HOLIDAY: CalendarDayClass.PUBLIC_HOLIDAY},
    ).no_op


def test_WT_FLEX_001_republishing_a_divergent_edition_is_refused_naming_the_days(
    db_session: Session,
) -> None:
    _publish(
        db_session,
        "divergent-v1",
        JULY,
        date(2026, 7, 31),
        {HOLIDAY: CalendarDayClass.PUBLIC_HOLIDAY},
    )
    with pytest.raises(CalendarEditionRefused) as excinfo:
        _publish(db_session, "divergent-v1", JULY, date(2026, 7, 31))
    assert "different day set" in str(excinfo.value)
    assert HOLIDAY.isoformat() in str(excinfo.value)


def test_WT_FLEX_001_publishing_a_narrowed_valid_to_is_impossible(db_session: Session) -> None:
    """Editions are insert-only: no path may narrow a published range.

    Narrowing under fail-closed coverage would punch a coverage gap and turn a
    local-looking edit into a global 409, so the only way to express a change is
    a new edition.
    """
    _publish(db_session, "narrow-v1", JULY, date(2026, 7, 31))
    with pytest.raises(CalendarEditionRefused) as excinfo:
        _publish(db_session, "narrow-v1", JULY, date(2026, 7, 15))
    assert "immutable" in str(excinfo.value)


def test_WT_FLEX_001_a_day_row_shadowed_by_a_later_edition_is_refused(db_session: Session) -> None:
    _publish(db_session, "shadow-later-v1", date(2026, 7, 20), None)
    with pytest.raises(CalendarEditionRefused) as excinfo:
        _publish(
            db_session,
            "shadow-earlier-v1",
            JULY,
            date(2026, 7, 31),
            {date(2026, 7, 25): CalendarDayClass.PUBLIC_HOLIDAY},
        )
    assert "can never win" in str(excinfo.value)


def test_WT_FLEX_001_a_blank_source_is_refused(db_session: Session) -> None:
    with pytest.raises(CalendarEditionRefused):
        publish_calendar_edition(
            db_session,
            calendar_key=CALENDAR_KEY,
            version="blank-source-v1",
            valid_from=JULY,
            valid_to=None,
            source="   ",
            days={},
            actor_principal="TEST",
        )


def test_WT_FLEX_001_the_month_view_reports_the_calendar_block(
    db_client, db_session: Session
) -> None:
    db_session.execute(delete(CalendarDay))
    db_session.execute(delete(CalendarVersion))
    db_session.flush()
    response = db_client.get(f"{MONTHS}/2026-07", headers=ME)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "CALENDAR_BLOCKED"


def test_WT_FLEX_001_a_published_edition_is_never_updated_in_place() -> None:
    """Structural guard: no application module emits UPDATE/DELETE on a calendar row.

    EINV-21 is "a published edition is immutable". The writer only INSERTs; the
    tripwire is deliberately narrow (an ``update(...)``/``delete(...)`` call whose
    target is one of the two calendar models) so it cannot produce false
    positives on unrelated tables. Behavioural proof lives in the overlay and
    republish tests above.
    """
    import ast
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in sorted((backend / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"update", "delete"}
            ):
                for argument in node.args:
                    if isinstance(argument, ast.Name) and argument.id in {
                        "CalendarVersion",
                        "CalendarDay",
                    }:
                        offenders.append(f"{path.name}:{node.lineno} {node.func.id}({argument.id})")
    assert offenders == []


def test_the_engine_receives_the_resolved_edition_ids_in_the_fingerprint(
    db_session: Session, employee: Employee
) -> None:
    """The calendar identity is a fingerprint anchor, not decoration."""
    before = calculate_employee_period(db_session, employee, JULY).input_fingerprint
    published = _publish(db_session, "fingerprint-v1", date(2026, 7, 16), None)
    after = calculate_employee_period(db_session, employee, JULY)
    assert str(published.id) in after.calendar_version_ids
    assert after.input_fingerprint != before


def test_the_intervals_endpoint_is_reachable_after_the_calendar_is_published(db_client) -> None:
    """Sanity: the seeded dev edition keeps the ordinary month path working."""
    response = db_client.get(f"{MONTHS}/2026-07", headers=ME)
    assert response.status_code == 200, response.text


def test_a_stored_instant_before_the_first_edition_still_fails_closed(
    db_session: Session, employee: Employee
) -> None:
    """The seeded edition starts 2026-01-01; December 2025 must not silently work."""
    from app.errors import PolicyEditionUnavailable

    with pytest.raises((CalendarUnavailable, PolicyEditionUnavailable)):
        calculate_employee_period(db_session, employee, date(2025, 12, 1))


def test_the_dev_edition_is_self_identifying(db_session: Session) -> None:
    """The seed declares no exceptions *inside a governed edition* — a data
    statement, unlike the retired placeholder which made missing coverage
    undetectable."""
    row = db_session.scalar(
        select(CalendarVersion).where(CalendarVersion.calendar_key == CALENDAR_KEY)
    )
    assert row is not None
    assert row.source == "DEV_SEED"
    assert row.version == "dev-no-exceptions"
    assert row.valid_to is None
    assert (
        db_session.scalar(select(CalendarDay.id).where(CalendarDay.calendar_version_id == row.id))
        is None
    )


def test_the_retired_placeholder_is_gone(db_session: Session) -> None:
    assert (
        db_session.scalar(
            select(CalendarVersion.id).where(
                CalendarVersion.calendar_key == CALENDAR_KEY,
                CalendarVersion.version == "default-v1",
            )
        )
        is None
    )


def test_an_interval_on_a_holiday_keeps_its_elapsed_minutes(
    db_client, db_session: Session, employee: Employee
) -> None:
    _publish(
        db_session,
        "elapsed-v1",
        JULY,
        date(2026, 7, 31),
        {HOLIDAY: CalendarDayClass.PUBLIC_HOLIDAY},
    )
    created = db_client.post(
        INTERVALS,
        headers=ME,
        json={
            "start_local": "2026-07-10T22:00:00",
            "end_local": "2026-07-11T06:00:00",
            "work_type": WorkType.ON_SITE.value,
        },
    )
    assert created.status_code == 201, created.text
    result = calculate_employee_period(db_session, employee, JULY)
    daily = {item.day: item for item in result.daily_results}
    assert daily[HOLIDAY].worked_minutes == 120
    assert daily[date(2026, 7, 11)].worked_minutes == 360


def test_a_cross_midnight_interval_is_not_reinterpreted_by_a_later_edition(
    db_session: Session, employee: Employee
) -> None:
    """A future-dated edition leaves an earlier month's inputs untouched."""
    from app.policy.service import _month_inputs

    before = _month_inputs(db_session, employee, JULY, ())
    _publish(db_session, "future-v1", date(2026, 9, 1), None)
    after = _month_inputs(db_session, employee, JULY, ())
    assert after.calendar_version_ids == before.calendar_version_ids
    assert dict(after.calendar_classes) == dict(before.calendar_classes)


def test_publishing_an_edition_does_not_touch_the_seeded_dev_edition(
    db_session: Session,
) -> None:
    seeded = db_session.scalar(
        select(CalendarVersion).where(CalendarVersion.version == "dev-no-exceptions")
    )
    assert seeded is not None
    before = (seeded.valid_from, seeded.valid_to, seeded.source)
    _publish(db_session, "adjacent-v1", date(2026, 9, 1), None)
    db_session.refresh(seeded)
    assert (seeded.valid_from, seeded.valid_to, seeded.source) == before


def test_the_coordination_lock_is_released_by_the_test_transaction(db_session: Session) -> None:
    """A publication inside one transaction does not wedge the next one."""
    _publish(db_session, "lock-a-v1", date(2026, 9, 1), None)
    _publish(db_session, "lock-b-v1", date(2026, 10, 1), None)


def test_the_legacy_csv_export_inherits_the_calendar_block(db_client, db_session: Session) -> None:
    db_session.execute(delete(CalendarDay))
    db_session.execute(delete(CalendarVersion))
    db_session.flush()
    response = db_client.get("/api/v1/me/reports/legacy/work-time-record?month=2026-07", headers=ME)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "CALENDAR_BLOCKED"


def test_the_calendar_classes_mapping_only_carries_exceptions(db_session: Session) -> None:
    _publish(
        db_session,
        "exceptions-only-v1",
        JULY,
        date(2026, 7, 31),
        {HOLIDAY: CalendarDayClass.PUBLIC_HOLIDAY},
    )
    classes, _ids = _classes(db_session, JULY, date(2026, 8, 1))
    assert set(classes) == {HOLIDAY}
    assert all(isinstance(day, date) for day in classes)


def test_the_policy_engine_never_performs_a_calendar_lookup() -> None:
    """ARCH-MOD-001: the pure engine reads frozen inputs and does no lookup."""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "policy" / "engine.py").read_text(
        encoding="utf-8"
    )
    assert "CalendarVersion" not in source
    assert "CalendarDay" not in source
    assert "Session" not in source


def test_a_stored_snapshot_keeps_the_retired_placeholder_id(db_session: Session) -> None:
    """EINV-07: the migration rewrites no frozen column.

    Asserted structurally here (there are no pre-F5 snapshots in a fresh test
    database); ``test_f5_migration.py`` proves the same property on real rows.
    """
    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0016_calendar_and_day_status.py"
    ).read_text(encoding="utf-8")
    assert "period_calculation_snapshot" in migration  # named, and deliberately untouched
    assert "UPDATE period_calculation_snapshot" not in migration
    assert "calendar_version_ids =" not in migration


def test_the_uncovered_error_carries_no_employee_data() -> None:
    exc = CalendarUnavailable(CALENDAR_KEY, HOLIDAY, chain_month=JULY)
    details = exc.details()
    assert set(details) == {"calendar_key", "first_uncovered_day", "chain_month"}
    assert details["first_uncovered_day"] == HOLIDAY.isoformat()


def test_an_interval_after_the_edition_range_end_is_uncovered(
    db_client, db_session: Session
) -> None:
    """A bounded edition that stops leaves the following days uncovered, not
    silently ordinary."""
    db_session.execute(delete(CalendarDay))
    db_session.execute(delete(CalendarVersion))
    db_session.flush()
    _publish(db_session, "bounded-only-v1", JULY, date(2026, 7, 31))
    assert db_client.get(f"{MONTHS}/2026-07", headers=ME).status_code == 200
    assert db_client.get(f"{MONTHS}/2026-08", headers=ME).status_code == 409


def test_a_calendar_day_class_is_exhaustive(db_session: Session) -> None:
    """Only the two supported classifications exist; adding a third is a code
    change, not a data accident."""
    assert {member.value for member in CalendarDayClass} == {"PUBLIC_HOLIDAY", "EMPLOYER_FREE"}


def test_the_interval_helper_used_above_is_utc_safe() -> None:
    assert datetime(2026, 7, 10, 8, tzinfo=UTC).tzinfo is not None


# ---------------------------------------------------------------------------
# Coverage derivation — the operator-facing form of the fail-closed rule
# ---------------------------------------------------------------------------


def _edition(valid_from: date, valid_to: date | None) -> CalendarVersion:
    """One published edition in memory.

    The coverage helpers are pure, so the rule is pinned here without a database;
    the API path that exposes them is covered by ``test_admin_api.py``.
    """
    return CalendarVersion(
        calendar_key=CALENDAR_KEY,
        version=f"v{valid_from:%Y%m%d}",
        valid_from=valid_from,
        valid_to=valid_to,
        source="TEST",
    )


def test_coverage_of_nothing_starts_at_the_month_asked_about() -> None:
    assert first_uncovered_month([], date(2026, 9, 1)) == date(2026, 9, 1)


def test_coverage_reports_the_month_after_a_bounded_edition_ends() -> None:
    editions = [_edition(date(2026, 1, 1), date(2026, 12, 31))]
    assert first_uncovered_month(editions, date(2026, 9, 1)) == date(2027, 1, 1)


def test_coverage_of_an_open_ended_edition_has_no_gap() -> None:
    """``None`` is "no gap from here on" — not a claim that coverage must be infinite."""
    editions = [_edition(date(2026, 1, 1), None)]
    assert first_uncovered_month(editions, date(2026, 9, 1)) is None


def test_adjacent_editions_are_one_continuous_coverage() -> None:
    """A year-boundary publication must not read as a gap."""
    editions = [
        _edition(date(2026, 1, 1), date(2026, 6, 30)),
        _edition(date(2026, 7, 1), date(2026, 12, 31)),
    ]
    assert first_uncovered_month(editions, date(2026, 1, 1)) == date(2027, 1, 1)


def test_a_corrective_overlay_inside_an_edition_changes_no_coverage() -> None:
    """Overlap is precedence: an overlay takes nothing away and is never an error."""
    base = _edition(date(2026, 1, 1), None)
    overlay = _edition(date(2026, 10, 1), date(2026, 10, 31))
    assert first_uncovered_month([base, overlay], date(2026, 9, 1)) is None
    assert first_uncovered_month([overlay, base], date(2026, 9, 1)) is None


def test_a_corrective_overlay_inside_a_bounded_edition_does_not_extend_it() -> None:
    editions = [
        _edition(date(2026, 1, 1), date(2026, 6, 30)),
        _edition(date(2026, 3, 1), date(2026, 3, 31)),
    ]
    assert first_uncovered_month(editions, date(2026, 1, 1)) == date(2026, 7, 1)


def test_a_gap_between_two_editions_reports_the_first_month_it_opens() -> None:
    editions = [
        _edition(date(2026, 1, 1), date(2026, 12, 31)),
        _edition(date(2027, 3, 1), date(2027, 12, 31)),
    ]
    assert first_uncovered_month(editions, date(2026, 9, 1)) == date(2027, 1, 1)


def test_coverage_that_lapsed_in_the_past_still_leaves_the_current_month_uncovered() -> None:
    """The cursor never moves backwards: a gap the operator is standing in is not
    hidden by coverage that ended months ago."""
    editions = [_edition(date(2026, 1, 1), date(2026, 3, 31))]
    assert first_uncovered_month(editions, date(2026, 9, 1)) == date(2026, 9, 1)


def test_an_edition_that_starts_in_the_future_leaves_today_uncovered() -> None:
    editions = [_edition(date(2027, 1, 1), date(2027, 12, 31))]
    assert first_uncovered_month(editions, date(2026, 9, 1)) == date(2026, 9, 1)


def test_a_partially_covered_month_is_the_first_uncovered_month() -> None:
    """One uncovered day blocks the WHOLE month view, so a mid-month end of
    coverage is reported as that month, not the next one."""
    editions = [_edition(date(2026, 1, 1), date(2026, 12, 15))]
    assert first_uncovered_month(editions, date(2026, 1, 1)) == date(2026, 12, 1)
