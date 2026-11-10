"""Employee-private calculation response schemas."""

from datetime import date

from pydantic import BaseModel

from app.policy.engine import FindingSeverity


class FindingOut(BaseModel):
    rule_id: str
    severity: FindingSeverity
    date: date | None
    message: str


class FindingsPeriodOut(BaseModel):
    period_month: date
    opening_balance_minutes: int
    closing_balance_minutes: int
    findings: list[FindingOut]
