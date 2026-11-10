"""Explicit factual breaks (WT-BACKEND §5.3 ``break_interval``).

Audit finding **M1**: the spec's break
table had no implementation, so a paid/unpaid statutory shift break (SHIFT-003)
could not be recorded at all — the shift report could only say
``EXPLICIT_FACT_REQUIRED``.

The engine half pins the only policy distinction the sources actually state: an
``UNPAID`` break reduces worked time, a ``PAID_SHIFT_BREAK`` is compensated
presence and does not, and ``OTHER`` must not tighten the result either
(``INV-15``). The command half pins the surface: idempotency, version chain,
month reopen, the disclosed-fact delete guard and employee-privacy.
"""

import uuid
from datetime import date, datetime

from app.calendar.service import publish_calendar_edition
from app.policy.engine import (
    BERLIN,
    BreakFact,
    CalendarClass,
    IntervalFact,
    PeriodInputs,
    calculate_period,
)
from app.worktime.models import BreakInterval, Correction
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.conftest import DEVELOPER_PRINCIPAL_HEADER as ME
from tests.conftest import HR_HEADER
from tests.conftest import OTHER_EMPLOYEE_HEADER as ME_B

BREAKS = "/api/v1/me/break-intervals"
INTERVALS = "/api/v1/me/work-intervals"
MONTHS = "/api/v1/me/months"
CALENDAR_KEY = "primary"

DAY = date(2032, 9, 7)
WORK_START = datetime(2032, 9, 7, 8, 0, tzinfo=BERLIN)
WORK_END = datetime(2032, 9, 7, 16, 0, tzinfo=BERLIN)  # 480 minutes
BREAK_START = datetime(2032, 9, 7, 12, 0, tzinfo=BERLIN)
BREAK_END = datetime(2032, 9, 7, 12, 30, tzinfo=BERLIN)  # 30 minutes


# ---------------------------------------------------------------------------
# Pure engine: the UNPAID / compensated distinction (SHIFT-003)
# ---------------------------------------------------------------------------


def _inputs(intervals: list[IntervalFact], breaks: list[BreakFact] | None = None) -> PeriodInputs:
    return PeriodInputs(
        period_start=date(2032, 9, 1),
        period_end=date(2032, 10, 1),
        daily_required_minutes={DAY: 0},
        weekly_required_minutes=0,
        calendar_classes={DAY: CalendarClass.WEEKDAY},
        intervals=tuple(intervals),
        opening_ledger_entries=(),
        policy_version="phase-2-ordinary-v1",
        engine_build="ordinary-time-v1",
        breaks=tuple(breaks or []),
    )


def _day_worked(result, day: date = DAY) -> int:
    return next(item for item in result.daily_results if item.day == day).worked_minutes


def test_an_unpaid_break_reduces_worked_time() -> None:
    span = [IntervalFact("i1", WORK_START, WORK_END, "SHIFT")]
    assert _day_worked(calculate_period(_inputs(span))) == 480
    assert (
        _day_worked(
            calculate_period(_inputs(span, [BreakFact("b1", BREAK_START, BREAK_END, "UNPAID")]))
        )
        == 450
    )


def test_a_compensated_break_is_recorded_but_not_deducted() -> None:
    """SHIFT-003: a paid statutory shift break is compensated presence.

    ``OTHER`` states no unpaid semantics in the sources, so it must not deduct
    either — deducting it would be stricter than the regime (``INV-15``).
    """
    span = [IntervalFact("i1", WORK_START, WORK_END, "SHIFT")]
    for kind in ("PAID_SHIFT_BREAK", "OTHER"):
        assert (
            _day_worked(
                calculate_period(_inputs(span, [BreakFact("b1", BREAK_START, BREAK_END, kind)]))
            )
            == 480
        ), kind


def test_a_break_inside_a_gap_changes_nothing() -> None:
    """A gap between intervals is already non-work; a break over it is not a
    second deduction (the double-count this rule must not introduce)."""
    split = [
        IntervalFact("i1", WORK_START, BREAK_START, "SHIFT"),
        IntervalFact("i2", BREAK_END, WORK_END, "SHIFT"),
    ]
    without = calculate_period(_inputs(split))
    with_break = calculate_period(
        _inputs(split, [BreakFact("b1", BREAK_START, BREAK_END, "UNPAID")])
    )
    assert _day_worked(without) == 450
    assert _day_worked(with_break) == 450


def test_a_break_reaches_the_input_fingerprint_only_when_present() -> None:
    """The fingerprint must gain the ``breaks`` key ONLY when a break exists, so
    every pre-existing month fingerprint/snapshot stays byte-stable (``INV-11``).

    The key's absence is structural (the ``if inputs.breaks:`` guard), so it is
    asserted against the source — the same tripwire pattern the repository uses
    for other structural guards.
    """
    import inspect

    from app.policy import engine

    assert "if inputs.breaks:" in inspect.getsource(engine._fingerprint)

    span = [IntervalFact("i1", WORK_START, WORK_END, "SHIFT")]
    without = calculate_period(_inputs(span)).input_fingerprint
    with_break = calculate_period(
        _inputs(span, [BreakFact("b1", BREAK_START, BREAK_END, "UNPAID")])
    ).input_fingerprint
    assert without != with_break
    assert calculate_period(_inputs(span)).input_fingerprint == without


# ---------------------------------------------------------------------------
# Statutory rules must SEE a recorded break (review finding: the rules were
# evaluated from interval spans, so recording the very fact a rule is about
# changed nothing and the employee kept a false CRITICAL finding)
# ---------------------------------------------------------------------------


def _rule_ids(result) -> set[str]:
    return {finding.rule_id for finding in result.findings}


def _qualifying(result, day: date = DAY) -> int:
    return next(item for item in result.daily_results if item.day == day).qualifying_break_minutes


def test_the_six_hour_break_rule_accepts_a_recorded_break() -> None:
    """WT-CORE-003: >6 h of work requires 30 minutes of qualifying break.

    Without the break the span has no gap, so the requirement is unmet; the
    recorded break is the ONLY thing that can satisfy it.
    """
    span = [IntervalFact("i1", WORK_START, WORK_END, "SHIFT")]
    assert "WT-CORE-003" in _rule_ids(calculate_period(_inputs(span)))
    assert "WT-CORE-003" not in _rule_ids(
        calculate_period(_inputs(span, [BreakFact("b1", BREAK_START, BREAK_END, "UNPAID")]))
    )


def test_the_uninterrupted_run_rule_accepts_a_recorded_break() -> None:
    """WT-CORE-005: work continued >6 h without a qualifying break."""
    span = [IntervalFact("i1", WORK_START, WORK_END, "SHIFT")]
    assert "WT-CORE-005" in _rule_ids(calculate_period(_inputs(span)))
    assert "WT-CORE-005" not in _rule_ids(
        calculate_period(_inputs(span, [BreakFact("b1", BREAK_START, BREAK_END, "UNPAID")]))
    )


def test_a_short_break_does_not_satisfy_the_six_hour_rule() -> None:
    """A break below the qualifying minimum must NOT buy the requirement off.

    This is the negative control for the two tests above: they would also pass
    if any break were credited blindly.
    """
    short = BreakFact("b1", BREAK_START, BREAK_START.replace(minute=10), "UNPAID")
    result = calculate_period(_inputs([IntervalFact("i1", WORK_START, WORK_END, "SHIFT")], [short]))
    assert "WT-CORE-003" in _rule_ids(result)
    assert "WT-CORE-005" in _rule_ids(result)
    # A sub-minimum break is not a QUALIFYING break at all — it is reported as 0,
    # exactly as a 10-minute gap between two intervals would be.
    assert _qualifying(result) == 0


def test_a_recorded_break_lowers_the_daily_maximum_measure() -> None:
    """WT-CORE-006 measures WORKING time, and a break is not working time.

    10 h 30 min of span is above the 10 h maximum; the same span with a 30-minute
    break is exactly at it (the rule is strictly greater), so the finding is the
    discriminator.
    """
    end = WORK_START.replace(hour=18, minute=30)
    span = [IntervalFact("i1", WORK_START, end, "SHIFT")]
    assert "WT-CORE-006" in _rule_ids(calculate_period(_inputs(span)))
    assert "WT-CORE-006" not in _rule_ids(
        calculate_period(_inputs(span, [BreakFact("b1", BREAK_START, BREAK_END, "UNPAID")]))
    )


def test_a_compensated_break_interrupts_work_without_reducing_credited_time() -> None:
    """The two layers, in one test (ArbZG §2 vs SHIFT-003).

    A Ruhepause is not working time whatever it pays, so a PAID_SHIFT_BREAK ends
    the statutory break requirement and the uninterrupted run — while the day's
    CREDITED minutes stay at the full span, because a paid shift break is
    remunerated presence.
    """
    span = [IntervalFact("i1", WORK_START, WORK_END, "SHIFT")]
    paid = [BreakFact("b1", BREAK_START, BREAK_END, "PAID_SHIFT_BREAK")]
    result = calculate_period(_inputs(span, paid))
    assert "WT-CORE-003" not in _rule_ids(result)
    assert "WT-CORE-005" not in _rule_ids(result)
    assert _qualifying(result) == 30
    assert _day_worked(result) == 480


def test_the_day_reports_a_recorded_break_as_qualifying() -> None:
    """``qualifying_break_minutes`` must count the employee's own fact, or the
    day view would show 0 break minutes next to a recorded 30-minute break."""
    span = [IntervalFact("i1", WORK_START, WORK_END, "SHIFT")]
    assert _qualifying(calculate_period(_inputs(span))) == 0
    assert (
        _qualifying(
            calculate_period(_inputs(span, [BreakFact("b1", BREAK_START, BREAK_END, "UNPAID")]))
        )
        == 30
    )


def test_a_break_in_a_gap_is_not_counted_twice() -> None:
    """A break lying in the gap between two intervals adds nothing: that time is
    already outside the work, so the gap term alone must supply the 30 minutes."""
    first = IntervalFact("i1", WORK_START, WORK_START.replace(hour=12), "SHIFT")
    second = IntervalFact("i2", WORK_START.replace(hour=13), WORK_START.replace(hour=16), "SHIFT")
    # The gap is 12:00-13:00 (60 min) and the break sits wholly inside it.
    in_gap = BreakFact("b1", BREAK_START, BREAK_END, "UNPAID")
    without = calculate_period(_inputs([first, second]))
    with_break = calculate_period(_inputs([first, second], [in_gap]))
    assert _qualifying(without) == 60
    assert _qualifying(with_break) == 60
    assert _day_worked(with_break) == _day_worked(without) == 420


# ---------------------------------------------------------------------------
# Command surface
# ---------------------------------------------------------------------------


def _create(
    client: TestClient,
    start_local: str = "2032-09-07T12:00:00",
    end_local: str = "2032-09-07T12:30:00",
    kind: str = "UNPAID",
    **extra: object,
) -> dict:
    response = client.post(
        BREAKS,
        headers=ME,
        json={"start_local": start_local, "end_local": end_local, "kind": kind, **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _publish_calendar(session: Session) -> None:
    publish_calendar_edition(
        session,
        calendar_key=CALENDAR_KEY,
        version="p0-breaks",
        valid_from=date(2032, 1, 1),
        valid_to=None,
        source="TEST",
        days={},
        actor_principal="TEST",
    )


def test_create_break_is_idempotent_on_client_request_id(db_client: TestClient) -> None:
    key = str(uuid.uuid4())
    first = _create(db_client, client_request_id=key)
    replay = db_client.post(
        BREAKS,
        headers=ME,
        json={
            "start_local": "2032-09-07T12:00:00",
            "end_local": "2032-09-07T12:30:00",
            "kind": "UNPAID",
            "client_request_id": key,
        },
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == first["id"]


def test_overlapping_breaks_are_rejected_but_adjacent_is_allowed(
    db_client: TestClient,
) -> None:
    _create(db_client)
    overlap = db_client.post(
        BREAKS,
        headers=ME,
        json={
            "start_local": "2032-09-07T12:15:00",
            "end_local": "2032-09-07T12:45:00",
            "kind": "UNPAID",
        },
    )
    assert overlap.status_code == 422
    assert overlap.json()["detail"]["code"] == "VALIDATION_INTERVAL_OVERLAP"
    adjacent = db_client.post(
        BREAKS,
        headers=ME,
        json={
            "start_local": "2032-09-07T12:30:00",
            "end_local": "2032-09-07T12:45:00",
            "kind": "UNPAID",
        },
    )
    assert adjacent.status_code == 201, adjacent.text


def test_a_break_may_sit_inside_a_work_interval(db_client: TestClient) -> None:
    """That is the whole point of the table: a break a gap cannot express."""
    created_interval = db_client.post(
        INTERVALS,
        headers=ME,
        json={
            "start_local": "2032-09-07T08:00:00",
            "end_local": "2032-09-07T16:00:00",
            "work_type": "SHIFT",
        },
    )
    assert created_interval.status_code == 201, created_interval.text
    assert _create(db_client)["kind"] == "UNPAID"


def test_correction_supersedes_with_lineage_and_refuses_a_stale_version(
    db_client: TestClient, db_session: Session
) -> None:
    created = _create(db_client)
    patched = db_client.patch(
        f"{BREAKS}/{created['id']}",
        headers=ME,
        json={"version": 1, "kind": "PAID_SHIFT_BREAK"},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["version"] == 2
    assert body["supersedes_id"] == created["id"]
    assert body["kind"] == "PAID_SHIFT_BREAK"
    lineage = db_session.scalars(
        select(Correction).where(Correction.entity_type == "BREAK_INTERVAL")
    ).all()
    assert [(row.old_version, row.new_version) for row in lineage] == [(1, 2)]

    stale = db_client.patch(
        f"{BREAKS}/{created['id']}", headers=ME, json={"version": 1, "kind": "UNPAID"}
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "CONFLICT_VERSION"


def test_delete_removes_the_whole_version_chain(db_client: TestClient, db_session: Session) -> None:
    created = _create(db_client)
    patched = db_client.patch(
        f"{BREAKS}/{created['id']}", headers=ME, json={"version": 1, "kind": "OTHER"}
    )
    assert patched.status_code == 200, patched.text
    # Delete targets the CURRENT head (a superseded id is a stale target, 409),
    # exactly like the interval command.
    deleted = db_client.delete(
        f"{BREAKS}/{patched.json()['id']}", headers=ME, params={"version": 2}
    )
    assert deleted.status_code == 204, deleted.text
    # Removing only the head would resurrect the predecessor as current, so the
    # whole chain must be gone.
    assert db_session.scalars(select(BreakInterval)).all() == []


def test_a_break_is_private_to_its_owner(db_client: TestClient) -> None:
    created = _create(db_client)
    # A foreign id is indistinguishable from a nonexistent one (404), never 403.
    assert (
        db_client.patch(
            f"{BREAKS}/{created['id']}", headers=ME_B, json={"version": 1, "kind": "UNPAID"}
        ).status_code
        == 404
    )
    assert (
        db_client.delete(
            f"{BREAKS}/{created['id']}", headers=ME_B, params={"version": 1}
        ).status_code
        == 404
    )
    # HR never reaches the employee-only surface.
    denied = db_client.post(
        BREAKS,
        headers=HR_HEADER,
        json={
            "start_local": "2032-09-07T12:00:00",
            "end_local": "2032-09-07T12:30:00",
            "kind": "UNPAID",
        },
    )
    assert denied.status_code == 403


def test_day_detail_reports_the_breaks(db_client: TestClient) -> None:
    created = _create(db_client)
    detail = db_client.get(f"{MONTHS}/2032-09/days/2032-09-07", headers=ME)
    assert detail.status_code == 200, detail.text
    assert [item["id"] for item in detail.json()["breaks"]] == [created["id"]]


def test_month_view_deducts_an_unpaid_break(db_client: TestClient, db_session: Session) -> None:
    _publish_calendar(db_session)
    db_client.post(
        INTERVALS,
        headers=ME,
        json={
            "start_local": "2032-09-07T08:00:00",
            "end_local": "2032-09-07T16:00:00",
            "work_type": "SHIFT",
        },
    )
    before = db_client.get(f"{MONTHS}/2032-09", headers=ME)
    assert before.status_code == 200, before.text
    worked_before = _month_worked(before.json())
    assert worked_before == 480

    _create(db_client)
    after = db_client.get(f"{MONTHS}/2032-09", headers=ME)
    assert after.status_code == 200, after.text
    assert _month_worked(after.json()) == 450


def _month_worked(body: dict) -> int:
    return next(
        item["worked_minutes"] for item in body["daily_results"] if item["date"] == "2032-09-07"
    )


def test_a_break_link_must_be_the_employees_own_assignment(db_client: TestClient) -> None:
    """The link is what SHIFT-003's ``paid_break_status`` is resolved from.

    A dangling or foreign assignment id would let a stored fact claim a
    relationship that does not exist, so it is refused at the trust boundary
    rather than persisted. A nonexistent id and another employee's id are
    deliberately indistinguishable.
    """
    response = db_client.post(
        BREAKS,
        headers=ME,
        json={
            "start_local": "2032-09-07T12:00:00",
            "end_local": "2032-09-07T12:30:00",
            "kind": "PAID_SHIFT_BREAK",
            "linked_shift_assignment_id": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "VALIDATION_FAILED"


def _period_rule_ids(db_client: TestClient, month: str) -> set[str]:
    response = db_client.get(f"/api/v1/me/findings?period={month}", headers=ME)
    assert response.status_code == 200, response.text
    return {item["rule_id"] for item in response.json()["findings"]}


def test_a_cross_month_break_reaches_the_next_months_calculation(
    db_client: TestClient, db_session: Session
) -> None:
    """A break is a CALCULATION INPUT for every month whose days it touches.

    The statutory rules judge a canonical work GROUP, and a group may cross a
    month boundary: a 20:00-03:00 shift starting on 30 September is one group
    judged in October too. Windowing breaks to the month alone therefore judged
    that group as seven hours of unbroken work and emitted false WT-CORE-003 /
    WT-CORE-005 findings about a break the employee had already recorded.
    """
    _publish_calendar(db_session)
    created = db_client.post(
        INTERVALS,
        headers=ME,
        json={
            "start_local": "2032-09-30T20:00:00",
            "end_local": "2032-10-01T03:00:00",
            "work_type": "SHIFT",
        },
    )
    assert created.status_code == 201, created.text
    # Without the break the cross-month group is unbroken work: the findings are
    # real, and the recorded break is the only fact that can retire them.
    assert {"WT-CORE-003", "WT-CORE-005"} <= _period_rule_ids(db_client, "2032-10")

    assert _create(db_client, start_local="2032-09-30T23:00:00", end_local="2032-09-30T23:30:00")
    assert not {"WT-CORE-003", "WT-CORE-005"} & _period_rule_ids(db_client, "2032-10")


def test_a_correction_honours_an_explicit_first_occurrence_fold(db_client: TestClient) -> None:
    """``start_fold``/``end_fold`` are per-boundary, and ``0`` is a REAL value.

    A break may straddle the fall-back hour (02:15 CEST to 02:45 CET), which one
    shared ``fold`` cannot describe. Reading an explicit ``0`` as "absent" and
    falling back to ``fold`` would silently move the start into the second
    occurrence and halve the break — so the duration is the discriminator.
    """
    created = _create(db_client)
    patched = db_client.patch(
        f"{BREAKS}/{created['id']}",
        headers=ME,
        json={
            "version": 1,
            "start_local": "2032-10-31T02:15:00",
            "end_local": "2032-10-31T02:45:00",
            "start_fold": 0,
            "end_fold": 1,
        },
    )
    assert patched.status_code == 200, patched.text
    # 02:15 CEST (00:15Z) -> 02:45 CET (01:45Z) is 90 minutes, not 30.
    assert patched.json()["elapsed_seconds"] == 90 * 60


def test_a_late_break_does_not_erase_a_completed_long_run() -> None:
    """WT-CORE-005: a break AFTER a long run must not hide that run.

    Regression for the run-reset ordering bug: 08:00-15:00 is a seven-hour
    uninterrupted run, and the 30-minute gap before 15:30-16:30 satisfies the
    aggregate break requirement (WT-CORE-003), so only the run rule can catch it.
    A previous implementation reset the block on the gap and then measured the
    16:30 tail, which is under the threshold, and reported nothing.
    """
    first = IntervalFact(
        "i1",
        WORK_START,
        datetime(2032, 9, 7, 15, 0, tzinfo=BERLIN),
        "SHIFT",
    )
    second = IntervalFact(
        "i2",
        datetime(2032, 9, 7, 15, 30, tzinfo=BERLIN),
        datetime(2032, 9, 7, 16, 30, tzinfo=BERLIN),
        "SHIFT",
    )
    result = calculate_period(_inputs([first, second]))
    assert _qualifying(result) == 30  # the aggregate requirement is met ...
    assert "WT-CORE-003" not in _rule_ids(result)  # ... so this rule stays silent
    assert "WT-CORE-005" in _rule_ids(result)  # ... and the run rule must speak


def test_two_short_runs_with_qualifying_breaks_are_not_flagged() -> None:
    """Negative control for the test above: short runs stay silent."""
    first = IntervalFact("i1", WORK_START, datetime(2032, 9, 7, 11, 0, tzinfo=BERLIN), "SHIFT")
    second = IntervalFact(
        "i2",
        datetime(2032, 9, 7, 11, 30, tzinfo=BERLIN),
        datetime(2032, 9, 7, 14, 0, tzinfo=BERLIN),
        "SHIFT",
    )
    result = calculate_period(_inputs([first, second]))
    assert "WT-CORE-005" not in _rule_ids(result)
