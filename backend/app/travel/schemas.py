"""Private travel command and derived-view schemas (TRAVEL-001..004)."""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.policy.engine import FindingSeverity
from app.travel.models import TravelSegmentBasis, TravelSegmentClassification, TripStartOrigin


class _WallTime(BaseModel):
    @field_validator("start_local", "end_local", check_fields=False)
    @classmethod
    def naive(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            raise ValueError("wall times must be naive; the timezone field selects the zone")
        return value


class TripIn(_WallTime):
    start_local: datetime
    end_local: datetime
    timezone: Literal["Europe/Berlin"] = "Europe/Berlin"
    start_origin: TripStartOrigin
    reference_label: str | None = Field(None, max_length=200)
    fold: Literal[0, 1] | None = None
    start_fold: Literal[0, 1] | None = None
    end_fold: Literal[0, 1] | None = None
    client_request_id: uuid.UUID | None = None


class TripPatch(_WallTime):
    version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)
    start_local: datetime | None = None
    end_local: datetime | None = None
    timezone: Literal["Europe/Berlin"] = "Europe/Berlin"
    start_origin: TripStartOrigin | None = None
    reference_label: str | None = Field(None, max_length=200)
    fold: Literal[0, 1] | None = None
    start_fold: Literal[0, 1] | None = None
    end_fold: Literal[0, 1] | None = None


class TripDelete(BaseModel):
    version: int = Field(ge=1)
    reason: str | None = Field(None, max_length=500)


class SegmentIn(_WallTime):
    start_local: datetime
    end_local: datetime
    timezone: Literal["Europe/Berlin"] = "Europe/Berlin"
    classification: TravelSegmentClassification
    basis: TravelSegmentBasis = TravelSegmentBasis.ACTUAL
    fold: Literal[0, 1] | None = None
    start_fold: Literal[0, 1] | None = None
    end_fold: Literal[0, 1] | None = None
    client_request_id: uuid.UUID | None = None


class SegmentPatch(_WallTime):
    version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=500)
    start_local: datetime | None = None
    end_local: datetime | None = None
    timezone: Literal["Europe/Berlin"] = "Europe/Berlin"
    classification: TravelSegmentClassification | None = None
    basis: TravelSegmentBasis | None = None
    fold: Literal[0, 1] | None = None
    start_fold: Literal[0, 1] | None = None
    end_fold: Literal[0, 1] | None = None


class SegmentDelete(BaseModel):
    version: int = Field(ge=1)
    reason: str | None = Field(None, max_length=500)


class SegmentOut(BaseModel):
    id: uuid.UUID
    trip_id: uuid.UUID
    start_at: datetime
    end_at: datetime
    start_local: datetime
    end_local: datetime
    classification: TravelSegmentClassification
    basis: TravelSegmentBasis
    work_interval_id: uuid.UUID | None
    version: int
    supersedes_id: uuid.UUID | None
    client_request_id: uuid.UUID | None


class TripOut(BaseModel):
    id: uuid.UUID
    start_at: datetime
    end_at: datetime
    start_local: datetime
    end_local: datetime
    start_origin: TripStartOrigin
    source: str
    reference_label: str | None
    version: int
    supersedes_id: uuid.UUID | None
    client_request_id: uuid.UUID | None
    segments: list[SegmentOut] = []


class TripListOut(BaseModel):
    trips: list[TripOut]


class TravelTopupOut(BaseModel):
    day: date
    minutes: int


class TravelCreditOut(BaseModel):
    occurred_on: date
    minutes: int
    rule_id: str
    source_entity_type: str
    source_entity_id: str
    window_start: date
    window_end: date
    eligible_minutes: int
    threshold_minutes: int
    excess_minutes: int


class TravelTripSummaryOut(BaseModel):
    trip_id: str
    window_start: date
    window_end: date
    eligible_minutes: int
    threshold_minutes: int
    excess_minutes: int
    credit_minutes: int
    credit_effective_on: date


class TravelFindingOut(BaseModel):
    rule_id: str
    severity: FindingSeverity
    date: date | None
    message: str


class TravelSummaryOut(BaseModel):
    period_month: date
    topups: list[TravelTopupOut]
    credits: list[TravelCreditOut]
    trips: list[TravelTripSummaryOut]
    findings: list[TravelFindingOut]
    input_fingerprint: str
