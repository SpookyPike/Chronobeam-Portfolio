"""Offline and migration-backed checks for the review-only policy snapshot."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import date
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from app.enums import PolicyVersionStatus
from app.policy.editions import EDITIONS, HISTORICAL_RULE_SETS
from app.policy.models import PolicyVersion
from sqlalchemy.orm import Session

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts import policy_snapshot  # noqa: E402


class _EnumValue(Enum):
    TEST = "test-value"


class _Result:
    def __init__(self, rows: list[PolicyVersion]) -> None:
        self.rows = rows

    def all(self) -> list[PolicyVersion]:
        return self.rows


class _OfflineSession:
    def __init__(self, rows: list[PolicyVersion]) -> None:
        self.rows = rows

    def scalars(self, _statement: Any) -> _Result:
        return _Result(self.rows)


def _offline_rows() -> list[PolicyVersion]:
    rows = []
    for rule_set, edition in EDITIONS.items():
        row = PolicyVersion(
            policy_key=f"synthetic-{rule_set}",
            version=edition.version,
            effective_from=date(2026, 6, 1),
            effective_to=None,
            status=PolicyVersionStatus.PROVISIONAL,
            engine_rule_set=rule_set,
            source_manifest_hash=edition.source_manifest_hash,
        )
        rows.append(row)
    return rows


def _snapshot_bytes() -> bytes:
    return policy_snapshot.render_snapshot(
        policy_snapshot.build_snapshot(_OfflineSession(_offline_rows()))  # type: ignore[arg-type]
    )


def test_deterministic_ordering_and_mapping_enum_date_serialization() -> None:
    mapping_a = MappingProxyType({"z": 2, "a": 1})
    mapping_b = MappingProxyType({"a": 1, "z": 2})
    assert policy_snapshot._json_value(mapping_a) == {"a": 1, "z": 2}
    assert policy_snapshot._json_value(mapping_a) == policy_snapshot._json_value(mapping_b)
    assert policy_snapshot._json_value(_EnumValue.TEST) == "test-value"
    assert policy_snapshot._json_value(date(2026, 6, 1)) == "2026-06-01"

    snapshot = policy_snapshot.build_snapshot(_OfflineSession(_offline_rows()))  # type: ignore[arg-type]
    registered_keys = [
        item["registered_identity"]["rule_set"] for item in snapshot["registered_editions"]
    ]
    assert registered_keys == sorted(EDITIONS)
    assert snapshot["shipped_history_allowlist"] == sorted(HISTORICAL_RULE_SETS)
    assert _snapshot_bytes() == _snapshot_bytes()


def test_canonical_render_digest_and_non_finite_rejection() -> None:
    snapshot = {"schema_version": 1, "values": {"date": "2026-06-01", "status": "PROVISIONAL"}}
    first = policy_snapshot.render_snapshot(snapshot)
    second = policy_snapshot.render_snapshot(snapshot)
    document = json.loads(first)
    digest = document.pop("content_sha256")
    canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert digest == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert first == second
    assert first.endswith(b"\n")
    assert (
        policy_snapshot.render_snapshot(snapshot).decode("utf-8")
        == json.dumps(json.loads(first), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    with pytest.raises(policy_snapshot.SnapshotError, match="non-finite float"):
        policy_snapshot._json_value(float("nan"))


def test_blocked_and_sample_policy_markers_are_explicit() -> None:
    snapshot = policy_snapshot.build_snapshot(_OfflineSession(_offline_rows()))  # type: ignore[arg-type]
    markers = snapshot["markers"]
    no_verdict = {item["rule_id"]: item for item in markers["no_verdict"]}
    # A rule the sample policy cannot settle is reported as BLOCKED, not guessed.
    assert no_verdict["WT-FLEX-007"]["marker"] == "NO_VERDICT"
    assert no_verdict["WT-FLEX-007"]["basis"] == "POL-01"
    sample = {item["rule_id"]: item for item in markers["sample_policy_parameters"]}
    assert sample["WT-CORE-008"]["basis"] == "POL-01"
    # A statutory rule must never be reported as sample policy.
    assert "WT-CORE-003" not in sample
    # The year-end payout month is the one parameter no selected source settles, so
    # it is carried as a NAMED open decision rather than an inferred value.
    payout = [
        item for item in markers["not_encoded"] if item.get("field") == "year_end_payout_month"
    ]
    assert len(payout) == 1, f"the payout-month marker is not unique: {payout}"
    assert payout[0]["rule_ids"] == ["WT-FLEX-006", "WT-FLEX-007"]
    assert payout[0]["decision"].strip(), "the open decision must be named, not left blank"

    unpublished = {item["rule_set"]: item for item in snapshot["designed_unpublished_editions"]}
    assert set(unpublished) == {"phase-2-ordinary-v2", "phase-2-travel-v2"}
    for item in unpublished.values():
        assert item["publication_state"] == "UNPUBLISHED"
        assert item["registered"] is False
        assert item["shipped_history_allowlisted"] is False
        assert item["database_row_present"] is False
    assert "phase-2-ordinary-v2" not in snapshot["shipped_history_allowlist"]
    assert "phase-2-travel-v2" not in snapshot["shipped_history_allowlist"]
    assert unpublished["phase-2-ordinary-v2"]["parameters"]["values"] is None
    assert unpublished["phase-2-travel-v2"]["parameters"]["values"]["credit_base"] == "TOTAL"


def test_the_snapshot_carries_no_retired_provenance_token() -> None:
    """The snapshot ships with the public tree, so no retired provenance token may
    survive anywhere in the rendered document. This is a regression guard: those
    prefixes belonged to a source register that is not part of this repository."""
    rendered = policy_snapshot.render_snapshot(
        policy_snapshot.build_snapshot(_OfflineSession(_offline_rows()))  # type: ignore[arg-type]
    ).decode("utf-8")
    for token in ("INST-", "MISSING-", "SRC-", "HR4U-"):
        assert token not in rendered, (
            f"the review snapshot carries the private register token {token!r}"
        )


def test_database_status_is_separate_from_the_rule_basis(
    db_session: Session,
) -> None:
    snapshot = policy_snapshot.build_snapshot(db_session)
    ordinary = next(
        item
        for item in snapshot["registered_editions"]
        if item["registered_identity"]["rule_set"] == "phase-2-ordinary-v1"
    )
    # The row lifecycle says nothing about whether a rule is statutory or sample policy.
    assert ordinary["database_row"]["status"] == "PROVISIONAL"
    basis = snapshot["rule_basis_index"]
    assert "WT-CORE-003" in basis["LAW-01"]
    assert "WT-CORE-008" in basis["POL-01"]
    assert (
        ordinary["database_row"]["engine_rule_set"] == ordinary["registered_identity"]["rule_set"]
    )
    assert ordinary["database_row"]["version"] == ordinary["registered_identity"]["version"]


def test_generated_bytes_are_repeatable_and_match_committed_file_when_present(
    db_session: Session,
) -> None:
    first = policy_snapshot.render_snapshot(policy_snapshot.build_snapshot(db_session))
    second = policy_snapshot.render_snapshot(policy_snapshot.build_snapshot(db_session))
    assert first == second
    committed = policy_snapshot.SNAPSHOT_PATH
    if not committed.exists():
        pytest.skip("generated docs/POLICY_SNAPSHOT.json is absent until host generation")
    assert first == committed.read_bytes()


def test_application_never_references_the_review_snapshot() -> None:
    forbidden = ("POLICY_SNAPSHOT", "policy_snapshot")
    for path in sorted((REPOSITORY_ROOT / "backend" / "app").rglob("*.py")):
        contents = path.read_text(encoding="utf-8")
        assert not any(token in contents for token in forbidden), path


def test_resolution_interval_follows_month_comparison_boundaries() -> None:
    assert policy_snapshot.resolution_interval(date(2026, 6, 1), None) == {
        "first_resolved_month": "2026-06",
        "last_resolved_month": None,
    }
    assert policy_snapshot.resolution_interval(date(2026, 6, 15), date(2026, 8, 1)) == {
        "first_resolved_month": "2026-07",
        "last_resolved_month": "2026-08",
    }
    assert policy_snapshot.resolution_interval(date(2026, 6, 15), date(2026, 6, 30)) == {
        "first_resolved_month": None,
        "last_resolved_month": None,
    }
