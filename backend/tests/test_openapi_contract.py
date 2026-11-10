"""The generated OpenAPI document is a contract, not an implementation detail.

``create_app`` serves it in non-production and the frontend is written against
it, so a response model that silently disappears from the document is a
breaking change that nothing else in the suite would notice. These tests pin
the high-risk response families by name.

Paired with ``just api-check`` (which fails when the committed digest in
``api-contract.json`` goes stale), this makes backend contract drift a
build failure rather than something a page discovers at runtime.
"""

from typing import Any

from app.main import app

#: Response families the frontend renders directly. A rename or a dropped model
#: here must fail loudly; the frontend's hand-written interfaces are keyed on
#: these names.
HIGH_RISK_RESPONSE_MODELS = (
    # disclosure / submissions
    "ShiftReportPreviewOut",
    "OcdCompensationPreviewOut",
    "OvertimePreviewOut",
    "SubmissionPackageOut",
    "RecipientSubmissionPackageOut",
    "DisclosureGrantOut",
    # employee ledger + day status
    "DayDetail",
    "DayStatusOut",
    "DayStatusMonthOut",
    "MonthSummary",
)

#: The fields the frontend reaches for on the preview responses. `EINV-*` is not
#: in play here; this is the drift that was confirmed live — the hand-written
#: interfaces omitted `preview_fingerprint`, and a page had to cast around it.
REQUIRED_PREVIEW_FIELDS = {
    "ShiftReportPreviewOut": {"preview_fingerprint", "findings"},
    "OcdCompensationPreviewOut": {"preview_fingerprint", "findings"},
    "OvertimePreviewOut": {"preview_fingerprint", "findings"},
}


#: The plan-entry REQUEST models. The frontend hand-writes these interfaces from
#: the plan's §8.6 table rather than from a generated client, so a rename here is
#: a silent 422 at runtime — the exact drift the frontend author flagged as the
#: highest-risk guess of the slice.
PLAN_ENTRY_REQUEST_MODELS = (
    "ShiftPlanIn",
    "ShiftPlanPatch",
    "ShiftWithdrawIn",
    "OcdAssignmentIn",
    "OcdAssignmentPatch",
    "OcdAssignmentCancelIn",
)


def _schemas() -> dict[str, Any]:
    return app.openapi()["components"]["schemas"]


def test_openapi_exposes_the_high_risk_response_models() -> None:
    schemas = _schemas()
    missing = [name for name in HIGH_RISK_RESPONSE_MODELS if name not in schemas]
    assert not missing, f"response models disappeared from the OpenAPI document: {missing}"


def test_openapi_exposes_the_plan_entry_request_models() -> None:
    schemas = _schemas()
    missing = [name for name in PLAN_ENTRY_REQUEST_MODELS if name not in schemas]
    assert not missing, f"plan-entry request models disappeared: {missing}"


def test_plan_entry_creates_require_the_idempotency_key_and_never_accept_a_smuggled_id() -> None:
    """``client_request_id`` is REQUIRED on create and absent from edit/withdraw.

    The second half is the load-bearing one: a create must be retry-safe, while a
    correction is optimistically versioned — an edit that also carried a key would
    give one command two identities.
    """
    schemas = _schemas()
    for name in ("ShiftPlanIn", "OcdAssignmentIn"):
        required = set(schemas[name].get("required", []))
        assert "client_request_id" in required, f"{name} must require client_request_id"
        assert schemas[name].get("additionalProperties") is False, (
            f"{name} must forbid extra fields, or employee_id could be smuggled in"
        )
    for name in (
        "ShiftPlanPatch",
        "ShiftWithdrawIn",
        "OcdAssignmentPatch",
        "OcdAssignmentCancelIn",
    ):
        properties = set(schemas[name].get("properties", {}))
        assert "client_request_id" not in properties, f"{name} must not carry an idempotency key"
        assert "employee_id" not in properties, f"{name} must never accept an employee id"


def test_preview_responses_declare_the_fingerprint_and_findings() -> None:
    """The exact drift that was fixed in F7, pinned so it cannot come back."""
    schemas = _schemas()
    for name, expected in REQUIRED_PREVIEW_FIELDS.items():
        properties = set(schemas[name].get("properties", {}))
        assert expected <= properties, f"{name} no longer declares {sorted(expected - properties)}"


#: Responses that must expose the optimistic-concurrency token their own
#: version-guarded commands require. `PATCH`/`POST …/cancel` answer 409
#: `CONFLICT_VERSION` on a stale token, so a card that cannot read the current one
#: must withhold the control entirely — which silently turns the feature off. This
#: pins the token, not just the model.
VERSION_TOKEN_MODELS = ("OcdOut", "OcdCallOut")


def test_version_guarded_responses_declare_their_concurrency_token() -> None:
    """A version-guarded write needs a readable version. Verified live: `OcdOut`
    did NOT declare one, so the per-card Correct/Cancel controls could not be
    offered against the real API even though every test was green."""
    schemas = _schemas()
    for name in VERSION_TOKEN_MODELS:
        properties = schemas.get(name, {}).get("properties", {})
        assert "version" in properties, f"{name} no longer declares its version token"
