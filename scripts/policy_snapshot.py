#!/usr/bin/env python3
"""Generate the deterministic, review-only policy snapshot.

This script is intentionally outside ``backend/app``. Policy values remain
owned by the typed code editions and migration-seeded ``policy_version`` rows;
this file only projects those existing sources for reviewers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.policy.editions import (  # noqa: E402
    EDITIONS,
    HISTORICAL_RULE_SETS,
    TOTAL_BASE_TRAVEL_PARAMETERS,
    TRAVEL_RULE_SET_V2,
    OrdinaryTimeParameters,
    PolicyEdition,
    TravelParameters,
)
from app.policy.models import PolicyVersion  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

SCHEMA_VERSION = 1
DIGEST_FIELD = "content_sha256"
RULE_CATALOGUE = ROOT / "docs" / "RULE_CATALOGUE.md"
SNAPSHOT_PATH = ROOT / "docs" / "review" / "POLICY_SNAPSHOT.json"
EXPECTED_DATABASE_NAME = "chronobeam_policy_snapshot"


class SnapshotError(ValueError):
    """Source data cannot be represented as an authoritative snapshot."""


def _json_value(value: Any) -> Any:
    """Convert typed policy data to JSON primitives deterministically."""
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        converted: dict[str, Any] = {}
        for key, item in value.items():
            string_key = str(key.value) if isinstance(key, Enum) else str(key)
            if string_key in converted:
                raise SnapshotError(f"mapping keys collide after string conversion: {string_key!r}")
            converted[string_key] = _json_value(item)
        return {key: converted[key] for key in sorted(converted)}
    if isinstance(value, (set, frozenset)):
        converted = [_json_value(item) for item in value]
        return sorted(converted, key=_canonical_json)
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise SnapshotError("non-finite float cannot be serialized deterministically")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise SnapshotError(f"unsupported value type: {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(snapshot: Mapping[str, Any]) -> str:
    without_digest = {key: value for key, value in snapshot.items() if key != DIGEST_FIELD}
    return hashlib.sha256(_canonical_json(without_digest).encode("utf-8")).hexdigest()


def render_snapshot(snapshot: Mapping[str, Any]) -> bytes:
    """Return stable pretty-printed UTF-8 JSON with a final newline."""
    document = _json_value(dict(snapshot))
    document[DIGEST_FIELD] = _digest(document)
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _month_start_at_or_after(value: date) -> date:
    if value.day == 1:
        return value
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _month_start_at_or_before(value: date) -> date:
    return date(value.year, value.month, 1)


def resolution_interval(effective_from: date, effective_to: date | None) -> dict[str, str | None]:
    """Describe the month-start inputs covered by the resolver's date comparisons."""
    first_month = _month_start_at_or_after(effective_from)
    last_month = None if effective_to is None else _month_start_at_or_before(effective_to)
    if last_month is not None and last_month < first_month:
        return {"first_resolved_month": None, "last_resolved_month": None}
    return {
        "first_resolved_month": first_month.strftime("%Y-%m"),
        "last_resolved_month": None if last_month is None else last_month.strftime("%Y-%m"),
    }


#: One row of the catalogue's markdown tables: | `RULE-ID` | title | basis | `STATUS` |
RULE_ROW = re.compile(
    r"^\| `(?P<rule_id>[A-Z][A-Z0-9-]+)` \| (?P<title>[^|]+?) \| (?P<basis>[^|]+?) "
    r"\| `(?P<status>[A-Z ]+)` \|$",
    re.M,
)


def _catalogue_rules() -> list[dict[str, str]]:
    """Read rule identity, basis and status from the canonical rule catalogue."""
    text = RULE_CATALOGUE.read_text(encoding="utf-8")
    rules = [
        {
            "rule_id": match.group("rule_id"),
            "title": match.group("title").strip(),
            "basis": match.group("basis").replace("`", "").strip(),
            "status": match.group("status").strip(),
        }
        for match in RULE_ROW.finditer(text)
    ]
    if not rules:
        raise SnapshotError("no rules parsed from the rule catalogue")
    return sorted(rules, key=lambda item: item["rule_id"])


def _rules_by_basis(rules: Sequence[Mapping[str, str]]) -> dict[str, list[str]]:
    """Group rule ids by the provenance token that governs them."""
    grouped: dict[str, list[str]] = {}
    for item in rules:
        for token in sorted(set(re.findall(r"\b(?:LAW|POL)-\d{2}\b", item["basis"]))):
            grouped.setdefault(token, []).append(item["rule_id"])
    return {token: sorted(ids) for token, ids in sorted(grouped.items())}


def _sample_policy_rules(rules: Sequence[Mapping[str, str]]) -> list[dict[str, Any]]:
    """Rules whose parameters are sample policy rather than statutory."""
    return [
        {
            "marker": "SAMPLE_POLICY_PARAMETERS",
            "rule_id": item["rule_id"],
            "basis": item["basis"],
            "status": item["status"],
        }
        for item in rules
        if "POL-" in item["basis"]
    ]


def _markers(rules: Sequence[Mapping[str, str]]) -> dict[str, list[dict[str, Any]]]:
    """Report what the shipped editions deliberately do not decide."""
    blocked = [
        {"marker": "NO_VERDICT", "rule_id": item["rule_id"], "basis": item["basis"]}
        for item in rules
        if item["status"] == "BLOCKED"
    ]
    return {
        "no_verdict": sorted(blocked, key=lambda item: item["rule_id"]),
        "sample_policy_parameters": _sample_policy_rules(rules),
        "not_encoded": [
            {
                "marker": "not encoded by the selected policy",
                "field": "year_end_payout_month",
                "decision": (
                    "The sample policy leaves the payout month open, so no edition encodes or "
                    "asserts one."
                ),
                "rule_ids": ["WT-FLEX-006", "WT-FLEX-007"],
            }
        ],
    }


def _row_to_json(row: PolicyVersion) -> dict[str, Any]:
    return {
        "policy_key": row.policy_key,
        "version": row.version,
        "effective_from": row.effective_from,
        "effective_to": row.effective_to,
        "status": row.status,
        "engine_rule_set": row.engine_rule_set,
        "source_manifest_hash": row.source_manifest_hash,
    }


def build_snapshot(session: Session) -> dict[str, Any]:
    """Build a snapshot from code registries and rows already present in a DB."""
    rows = session.scalars(
        select(PolicyVersion).order_by(
            PolicyVersion.policy_key,
            PolicyVersion.engine_rule_set,
            PolicyVersion.version,
            PolicyVersion.effective_from,
        )
    ).all()
    rules = _catalogue_rules()
    rows_by_rule_set: dict[str, list[PolicyVersion]] = {}
    for row in rows:
        rows_by_rule_set.setdefault(row.engine_rule_set, []).append(row)

    editions: list[dict[str, Any]] = []
    for key in sorted(EDITIONS):
        edition: PolicyEdition = EDITIONS[key]
        if edition.rule_set != key:
            raise SnapshotError(f"edition key and rule_set differ for {key!r}")
        matches = rows_by_rule_set.get(key, [])
        if len(matches) != 1:
            raise SnapshotError(
                f"expected one migration-seeded row for {key!r}; found {len(matches)}"
            )
        row = matches[0]
        if row.version != edition.version:
            raise SnapshotError(f"database/code version mismatch for {key!r}")
        if row.source_manifest_hash != edition.source_manifest_hash:
            raise SnapshotError(f"database/code source-manifest hash mismatch for {key!r}")
        row_data = _row_to_json(row)
        parameters = _json_value(edition.parameters)
        if isinstance(edition.parameters, OrdinaryTimeParameters):
            parameter_kind = "OrdinaryTimeParameters"
        elif isinstance(edition.parameters, TravelParameters):
            parameter_kind = "TravelParameters"
        elif edition.parameters is None:
            parameter_kind = None
        else:
            raise SnapshotError(f"unsupported parameter record for {key!r}")
        editions.append(
            {
                "registered_identity": {
                    "rule_set": edition.rule_set,
                    "version": edition.version,
                    "algorithm": edition.algorithm,
                    "source_manifest_hash": edition.source_manifest_hash,
                },
                "database_row": row_data,
                "resolution_interval": resolution_interval(row.effective_from, row.effective_to),
                "parameters": {"type": parameter_kind, "values": parameters},
            }
        )

    unknown_rows = sorted(set(rows_by_rule_set) - set(EDITIONS))
    if unknown_rows:
        raise SnapshotError(f"database contains unregistered engine rule sets: {unknown_rows!r}")

    historical = sorted(HISTORICAL_RULE_SETS)
    designed: list[dict[str, Any]] = []
    migration_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((BACKEND / "alembic" / "versions").glob("*.py"))
    )
    for rule_set in ("phase-2-ordinary-v2", TRAVEL_RULE_SET_V2):
        params = TOTAL_BASE_TRAVEL_PARAMETERS if rule_set == TRAVEL_RULE_SET_V2 else None
        designed.append(
            {
                "rule_set": rule_set,
                "publication_state": "UNPUBLISHED",
                "registered": rule_set in EDITIONS,
                "shipped_history_allowlisted": rule_set in HISTORICAL_RULE_SETS,
                "migration_seed_present": rule_set in migration_text,
                "database_row_present": rule_set in rows_by_rule_set,
                "parameters": {
                    "type": "TravelParameters" if params is not None else None,
                    "values": _json_value(params),
                    "source": "code-owned designed value"
                    if params is not None
                    else "not imported from test-only corrected ordinary parameters",
                },
            }
        )
    markers = _markers(rules)
    for item in editions:
        if item["parameters"]["type"] is None:
            markers["not_encoded"].append(
                {
                    "marker": "not encoded by the selected policy",
                    "field": "parameters.values",
                    "decision": (
                        "This edition carries identity only; its constants are code-owned and "
                        "not yet parameterised."
                    ),
                    "rule_set": item["registered_identity"]["rule_set"],
                }
            )
    markers["not_encoded"].sort(key=lambda item: (item.get("rule_set", ""), item["field"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "source_catalogue": "docs/RULE_CATALOGUE.md",
        "rule_basis_index": _rules_by_basis(rules),
        "registered_editions": editions,
        "shipped_history_allowlist": historical,
        "designed_unpublished_editions": sorted(designed, key=lambda item: item["rule_set"]),
        "markers": markers,
    }


def generate(engine: Engine) -> bytes:
    with Session(engine) as session:
        return render_snapshot(build_snapshot(session))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        required=True,
        help=f"explicit URL for the disposable {EXPECTED_DATABASE_NAME} scratch database",
    )
    parser.add_argument("--output", type=Path, default=SNAPSHOT_PATH)
    args = parser.parse_args(argv)

    from app.db import create_engine
    from sqlalchemy.engine import make_url

    url = make_url(args.database_url)
    if url.database != EXPECTED_DATABASE_NAME:
        parser.error(
            f"database name must be {EXPECTED_DATABASE_NAME!r}; refusing to read {url.database!r}"
        )
    engine = create_engine(args.database_url, pool_pre_ping=True)
    try:
        payload = generate(engine)
    except Exception as exc:
        parser.exit(2, f"policy snapshot failed: {exc}\n")
    finally:
        engine.dispose()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
