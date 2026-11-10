"""Emit a stable digest of the backend's HTTP contract.

Why this exists: the frontend keeps a hand-written type surface
(``frontend/src/lib/api.ts``), and a backend schema change could previously land
with nothing anywhere failing — the drift was invisible until a page reached for
a field the type did not declare (the confirmed case: ``preview_fingerprint`` on
the three preview responses, worked around with a cast).

What it does NOT do: it is not a code generator and not a type checker. The
obvious off-the-shelf generator (``openapi-typescript``) cannot run in this
repository — it drives the TypeScript compiler API, which TypeScript 7 no longer
ships (the compiler moved to Go), so it fails at import. Rather than pin a second
TypeScript major purely to run a codegen, this emits the *resolved shape* of the
response models — types, enums, nesting, nullability — which is the part that
drifts. (A property-name-only digest was tried first and rejected in review: a
field changing type left it byte-identical.)

The digest is committed and regenerated in CI, so a backend contract change
becomes a visible diff that a reviewer must acknowledge — and the backend test
``test_openapi_exposes_the_high_risk_response_models`` keeps the models from
being silently dropped. The residual (the hand-written interfaces are not
mechanically tied to these property sets) is recorded in the F7 closure record.

It lives at the REPOSITORY ROOT on purpose: inside ``frontend/`` it would be owned
by oxfmt, which formats JSON differently from this emitter, so the check would diff
against itself forever.

Run: ``just api-contract`` (regenerate) / ``just api-check`` (fail on a diff).
"""

from __future__ import annotations

import json
import sys

from app.main import app

#: Response families whose drift would silently mislead a page. Kept explicit so
#: a renamed or dropped model fails loudly here rather than in the browser.
CONTRACT_SCHEMAS = (
    "ShiftReportPreviewOut",
    "OcdCompensationPreviewOut",
    "OvertimePreviewOut",
    "DayDetail",
    "DayStatusOut",
    "DayStatusMonthOut",
    "MonthSummary",
    "SubmissionPackageOut",
    "RecipientSubmissionPackageOut",
    "DisclosureGrantOut",
    # Self-onboarding: the frontend keeps a hand-written interface for this
    # response, so a backend reshape of it must be a visible diff here too.
    "OnboardingStateOut",
    # Users & Access: same reason. The admin list gained ``roles`` (and the endpoint
    # gained sort/direction/limit), and the frontend keeps its own interface for the
    # row — precisely the drift this digest exists to make visible.
    "AdminUserSummary",
    "AdminUserDetail",
    # The System card: the frontend keeps its own ``AdminSystem`` interface for this
    # payload too, and the calendar coverage field is a nested object — exactly the
    # reshape this digest exists to make a reviewable diff.
    "AdminSystemOut",
)


#: Keys that carry MEANING for a consumer of the contract. Everything else
#: (``description``, ``title``, ``examples``, ``default``) is prose or an
#: implementation default, and including it would make the digest churn for
#: reasons that cannot break the frontend.
CONTRACT_KEYS = (
    "$ref",
    "type",
    "format",
    "enum",
    "const",
    "required",
    "items",
    "properties",
    "additionalProperties",
    "anyOf",
    "oneOf",
    "allOf",
    "nullable",
)


def _normalise(node: object, schemas: dict[str, object], seen: frozenset[str]) -> object:
    """Recursively keep only the contract-bearing parts of a schema node.

    Recording just the property NAMES (the first version of this digest) could
    not see a field change type, become nullable, gain an enum, or have a nested
    object reshaped — all of which break the frontend while leaving the digest
    byte-identical. This keeps the structure, so each of those is a diff.

    Two details that are load-bearing:

    ``properties`` is handled separately because it is a MAP from property name
    to schema: its keys are arbitrary names, not contract keywords, so filtering
    it like every other dict would drop every property and produce an empty —
    and therefore vacuous — digest.

    ``$ref`` is INLINED rather than recorded as a pointer. A pointer is opaque to
    this digest, so a change to any referenced model (e.g. ``DaySummary`` nested
    inside ``MonthSummary``) would leave the digest byte-identical — verified: a
    field changing ``int`` to ``str`` in a nested model produced a zero-line
    diff. Inlining is also the right semantic, because the frontend consumes
    field shapes, never backend schema names. ``seen`` breaks cycles, which the
    document can contain.
    """
    if isinstance(node, list):
        return [_normalise(item, schemas, seen) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, object] = {}
    for key, value in sorted(node.items()):
        if value is None:
            continue
        if key == "$ref" and isinstance(value, str):
            name = value.rsplit("/", 1)[-1]
            target = schemas.get(name)
            if target is None or name in seen:
                out["$ref"] = value
            else:
                out[key] = _normalise(target, schemas, seen | {name})
        elif key == "properties" and isinstance(value, dict):
            out["properties"] = {
                name: _normalise(schema, schemas, seen) for name, schema in sorted(value.items())
            }
        elif key in CONTRACT_KEYS:
            out[key] = _normalise(value, schemas, seen)
    return out


def build_digest() -> dict[str, object]:
    document = app.openapi()
    schemas = document.get("components", {}).get("schemas", {})
    digest: dict[str, object] = {}
    for name in CONTRACT_SCHEMAS:
        schema = schemas.get(name)
        if schema is None:
            raise SystemExit(f"contract schema {name!r} is missing from the OpenAPI document")
        digest[name] = _normalise(schema, schemas, frozenset({name}))
    return digest


def main() -> None:
    json.dump(build_digest(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
