"""Boundary for external shift-plan sources.

Only the typed import/seed seam exists. No network endpoint,
credential, or vendor-specific payload is assumed before an interface is
actually established.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ShiftAssignmentSeed:
    plan_key: str | None
    plan_name: str | None
    planned_start_at: datetime
    planned_end_at: datetime
    shift_kind: str
    source: str
    source_ref: str | None = None


class ShiftPlanSource(Protocol):
    """Real adapters implement this only after OPEN-06 is resolved."""

    def fetch_assignments(
        self, *, changed_since: datetime | None = None
    ) -> list[ShiftAssignmentSeed]: ...
