"""Cutover opening-balance route.

``POST /api/v1/cutover/opening-balances`` is guarded by the dedicated
CUTOVER_SEED_WRITE capability (server-controlled operator allowlist in
production, empty = fail closed). There is deliberately NO workbook
upload/import operation anywhere in this API (CUTOVER-T03).
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth.deps import get_current_principal
from app.auth.principal import Capability, Principal
from app.cutover.schemas import CutoverSeedIn, CutoverSeedOut
from app.cutover.service import CutoverSeedCreated, CutoverSeedResult, seed_opening_balance
from app.db import get_db
from app.errors import ErrorCode, forbidden

router = APIRouter(prefix="/api/v1/cutover", tags=["cutover"])


def get_cutover_operator(
    principal: Annotated[Principal, Depends(get_current_principal)],
) -> Principal:
    """Capability absence is the denial point — one byte-identical 403."""
    if not principal.has_capability(Capability.CUTOVER_SEED_WRITE):
        raise forbidden()
    return principal


@router.post(
    "/opening-balances",
    response_model=CutoverSeedOut,
    status_code=status.HTTP_201_CREATED,
    summary="Seed one employee's validated legacy closing balance (cutover only)",
)
def create_opening_balance_seed(
    payload: CutoverSeedIn,
    principal: Annotated[Principal, Depends(get_cutover_operator)],
    session: Annotated[Session, Depends(get_db)],
) -> CutoverSeedOut | JSONResponse:
    year, month = (int(part) for part in payload.cutover_month.split("-"))
    cutover_month = date(year, month, 1)
    result: CutoverSeedResult = seed_opening_balance(
        session,
        principal,
        employee_external_id=payload.employee_external_id,
        cutover_month=cutover_month,
        minutes=payload.minutes,
    )
    if isinstance(result, CutoverSeedCreated):
        entry = result.entry
        return CutoverSeedOut(
            seed_id=entry.id,
            employee_id=entry.employee_id,
            cutover_month=payload.cutover_month,
            occurred_on=entry.occurred_on,
            minutes=entry.minutes,
            previous_period=result.previous_period,
            confirmed_by=principal.subject,
        )
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "detail": {
                "code": ErrorCode.CONFLICT_CUTOVER_SEED.value,
                "message": result.message,
                "details": {"reason": result.reason},
            }
        },
    )
