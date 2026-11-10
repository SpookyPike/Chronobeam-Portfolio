"""Pure legacy-compatible CSV renderers for the transition pilot.

Presentation-only. Values come from the deterministic engine (Work Time
Record export) or from the FROZEN ``submission_package.payload_json``
(submission exports) — never from recomputing live facts, so a later
correction cannot rewrite an already-disclosed artifact (INV-11,
GATE-HIST-01). The recipient field sets in Policy spec §13 remain
provisional: every artifact carries ``report_contract_status=PROVISIONAL``
and unavailable provisional values render blank, never invented.

Output is CRLF CSV written by the standard csv writer; the route prepends a
UTF-8 BOM so Excel opens it with correct encoding — semantic compatibility
for shadow-month comparison, not workbook styling.
"""

import csv
import io
from collections.abc import Mapping, Sequence
from typing import Any

REPORT_CONTRACT_STATUS = "PROVISIONAL"

# Spreadsheet formula-injection neutralization (CSV injection): a text cell
# whose first non-whitespace character could start a formula is prefixed.
# Numeric cells bypass this — they must stay numeric in Excel.
_FORMULA_TRIGGER = ("=", "+", "-", "@", "\t", "\r")


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    stripped = text.lstrip()
    if stripped and stripped[0] in _FORMULA_TRIGGER and not text.startswith("'"):
        return "'" + text
    return text


def _num(value: Any) -> str:
    return "" if value is None else str(value)


def _dump(header: Sequence[str], rows: Sequence[Mapping[str, str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([row.get(column, "") for column in header])
    return buffer.getvalue()


def _envelope(package: Mapping[str, Any]) -> dict[str, str]:
    return {
        "package_id": _text(package["id"]),
        "package_version": _num(package["version"]),
        "package_status": _text(package["status"]),
        "supersedes_id": _text(package["supersedes_id"]),
        "report_contract_status": REPORT_CONTRACT_STATUS,
    }


WORK_TIME_RECORD_HEADER = (
    "record_type",
    "report_contract_status",
    "employee_identifier",
    "period_month",
    "policy_version",
    "engine_build",
    "opening_balance_minutes",
    "closing_balance_minutes",
    "day",
    "calendar_class",
    "worked_minutes",
    "required_minutes",
    "delta_minutes",
    "interval_id",
    "interval_version",
    "start_at",
    "end_at",
    "work_type",
)


def render_work_time_record(
    *,
    employee_identifier: str,
    period_month: str,
    policy_version: str,
    engine_build: str,
    opening_balance_minutes: int,
    closing_balance_minutes: int,
    daily_rows: Sequence[Mapping[str, Any]],
    interval_rows: Sequence[Mapping[str, Any]],
) -> str:
    """Month summary + daily aggregates + current interval facts.

    ``daily_rows`` / ``interval_rows`` arrive pre-sorted by the caller
    (date order; start/id order) — the renderer never sorts or derives.
    """
    common = {
        "report_contract_status": REPORT_CONTRACT_STATUS,
        "employee_identifier": _text(employee_identifier),
    }
    rows: list[dict[str, str]] = [
        {
            **common,
            "record_type": "MONTH",
            "period_month": _text(period_month),
            "policy_version": _text(policy_version),
            "engine_build": _text(engine_build),
            "opening_balance_minutes": _num(opening_balance_minutes),
            "closing_balance_minutes": _num(closing_balance_minutes),
        }
    ]
    rows.extend(
        {
            **common,
            "record_type": "DAILY",
            "day": _text(row["day"]),
            "calendar_class": _text(row["calendar_class"]),
            "worked_minutes": _num(row["worked_minutes"]),
            "required_minutes": _num(row["required_minutes"]),
            "delta_minutes": _num(row["delta_minutes"]),
        }
        for row in daily_rows
    )
    rows.extend(
        {
            **common,
            "record_type": "INTERVAL",
            "interval_id": _text(row["interval_id"]),
            "interval_version": _num(row["interval_version"]),
            "start_at": _text(row["start_at"]),
            "end_at": _text(row["end_at"]),
            "work_type": _text(row["work_type"]),
        }
        for row in interval_rows
    )
    return _dump(WORK_TIME_RECORD_HEADER, rows)


SHIFT_HEADER = (
    "record_type",
    "report_contract_status",
    "package_id",
    "package_version",
    "package_status",
    "supersedes_id",
    "policy_version",
    "policy_status",
    "employee_identifier",
    "shift_assignment_id",
    "planned_start_at",
    "planned_end_at",
    "actual_start_at",
    "actual_end_at",
    "deviation_type",
    "noticed_or_decided_at",
    "decision_maker_type",
    "reason",
    "paid_break_status",
    # Appended, never inserted: existing columns keep their positions so a
    # downstream reader of the frozen CSV is not silently re-pointed at a
    # different field. ``plan_source`` says who wrote the plan this report is
    # about, and ``compensation_review_status`` says whether the compensation
    # outcome still needs a human decision — it is a STATUS, never an amount.
    "plan_source",
    "compensation_review_status",
)


def render_shift_report(package: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    row = {
        "record_type": "REPORT",
        **_envelope(package),
        **{key: _text(value) for key, value in payload.items()},
    }
    return _dump(SHIFT_HEADER, [row])


OVERTIME_HEADER = (
    "record_type",
    "report_contract_status",
    "package_id",
    "package_version",
    "package_status",
    "supersedes_id",
    "policy_version",
    "policy_status",
    "employee_identifier",
    "period_start",
    "period_end",
    "auth_id",
    "auth_valid_from",
    "auth_valid_to",
    "auth_purpose",
    "auth_status",
    "auth_source_ref",
    "interval_id",
    "interval_version",
    "interval_start_at",
    "interval_end_at",
    "interval_work_type",
    "interval_elapsed_seconds",
    "interval_linked_authorization_id",
    "component_rule_id",
    "component",
    "component_minutes",
    "component_time_equivalent_minutes",
    "election_id",
    "election_mode",
    "election_version",
)


def render_overtime(package: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    env = _envelope(package)
    election = payload.get("election") or {}
    rows: list[dict[str, str]] = [
        {
            "record_type": "REPORT",
            **env,
            "policy_version": _text(payload["policy_version"]),
            "policy_status": _text(payload["policy_status"]),
            "employee_identifier": _text(payload["employee_identifier"]),
            "period_start": _text(payload["period_start"]),
            "period_end": _text(payload["period_end"]),
            "election_id": _text(election.get("id")),
            "election_mode": _text(election.get("mode")),
            "election_version": _num(election.get("version")),
        }
    ]
    rows.extend(
        {
            "record_type": "AUTH",
            **env,
            "auth_id": _text(auth["id"]),
            "auth_valid_from": _text(auth["valid_from"]),
            "auth_valid_to": _text(auth["valid_to"]),
            "auth_purpose": _text(auth["purpose"]),
            "auth_status": _text(auth["status"]),
            "auth_source_ref": _text(auth["source_ref"]),
        }
        for auth in payload.get("authorizations", [])
    )
    rows.extend(
        {
            "record_type": "INTERVAL",
            **env,
            "interval_id": _text(interval["interval_id"]),
            "interval_version": _num(interval["version"]),
            "interval_start_at": _text(interval["start_at"]),
            "interval_end_at": _text(interval["end_at"]),
            "interval_work_type": _text(interval["work_type"]),
            "interval_elapsed_seconds": _num(interval["elapsed_seconds"]),
            "interval_linked_authorization_id": _text(interval["linked_authorization_id"]),
        }
        for interval in payload.get("overtime_intervals", [])
    )
    rows.extend(
        {
            "record_type": "COMPONENT",
            **env,
            "component_rule_id": _text(component["rule_id"]),
            "component": _text(component["component"]),
            "component_minutes": _num(component["minutes"]),
            "component_time_equivalent_minutes": _num(component.get("time_equivalent_minutes")),
        }
        for component in payload.get("classified_components", [])
    )
    return _dump(OVERTIME_HEADER, rows)


OCD_HEADER = (
    "record_type",
    "report_contract_status",
    "package_id",
    "package_version",
    "package_status",
    "supersedes_id",
    "policy_version",
    "policy_status",
    "employee_identifier",
    "ocd_assignment_id",
    "assignment_start_at",
    "assignment_end_at",
    "call_id",
    "call_mode",
    "call_start_at",
    "call_end_at",
    "travel_out_minutes",
    "travel_back_minutes",
    "span_start_at",
    "span_end_at",
    "call_elapsed_seconds",
    "component_rule_id",
    "component",
    "component_minutes",
    "component_time_equivalent_minutes",
    "duty_minutes",
    "duty_kind",
    "duty_units",
    "duty_basis_points",
    "duty_short_notice",
    "election_id",
    "election_mode",
    "election_version",
)


def render_ocd_compensation(package: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    env = _envelope(package)
    duty = payload.get("duty") or {}
    election = payload.get("election") or {}
    rows: list[dict[str, str]] = [
        {
            "record_type": "REPORT",
            **env,
            "policy_version": _text(payload["policy_version"]),
            "policy_status": _text(payload["policy_status"]),
            "employee_identifier": _text(payload["employee_identifier"]),
            "ocd_assignment_id": _text(payload["ocd_assignment_id"]),
            "assignment_start_at": _text(payload["assignment_start_at"]),
            "assignment_end_at": _text(payload["assignment_end_at"]),
            "duty_minutes": _num(duty.get("duty_minutes")),
            "duty_kind": _text(duty.get("duty_kind")),
            "duty_units": _num(duty.get("units")),
            "duty_basis_points": _num(duty.get("basis_points")),
            "duty_short_notice": _text(duty.get("short_notice")),
            "election_id": _text(election.get("id")),
            "election_mode": _text(election.get("mode")),
            "election_version": _num(election.get("version")),
        }
    ]
    rows.extend(
        {
            "record_type": "CALL",
            **env,
            "call_id": _text(call["call_id"]),
            "call_mode": _text(call["mode"]),
            "call_start_at": _text(call["call_start_at"]),
            "call_end_at": _text(call["call_end_at"]),
            "travel_out_minutes": _num(call["travel_out_minutes"]),
            "travel_back_minutes": _num(call["travel_back_minutes"]),
            "span_start_at": _text(call["span_start_at"]),
            "span_end_at": _text(call["span_end_at"]),
            "call_elapsed_seconds": _num(call["elapsed_seconds"]),
        }
        for call in payload.get("calls", [])
    )
    rows.extend(
        {
            "record_type": "COMPONENT",
            **env,
            "component_rule_id": _text(component["rule_id"]),
            "component": _text(component["component"]),
            "component_minutes": _num(component["minutes"]),
            "component_time_equivalent_minutes": _num(component.get("time_equivalent_minutes")),
        }
        for component in payload.get("classified_components", [])
    )
    return _dump(OCD_HEADER, rows)
