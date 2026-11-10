"""Employee-self legacy transition export routes.

Two read surfaces, both strictly /me-scoped (the employee identifier is
never a parameter):

- ``GET /api/v1/me/reports/legacy/work-time-record?month=YYYY-MM`` renders
  the private month exactly as the deterministic engine computed it.
- ``GET /api/v1/me/reports/legacy/submissions/{package_id}`` renders the
  FROZEN submission payload by purpose (superseded/withdrawn packages stay
  exportable for reproducibility; their status is in every row).

No recipient-side export exists: recipients keep reading assigned frozen
projections through the disclosure grant boundaries, and delivery of a CSV
artifact is an employee-mediated act, like emailing today's workbook.
Successful private exports are not audited (they disclose nothing on their
own — same rule as ordinary private reads).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import get_current_employee
from app.db import get_db
from app.disclosure.models import SubmissionPackage
from app.enums import SubmissionPurpose
from app.errors import ErrorCode, api_error
from app.identity.models import Employee
from app.policy.routes import _parse_month
from app.policy.service import calculate_employee_period, policy_version_for
from app.reporting.legacy_csv import (
    render_ocd_compensation,
    render_overtime,
    render_shift_report,
    render_work_time_record,
)
from app.worktime.queries import current_intervals, month_bounds

router = APIRouter(prefix="/api/v1/me/reports/legacy", tags=["reporting"])

_RENDERER_BY_PURPOSE = {
    SubmissionPurpose.SHIFT_REPORT: render_shift_report,
    SubmissionPurpose.OVERTIME_PAID: render_overtime,
    SubmissionPurpose.OVERTIME_TIME_COMP: render_overtime,
    SubmissionPurpose.OCD_COMPENSATION: render_ocd_compensation,
}


def _csv_response(csv_text: str, filename: str) -> Response:
    # UTF-8 BOM: Excel on Windows only auto-detects UTF-8 when present.
    return Response(
        content="\ufeff" + csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _employee_identifier(employee: Employee) -> str:
    return employee.external_employee_id or str(employee.id)


@router.get(
    "/work-time-record",
    summary="Legacy-compatible monthly Work Time Record export (employee-self)",
)
def export_work_time_record(
    month: Annotated[str, Query(description="calendar month in YYYY-MM format")],
    employee: Annotated[Employee, Depends(get_current_employee)],
    session: Annotated[Session, Depends(get_db)],
) -> Response:
    parsed = _parse_month(month)
    calculation = calculate_employee_period(session, employee, parsed)
    policy = policy_version_for(session, parsed)
    start_at, end_at = month_bounds(parsed)
    intervals = current_intervals(
        session, employee_id=employee.id, intersects_from=start_at, intersects_to=end_at
    )
    csv_text = render_work_time_record(
        employee_identifier=_employee_identifier(employee),
        period_month=month,
        policy_version=policy.version,
        engine_build=policy.engine_rule_set,
        opening_balance_minutes=calculation.opening_balance_minutes,
        closing_balance_minutes=calculation.closing_balance_minutes,
        daily_rows=[
            {
                "day": day.day.isoformat(),
                "calendar_class": day.calendar_class.value,
                "worked_minutes": day.worked_minutes,
                "required_minutes": day.required_minutes,
                "delta_minutes": day.flex_delta_minutes,
            }
            for day in calculation.daily_results
        ],
        interval_rows=[
            {
                "interval_id": str(row.id),
                "interval_version": row.version,
                "start_at": row.start_at.isoformat(),
                "end_at": row.end_at.isoformat(),
                "work_type": row.work_type.value,
            }
            for row in sorted(intervals, key=lambda row: (row.start_at, str(row.id)))
        ],
    )
    return _csv_response(csv_text, f"work-time-record-{month}.csv")


@router.get(
    "/submissions/{package_id}",
    summary="Legacy-compatible export of a frozen submission package (employee-self)",
)
def export_submission(
    package_id: UUID,
    employee: Annotated[Employee, Depends(get_current_employee)],
    session: Annotated[Session, Depends(get_db)],
) -> Response:
    package = session.scalar(
        select(SubmissionPackage).where(
            SubmissionPackage.id == package_id,
            SubmissionPackage.employee_id == employee.id,
        )
    )
    if package is None:
        # Same body for unknown and foreign ids — no existence oracle.
        raise api_error(404, ErrorCode.NOT_FOUND, "The requested resource does not exist.")
    renderer = _RENDERER_BY_PURPOSE.get(package.purpose)
    if renderer is None:
        raise api_error(
            422,
            ErrorCode.VALIDATION_FAILED,
            "This submission purpose has no legacy-compatible export.",
        )
    csv_text = renderer(
        {
            "id": str(package.id),
            "version": package.version,
            "status": package.status.value,
            "supersedes_id": str(package.supersedes_id) if package.supersedes_id else None,
        },
        package.payload_json,
    )
    filename = f"{package.purpose.value.lower()}-v{package.version}.csv"
    return _csv_response(csv_text, filename)
