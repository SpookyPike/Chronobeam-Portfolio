"""Disclosed-fact guards (ACCESS-011).

A fact that was included in a submitted package or a granted snapshot must
not be destructively deleted: the recipient already reviewed the old state,
and DELETE removes the whole version chain (the resurrected-predecessor bug
class). Employees keep the full correction path (versioned PATCH), which
preserves lineage; a re-submission or re-grant creates the superseding
disclosed version (ACCESS-011).

The guard is month-granular (any disclosed object covering the month):
deliberately coarser than payload parsing — simple, deterministic, and safe.
"""

import uuid
from datetime import date

import sqlalchemy as sa
from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session, aliased

from app.disclosure.models import (
    DisclosureGrant,
    SubmissionPackage,
    SubmissionPackageStatus,
)
from app.worktime.models import WorkInterval


def month_is_disclosed(
    session: Session, employee_id: uuid.UUID, month: date
) -> tuple[bool, str | None]:
    """True when the employee's month is covered by disclosed objects.

    Returns (disclosed, kind): kind is ``"package"`` or ``"grant"`` for the
    message. Packages still SUPERSEDED count — their payloads were reviewed;
    WITHDRAWN packages do not block.
    """
    package = session.scalar(
        select(SubmissionPackage.id)
        .where(
            SubmissionPackage.employee_id == employee_id,
            SubmissionPackage.period_start <= month,
            SubmissionPackage.period_end >= month,
            SubmissionPackage.status != SubmissionPackageStatus.WITHDRAWN,
        )
        .limit(1)
    )
    if package is not None:
        return True, "package"
    newer = aliased(DisclosureGrant)
    superseded = exists(select(newer.id).where(newer.supersedes_id == DisclosureGrant.id))
    grant = session.scalar(
        select(DisclosureGrant.id)
        .where(
            DisclosureGrant.employee_id == employee_id,
            DisclosureGrant.period_start <= month,
            DisclosureGrant.period_end >= month,
            DisclosureGrant.revoked_at.is_(None),
            ~superseded,
            or_(
                DisclosureGrant.expires_at.is_(None),
                DisclosureGrant.expires_at > sa.func.now(),
            ),
        )
        .limit(1)
    )
    if grant is not None:
        return True, "grant"
    return False, None


def assert_interval_not_disclosed(
    session: Session, employee_id: uuid.UUID, interval: WorkInterval
) -> None:
    """Refuse deletion when any Berlin-local day of an interval is disclosed.

    A single canonical interval can cross a month boundary. Checking only its
    start month leaves the portion in the later disclosed month destructively
    removable, so use the same half-open local-day semantics as ledger views.
    """
    from app.worktime.queries import spanned_local_dates

    for covered_day in spanned_local_dates(interval):
        assert_not_disclosed(session, employee_id, covered_day.replace(day=1))


def assert_not_disclosed(
    session: Session, employee_id: uuid.UUID, month: date, action: str = "delete"
) -> None:
    """Refuse destructive operations on disclosed months.

    Called by the worktime delete command; raises the HTTPException the
    caller should propagate. Kept dependency-light (models only) so worktime
    can import it without an import cycle.
    """
    from fastapi import status

    from app.errors import ErrorCode, api_error

    disclosed, kind = month_is_disclosed(session, employee_id, month)
    if disclosed:
        raise api_error(
            status.HTTP_409_CONFLICT,
            ErrorCode.CONFLICT_DISCLOSED_FACT,
            "This month contains facts covered by a disclosure. "
            "Correct them with a versioned correction instead of deleting "
            "them; resubmit or re-grant to share the updated view.",
            details={"disclosure_kind": kind, "action": action},
        )
