"""Europe/Berlin wall-time resolution with explicit DST handling (DATA-009/010).

Business timezone is Europe/Berlin. Wall times entered by the
user are resolved to UTC instants stored as ``timestamptz``.

``datetime.replace(tzinfo=tz)`` silently ACCEPTS nonexistent local times, so
detection must be explicit: compare the
fold=0/fold=1 candidate offsets.

- equal offsets            -> unambiguous instant
- fold0 < fold1            -> NONEXISTENT (spring-forward gap)  -> reject
- fold0 > fold1            -> AMBIGUOUS (fall-back overlap)     -> reject unless fold given

The ``fold`` field is the client-facing disambiguation mechanism
(WT-BACKEND §11 VALIDATION_DST_AMBIGUOUS).

# ceiling: relies on OS zoneinfo supplying Europe/Berlin (present on dev and
# ubuntu-latest runners). If the production runtime image lacks tzdata, add the
# tzdata PyPI package there — not to the app dependency set now.
"""

from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")

NONEXISTENT = "nonexistent"
AMBIGUOUS = "ambiguous"


class DstResolutionError(ValueError):
    """Raised for nonexistent or (without fold) ambiguous local wall times."""

    def __init__(self, kind: Literal["nonexistent", "ambiguous"], naive: datetime) -> None:
        self.kind = kind
        self.naive = naive
        super().__init__(f"{kind} wall time: {naive.isoformat()}")


def resolve_wall_time(
    naive: datetime,
    fold: int | None = None,
    tz: ZoneInfo = BERLIN,
) -> datetime:
    """Resolve a naive Berlin wall time to an aware instant.

    Raises DstResolutionError for nonexistent times and for ambiguous times
    without an explicit fold. With fold=0/1 an ambiguous time resolves to the
    first/second occurrence deterministically (never silently guessed).
    """
    if naive.tzinfo is not None:
        raise ValueError("naive datetime expected (no tzinfo)")

    first = naive.replace(fold=0, tzinfo=tz)
    second = naive.replace(fold=1, tzinfo=tz)
    first_offset = first.utcoffset()
    second_offset = second.utcoffset()
    if first_offset is None or second_offset is None:
        # No zone offset exists at this wall time (unmappable time).
        raise DstResolutionError(NONEXISTENT, naive)
    if first_offset == second_offset:
        return first
    if first_offset < second_offset:
        raise DstResolutionError(NONEXISTENT, naive)
    # Ambiguous (fall-back). Require explicit disambiguation.
    if fold not in (0, 1):
        raise DstResolutionError(AMBIGUOUS, naive)
    return naive.replace(fold=fold, tzinfo=tz)


def now_berlin() -> datetime:
    return datetime.now(BERLIN)


def local_date(instant: datetime) -> date:
    return instant.astimezone(BERLIN).date()
