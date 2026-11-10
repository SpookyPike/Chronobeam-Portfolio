"""Employee-private travel endpoints; router registration is integration-owned."""

import uuid
from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.auth.deps import get_current_employee, get_current_employee_writer, get_current_principal
from app.auth.principal import Principal
from app.db import get_db
from app.errors import ErrorCode, api_error
from app.identity.models import Employee
from app.travel.schemas import (
    SegmentDelete,
    SegmentIn,
    SegmentOut,
    SegmentPatch,
    TravelSummaryOut,
    TripDelete,
    TripIn,
    TripListOut,
    TripOut,
    TripPatch,
)
from app.travel.service import (
    create_segment,
    create_trip,
    delete_segment,
    delete_trip,
    list_trips,
    patch_segment,
    patch_trip,
    travel_summary,
)

router = APIRouter(prefix="/api/v1/me", tags=["travel"])


def _month(value: str) -> date:
    try:
        parsed = datetime.strptime(value, "%Y-%m").date()
    except ValueError:
        raise api_error(
            422, ErrorCode.VALIDATION_INVALID_MONTH, "That period is not a calendar month."
        ) from None
    return parsed


@router.get("/travel-trips", response_model=TripListOut)
def read_trips(
    employee: Annotated[Employee, Depends(get_current_employee)],
    session: Annotated[Session, Depends(get_db)],
) -> TripListOut:
    return TripListOut(trips=list_trips(session, employee))


@router.post("/travel-trips", response_model=TripOut)
def add_trip(
    payload: TripIn,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> Response:
    result, created = create_trip(session, employee, payload)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        content=result.model_dump(mode="json"),
    )


@router.patch("/travel-trips/{trip_id}", response_model=TripOut)
def edit_trip(
    trip_id: uuid.UUID,
    payload: TripPatch,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> TripOut:
    return patch_trip(session, employee, trip_id, payload)


@router.delete("/travel-trips/{trip_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_trip(
    trip_id: uuid.UUID,
    payload: Annotated[TripDelete, Body()],
    principal: Annotated[Principal, Depends(get_current_principal)],
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> Response:
    delete_trip(session, principal, employee, trip_id, payload.version, payload.reason)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/travel-trips/{trip_id}/segments", response_model=SegmentOut)
def add_segment(
    trip_id: uuid.UUID,
    payload: SegmentIn,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> Response:
    result, created = create_segment(session, employee, trip_id, payload)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        content=result.model_dump(mode="json"),
    )


@router.patch("/travel-segments/{segment_id}", response_model=SegmentOut)
def edit_segment(
    segment_id: uuid.UUID,
    payload: SegmentPatch,
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> SegmentOut:
    return patch_segment(session, employee, segment_id, payload)


@router.delete("/travel-segments/{segment_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_segment(
    segment_id: uuid.UUID,
    payload: Annotated[SegmentDelete, Body()],
    principal: Annotated[Principal, Depends(get_current_principal)],
    employee: Annotated[Employee, Depends(get_current_employee_writer)],
    session: Annotated[Session, Depends(get_db)],
) -> Response:
    delete_segment(session, principal, employee, segment_id, payload.version, payload.reason)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/travel-summary", response_model=TravelSummaryOut)
def read_summary(
    period: Annotated[str, Query()],
    employee: Annotated[Employee, Depends(get_current_employee)],
    session: Annotated[Session, Depends(get_db)],
) -> TravelSummaryOut:
    return travel_summary(session, employee, _month(period))
