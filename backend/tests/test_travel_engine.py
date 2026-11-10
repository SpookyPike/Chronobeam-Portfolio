"""Golden TRAVEL-003/004 tests, including elapsed-instant DST boundaries."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.travel.engine import PassiveFact, TripFact, WorkFact, calculate_travel

BERLIN = ZoneInfo("Europe/Berlin")


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=BERLIN)


def test_travel_004_floor_boundaries() -> None:
    # 15h has no excess.  Every listed boundary follows excess // 4 exactly.
    for total, expected in ((899, 0), (900, 0), (901, 0), (903, 0), (904, 1), (960, 15)):
        start = instant("2026-05-15T00:00")
        end = start.replace(day=15 + total // (24 * 60), hour=(total // 60) % 24, minute=total % 60)
        outcome = calculate_travel(
            trips=(TripFact("trip", date(2026, 5, 15)),),
            passive_facts=(PassiveFact("trip", start, end, "ACTUAL"),),
            work_facts=(),
            required_minutes={},
        )
        assert outcome.trips[0].credit_minutes == expected


def test_topup_is_only_the_unclosed_weekday_deficit_and_excludes_sunday() -> None:
    outcome = calculate_travel(
        trips=(TripFact("trip", date(2026, 5, 15)),),
        passive_facts=(
            PassiveFact("trip", instant("2026-05-15T08:00"), instant("2026-05-15T12:00"), "ACTUAL"),
            PassiveFact("trip", instant("2026-05-16T08:00"), instant("2026-05-16T11:00"), "ACTUAL"),
            PassiveFact("trip", instant("2026-05-17T08:00"), instant("2026-05-17T14:00"), "ACTUAL"),
        ),
        work_facts=(WorkFact(instant("2026-05-15T08:00"), instant("2026-05-15T10:00")),),
        required_minutes={date(2026, 5, 15): 300, date(2026, 5, 16): 240, date(2026, 5, 17): 360},
    )
    assert [(item.day, item.minutes) for item in outcome.topups] == [
        (date(2026, 5, 15), 120),
        (date(2026, 5, 16), 180),
    ]


def test_work_on_train_is_subtracted_once_and_fictitious_is_passive_not_work() -> None:
    outcome = calculate_travel(
        trips=(TripFact("trip", date(2026, 5, 15)),),
        passive_facts=(
            PassiveFact(
                "trip",
                instant("2026-05-15T08:00"),
                instant("2026-05-15T16:00"),
                "FICTITIOUS_BUSINESS_ROUTE",
            ),
        ),
        work_facts=(
            WorkFact(instant("2026-05-15T10:00"), instant("2026-05-15T12:00")),
            WorkFact(instant("2026-05-15T10:00"), instant("2026-05-15T12:00")),
        ),
        required_minutes={date(2026, 5, 15): 480},
    )
    assert [(item.day, item.minutes) for item in outcome.topups] == [(date(2026, 5, 15), 360)]
    assert outcome.trips[0].eligible_minutes == 360


def test_trip_windows_and_cross_midnight_dst_are_elapsed_and_calendar_safe() -> None:
    fallback = calculate_travel(
        trips=(TripFact("fall", date(2026, 10, 25)),),
        passive_facts=(
            PassiveFact("fall", instant("2026-10-25T00:00"), instant("2026-10-26T00:00"), "ACTUAL"),
        ),
        work_facts=(),
        required_minutes={date(2026, 10, 25): 480},
    )
    # Fall-back Sunday contains 25 elapsed hours but receives no Sunday top-up.
    assert fallback.trips[0].eligible_minutes == 25 * 60
    spring = calculate_travel(
        trips=(TripFact("spring", date(2026, 3, 28)),),
        passive_facts=(
            PassiveFact(
                "spring", instant("2026-03-28T23:00"), instant("2026-03-29T03:00"), "ACTUAL"
            ),
        ),
        work_facts=(),
        required_minutes={date(2026, 3, 28): 480, date(2026, 3, 29): 480},
    )
    assert [(x.day, x.minutes) for x in spring.topups] == [(date(2026, 3, 28), 60)]
    dec = calculate_travel(
        trips=(TripFact("dec", date(2026, 12, 31)),),
        passive_facts=(
            PassiveFact("dec", instant("2027-01-30T00:00"), instant("2027-01-30T16:00"), "ACTUAL"),
        ),
        work_facts=(),
        required_minutes={},
    )
    assert dec.trips[0].window_end == date(2027, 1, 31)
    assert dec.trips[0].credit_effective_on == date(2027, 2, 1)


def test_passive_union_does_not_multiply_and_credit_is_monotonic() -> None:
    trips = (TripFact("a", date(2026, 5, 1)), TripFact("b", date(2026, 5, 1)))
    shared = PassiveFact("a", instant("2026-05-01T00:00"), instant("2026-05-01T16:00"), "ACTUAL")
    overlap = PassiveFact("b", instant("2026-05-01T00:00"), instant("2026-05-01T16:00"), "ACTUAL")
    outcome = calculate_travel(
        trips=trips, passive_facts=(shared, overlap), work_facts=(), required_minutes={}
    )
    assert sum(item.eligible_minutes for item in outcome.trips) == 960
    previous = 0
    for minutes in range(900, 1_201, 7):
        start = instant("2026-05-01T00:00")
        end = datetime.fromtimestamp(start.timestamp() + minutes * 60, tz=BERLIN)
        current = (
            calculate_travel(
                trips=(TripFact("trip", date(2026, 5, 1)),),
                passive_facts=(PassiveFact("trip", start, end, "ACTUAL"),),
                work_facts=(),
                required_minutes={},
            )
            .trips[0]
            .credit_minutes
        )
        assert current >= previous
        previous = current


def test_may31_source_example_window_clamps_to_june30_and_credits_july() -> None:
    # A trip starting on May 31 belongs to the window that ends June 30: 16:00 of
    # non-credited travel yields 15 minutes (the ``POL-08`` worked example),
    # effective at the start of the month after the window.
    start = instant("2026-05-31T00:00")
    outcome = calculate_travel(
        trips=(TripFact("may31", date(2026, 5, 31)),),
        passive_facts=(PassiveFact("may31", start, instant("2026-05-31T16:00"), "ACTUAL"),),
        work_facts=(),
        required_minutes={},
    )
    trip = outcome.trips[0]
    assert trip.window_end == date(2026, 6, 30)
    assert trip.eligible_minutes == 960
    assert trip.excess_minutes == 60
    assert trip.credit_minutes == 15
    assert trip.credit_effective_on == date(2026, 7, 1)


def test_may15_example_window_until_june15_credit_july() -> None:
    start = instant("2026-05-15T00:00")
    outcome = calculate_travel(
        trips=(TripFact("may15", date(2026, 5, 15)),),
        passive_facts=(PassiveFact("may15", start, instant("2026-05-15T16:00"), "ACTUAL"),),
        work_facts=(),
        required_minutes={},
    )
    trip = outcome.trips[0]
    assert trip.window_end == date(2026, 6, 15)
    assert trip.credit_minutes == 15
    assert trip.credit_effective_on == date(2026, 7, 1)


def test_sunday_findings_authorization_coverage_and_driving_warning() -> None:
    sunday_work = (
        WorkFact(instant("2026-05-17T10:00"), instant("2026-05-17T12:00"), "ACTIVE_WORK"),
    )
    uncovered = calculate_travel(
        trips=(TripFact("t", date(2026, 5, 17)),),
        passive_facts=(),
        work_facts=sunday_work,
        required_minutes={},
    )
    rules = {finding.rule_id for finding in uncovered.findings}
    assert "TRAVEL-SUNDAY-001" in rules  # prior overtime application missing
    assert "TRAVEL-LOCAL-001" in rules  # host-country legality stays with the employee

    covered = calculate_travel(
        trips=(TripFact("t", date(2026, 5, 17)),),
        passive_facts=(),
        work_facts=sunday_work,
        required_minutes={},
        authorization_windows=((instant("2026-05-17T09:00"), instant("2026-05-17T13:00")),),
    )
    assert "TRAVEL-SUNDAY-001" not in {finding.rule_id for finding in covered.findings}

    partial = calculate_travel(
        trips=(TripFact("t", date(2026, 5, 17)),),
        passive_facts=(),
        work_facts=sunday_work,
        required_minutes={},
        authorization_windows=((instant("2026-05-17T10:30"), instant("2026-05-17T12:00")),),
    )
    assert "TRAVEL-SUNDAY-001" in {finding.rule_id for finding in partial.findings}

    driving = calculate_travel(
        trips=(TripFact("t", date(2026, 5, 17)),),
        passive_facts=(),
        work_facts=(WorkFact(instant("2026-05-17T10:00"), instant("2026-05-17T12:00"), "DRIVING"),),
        required_minutes={},
        authorization_windows=((instant("2026-05-17T09:00"), instant("2026-05-17T13:00")),),
    )
    # Authorized or not, Sunday DRIVING is working time under ArbZG and stays warned.
    assert "TRAVEL-SUNDAY-002" in {finding.rule_id for finding in driving.findings}


def test_ordinary_sunday_work_emits_no_travel_findings() -> None:
    """Reviewer F2: work facts marked non-travel never raise travel findings,
    though they still subtract from passive top-up/credit arithmetic."""
    ordinary = WorkFact(
        instant("2026-05-17T10:00"), instant("2026-05-17T12:00"), "ON_SITE", is_travel=False
    )
    outcome = calculate_travel(
        trips=(TripFact("t", date(2026, 5, 17)),),
        passive_facts=(
            PassiveFact("t", instant("2026-05-17T08:00"), instant("2026-05-17T14:00"), "ACTUAL"),
        ),
        work_facts=(ordinary,),
        required_minutes={},
    )
    assert outcome.findings == ()
    # The ordinary work still counts as work for the passive subtraction:
    # 6:00 passive minus the 2:00 ordinary work = 4:00 eligible.
    assert outcome.trips[0].eligible_minutes == 4 * 60


def test_EINV_17_travel_day_split_preserves_elapsed_minutes() -> None:
    """EINV-17 sibling: the travel day split must not lose elapsed time.

    ``_split_days`` feeds both the daily top-up and the TRAVEL-004 credit's
    ``eligible_minutes``, and it used to floor each day's portion
    independently — so a passive span crossing midnight with sub-minute
    fragments on both sides lost the whole minute, and a credit could be
    computed from less time than the employee actually declared.
    """
    start = datetime(2032, 3, 31, 23, 59, 30, tzinfo=BERLIN)
    end = datetime(2032, 4, 1, 0, 0, 30, tzinfo=BERLIN)
    outcome = calculate_travel(
        trips=(TripFact("t", date(2032, 3, 31)),),
        passive_facts=(PassiveFact("t", start, end, "ACTUAL"),),
        work_facts=(),
        required_minutes={},
    )
    # One completed minute of declared passive travel; the credit window is
    # assessed from the same split, so the eligible total must see it too.
    assert outcome.trips[0].eligible_minutes == 1


def test_EINV_17_travel_split_attributes_a_straddling_minute_by_completion() -> None:
    """The same frozen convention as the ordinary-time engine.

    A slot belongs to the day containing its completion instant, so
    ``23:59:30 -> 00:00:30`` credits April 1 with the single completed minute.
    Asserted through the top-up, which is the day-keyed output of the split.
    """
    start = datetime(2032, 3, 31, 23, 59, 30, tzinfo=BERLIN)
    end = datetime(2032, 4, 1, 0, 0, 30, tzinfo=BERLIN)
    outcome = calculate_travel(
        trips=(TripFact("t", date(2032, 3, 31)),),
        passive_facts=(PassiveFact("t", start, end, "ACTUAL"),),
        work_facts=(),
        required_minutes={date(2032, 3, 31): 480, date(2032, 4, 1): 480},
    )
    topups = {item.day: item.minutes for item in outcome.topups}
    assert topups.get(date(2032, 3, 31), 0) == 0
    assert topups.get(date(2032, 4, 1), 0) == 1
