"""Pure, source-safe shift findings (SHIFT-001..013; no DB/HTTP imports)."""

from dataclasses import dataclass
from datetime import UTC, datetime

from app.enums import FindingStatus, ShiftDeviationType

TEN_HOURS_SECONDS = 10 * 60 * 60


@dataclass(frozen=True, slots=True)
class ShiftFinding:
    rule_id: str
    status: FindingStatus
    message: str


def evaluate_shift(
    *,
    deviation_type: ShiftDeviationType | None,
    actual_start_at: datetime | None,
    actual_end_at: datetime | None,
    communicated_at: datetime | None,
    authoritative_compensation_enabled: bool,
    cancellation_3d_enabled: bool,
    paid_break_recorded: bool = False,
) -> list[ShiftFinding]:
    """Return facts and uncertainty only; never calculate unconfirmed pay.

    UTC normalization matters because Python compares same-zone aware values
    using wall-clock semantics around a DST fold (docs/ARCHITECTURE.md).

    ``paid_break_recorded`` is the SHIFT-003 condition, not a policy guess: the
    rule *requires* explicit break facts, so once one is recorded as linked to
    this assignment the requirement is met and the warning would assert the
    opposite of the employee's own fact. Nothing is inferred from a plan either
    way — ``paid_break_status`` on the report carries what was recorded.
    """
    findings = [
        ShiftFinding("SHIFT-001", "INFO", "Plan, actual work, and deviation are separate facts."),
    ]
    if not paid_break_recorded:
        findings.append(
            ShiftFinding(
                "SHIFT-003",
                "WARNING",
                "Paid shift breaks require explicit break facts; no break time was "
                "inferred from this plan.",
            )
        )
    if actual_start_at is not None and actual_end_at is not None:
        elapsed = (actual_end_at.astimezone(UTC) - actual_start_at.astimezone(UTC)).total_seconds()
        if elapsed > TEN_HOURS_SECONDS:
            findings.append(
                ShiftFinding(
                    "SHIFT-012",
                    "CRITICAL",
                    "Actual shift time above 10 hours is retained and needs report justification.",
                )
            )
        elif deviation_type == ShiftDeviationType.OVERRUN:
            findings.append(
                ShiftFinding(
                    "SHIFT-011",
                    "INFO" if authoritative_compensation_enabled else "WARNING",
                    "Overrun compensation is provisional: this build ships no confirmed "
                    "overrun rate between 7:48 h and 10 h.",
                )
            )
    if deviation_type == ShiftDeviationType.CANCELLED:
        if communicated_at is None:
            findings.append(
                ShiftFinding(
                    "SHIFT-009",
                    "BLOCKED_POLICY",
                    "Cancellation timing cannot be classified without the communicated time.",
                )
            )
        else:
            findings.append(
                ShiftFinding(
                    "SHIFT-009",
                    "WARNING",
                    "Cancellation timing is retained for provisional review; no "
                    "compensation was calculated.",
                )
            )
    # SHIFT-010 covers BOTH cases the source groups together: a cancellation
    # within three days of the shift, and an abortion of a shift that had already
    # started. SHIFT-009 above stays CANCELLED-only — it asks about the lead time
    # before a shift "was to be performed", which an abortion after the start does
    # not have.
    if deviation_type in (
        ShiftDeviationType.CANCELLED,
        ShiftDeviationType.ABORTED_BY_COMPANY,
    ):
        findings.append(
            ShiftFinding(
                "SHIFT-010",
                "WARNING" if cancellation_3d_enabled else "BLOCKED_POLICY",
                "The exact three-day cancellation and Company-abortion entitlement is "
                "not authoritative under the current policy source.",
            )
        )
    return findings
