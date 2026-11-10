"""Europe/Berlin wall-time resolution, DST cases (DATA-009/010).

The four discriminating instants were verified numerically:
2026-03-29 02:30 = nonexistent (spring-forward),
2026-10-25 02:30 = ambiguous (fall-back), 01:30/03:30 on both dates normal.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app.worktime.walltime import AMBIGUOUS, NONEXISTENT, DstResolutionError, resolve_wall_time


def test_DATA_009_spring_forward_nonexistent_time_rejected() -> None:
    with pytest.raises(DstResolutionError) as excinfo:
        resolve_wall_time(datetime(2026, 3, 29, 2, 30))
    assert excinfo.value.kind == NONEXISTENT


def test_DATA_010_fall_back_ambiguous_time_requires_fold() -> None:
    with pytest.raises(DstResolutionError) as excinfo:
        resolve_wall_time(datetime(2026, 10, 25, 2, 30))
    assert excinfo.value.kind == AMBIGUOUS


def test_DATA_010_fall_back_fold_resolves_both_occurrences() -> None:
    first = resolve_wall_time(datetime(2026, 10, 25, 2, 30), fold=0)
    second = resolve_wall_time(datetime(2026, 10, 25, 2, 30), fold=1)
    # fold=0 -> CEST (+02:00), fold=1 -> CET (+01:00): exactly one hour apart.
    # (Note: subtracting the two Berlin-aware datetimes directly would return
    # 0 because Python ignores fold for same-tzinfo arithmetic — the service
    # normalizes to UTC; assert utcoffset + UTC-normalized delta here.)
    assert first.utcoffset() == timedelta(hours=2)
    assert second.utcoffset() == timedelta(hours=1)
    assert (second.astimezone(UTC) - first.astimezone(UTC)).total_seconds() == 3600


@pytest.mark.parametrize(
    ("naive", "expected_utc"),
    [
        # 2026 DST transitions: spring-forward 29 March, fall-back 25 October.
        (datetime(2026, 3, 29, 1, 30), datetime(2026, 3, 29, 0, 30, tzinfo=UTC)),
        (datetime(2026, 3, 29, 3, 30), datetime(2026, 3, 29, 1, 30, tzinfo=UTC)),
        (datetime(2026, 10, 25, 1, 30), datetime(2026, 10, 24, 23, 30, tzinfo=UTC)),
        (datetime(2026, 10, 25, 3, 30), datetime(2026, 10, 25, 2, 30, tzinfo=UTC)),
        (datetime(2026, 7, 1, 8, 0), datetime(2026, 7, 1, 6, 0, tzinfo=UTC)),
        (datetime(2026, 12, 24, 23, 59), datetime(2026, 12, 24, 22, 59, tzinfo=UTC)),
    ],
)
def test_unambiguous_wall_times_resolve(naive: datetime, expected_utc: datetime) -> None:
    """F8.1 — pin the UTC INSTANT, not `resolved.astimezone() == resolved`.

    The previous assertion compared two aware datetimes, which Python compares by
    instant, so it was true for any zone and could not fail; it also left the
    suite's only wall-time coverage host-timezone dependent. Pinning the instant
    proves the actual Berlin anchoring (CEST +02:00 / CET +01:00) and is
    independent of the host zone — the suite runs under TZ=UTC (conftest).
    """
    assert resolve_wall_time(naive).astimezone(UTC) == expected_utc


def test_cross_midnight_wall_times_resolve() -> None:
    start = resolve_wall_time(datetime(2026, 7, 31, 19, 0))
    end = resolve_wall_time(datetime(2026, 8, 1, 2, 30))
    assert (end - start).total_seconds() == 7.5 * 3600


def test_dst_gap_elapsed_uses_instants() -> None:
    """01:30 -> 03:30 across the spring-forward gap is 1 elapsed hour.

    Raw same-tzinfo subtraction would wrongly report 2 wall hours; the
    canonical elapsed helper normalizes to UTC instants (docs/ARCHITECTURE.md).
    """
    from app.worktime.service import elapsed_seconds

    start = resolve_wall_time(datetime(2026, 3, 29, 1, 30))
    end = resolve_wall_time(datetime(2026, 3, 29, 3, 30))
    assert elapsed_seconds(start, end) == 3600


def test_aware_datetime_rejected() -> None:
    aware = datetime(2026, 7, 1, 8, 0, tzinfo=ZoneInfo("UTC"))
    with pytest.raises(ValueError, match="naive"):
        resolve_wall_time(aware)
