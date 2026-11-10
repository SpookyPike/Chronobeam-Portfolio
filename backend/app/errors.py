"""Stable application error envelope (WT-BACKEND §11).

Every API 4xx/5xx carries ``{"detail": {"code", "message", ...}}`` — the
shape is extended to ALL routes via the ``RequestValidationError`` handler
in main.py. The Pydantic handler deliberately strips
``input``/``ctx`` so invalid values are never echoed back to clients or into
proxy/access logs (DP-LOG-001, DP-DATA-002).
"""

from datetime import date
from enum import StrEnum

from fastapi import HTTPException, status


class ErrorCode(StrEnum):
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_UNKNOWN_PRINCIPAL = "AUTH_UNKNOWN_PRINCIPAL"
    AUTH_INVALID_TOKEN = "AUTH_INVALID_TOKEN"  # noqa: S105 - error code, not a credential
    AUTH_FORBIDDEN = "AUTH_FORBIDDEN"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    VALIDATION_INVALID_MONTH = "VALIDATION_INVALID_MONTH"
    VALIDATION_INVALID_DATE = "VALIDATION_INVALID_DATE"
    VALIDATION_INTERVAL_ORDER = "VALIDATION_INTERVAL_ORDER"
    VALIDATION_INTERVAL_OVERLAP = "VALIDATION_INTERVAL_OVERLAP"
    VALIDATION_DST_AMBIGUOUS = "VALIDATION_DST_AMBIGUOUS"
    CONFLICT_VERSION = "CONFLICT_VERSION"
    CONFLICT_SHIFT_PLAN_CHANGED = "CONFLICT_SHIFT_PLAN_CHANGED"
    CONFLICT_DISCLOSED_FACT = "CONFLICT_DISCLOSED_FACT"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    CALENDAR_BLOCKED = "CALENDAR_BLOCKED"
    SUBMISSION_MISSING_REQUIRED_DATA = "SUBMISSION_MISSING_REQUIRED_DATA"
    SUBMISSION_CHANGED = "SUBMISSION_CHANGED"
    SUBMISSION_ALREADY_CREATED = "SUBMISSION_ALREADY_CREATED"
    CONFLICT_CUTOVER_SEED = "CONFLICT_CUTOVER_SEED"
    CONFLICT_DUPLICATE_IDENTITY = "CONFLICT_DUPLICATE_IDENTITY"
    ADMIN_SELF_MUTATION = "ADMIN_SELF_MUTATION"
    NOT_FOUND = "NOT_FOUND"


class PolicyEditionUnavailable(Exception):
    """No installed policy edition covers a month (EINV-08 / F4 D5).

    A *known*, policy-caused unavailability is not an unexpected server fault:
    it is answered as 409 ``POLICY_BLOCKED`` rather than a 500. It is a plain
    domain exception rather than an ``HTTPException`` so the persistence adapter
    and the pure engine stay HTTP-free and non-HTTP callers (the cutover seed,
    offline tooling) can choose their own status.
    """

    def __init__(self, policy_key: str, month: date, chain_month: date | None = None) -> None:
        self.policy_key = policy_key
        self.month = month
        # The carry-forward chain resolves one edition per chain month, so a
        # request for 2027-01 can fail because 2026-11 has no edition. Naming
        # only the requested month would be unactionable.
        self.chain_month = chain_month
        detail = f"no policy edition for {policy_key!r} covers {month:%Y-%m}"
        if chain_month is not None and chain_month != month:
            detail += f" (carry-forward chain month {chain_month:%Y-%m} has no edition)"
        super().__init__(detail)

    def details(self) -> dict[str, object]:
        return {
            "policy_key": self.policy_key,
            "month": f"{self.month:%Y-%m}",
            "chain_month": f"{(self.chain_month or self.month):%Y-%m}",
        }


class CalendarUnavailable(Exception):
    """No institutional calendar edition covers a day the calculation needs (EINV-10).

    A *known*, operator-fixable domain condition is not an unexpected server
    fault: it is answered as 409 ``CALENDAR_BLOCKED`` rather than a 500 — and
    never as the silent "every day is an ordinary working day" fallback that
    made a holiday add required time (audit B-2). Like
    ``PolicyEditionUnavailable`` it is a plain domain exception so the pure
    engine, the persistence adapter and offline tooling stay HTTP-free.
    """

    def __init__(
        self, calendar_key: str, first_uncovered: date, chain_month: date | None = None
    ) -> None:
        self.calendar_key = calendar_key
        # The carry-forward chain resolves a calendar for every chain month, so
        # a request for 2027-01 can fail because 2026-11 is uncovered. Naming
        # only the requested month would be unactionable.
        self.first_uncovered = first_uncovered
        self.chain_month = chain_month
        detail = f"no calendar edition for {calendar_key!r} covers {first_uncovered.isoformat()}"
        if chain_month is not None:
            detail += f" (carry-forward chain month {chain_month:%Y-%m} is uncovered)"
        super().__init__(detail)

    def details(self) -> dict[str, object]:
        return {
            "calendar_key": self.calendar_key,
            "first_uncovered_day": self.first_uncovered.isoformat(),
            "chain_month": f"{self.chain_month:%Y-%m}" if self.chain_month else None,
        }


class OvertimeAuthorizationDecisionRefused(Exception):
    """An institutional overtime decision cannot be applied to this row (ACCESS-017).

    Raised by ``app.ocd_overtime.service.decide_authorization`` — the ONE
    institutional writer. A plain domain exception rather than an
    ``HTTPException``, following ``PolicyEditionUnavailable``: there is
    deliberately no HTTP route for an authority decision, so the operator CLI
    (and a future adapter) choose their own reporting.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def error_body(
    code: ErrorCode | str,
    message: str,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    """The canonical error envelope, as a plain dict.

    Used by ``api_error`` (which wraps it in an ``HTTPException``) and by any
    endpoint that must return an explicit ``Response`` — an exception response
    cannot carry a cookie, so the OIDC callback builds its error responses from
    this one builder rather than repeating the JSON literal.
    """
    detail: dict[str, object] = {
        "code": code.value if isinstance(code, ErrorCode) else code,
        "message": message,
    }
    if details:
        detail["details"] = details
    return detail


def api_error(
    status_code: int,
    code: ErrorCode,
    message: str,
    details: dict[str, object] | None = None,
) -> HTTPException:
    return HTTPException(status_code=status_code, detail=error_body(code, message, details))


AUTH_FORBIDDEN_BODY: dict[str, str] = {
    "code": ErrorCode.AUTH_FORBIDDEN.value,
    "message": "Access to this resource is restricted.",
}


def forbidden() -> HTTPException:
    """The single byte-identical 403 used by every /me/* denial (M4).

    One constant body for: missing capability, unmapped subject, inactive
    employee. No branch of the denial path may differ, so attackers cannot
    distinguish reasons (ARCH-AUTH-004, WT-BACKEND §11).
    """
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=dict(AUTH_FORBIDDEN_BODY))
