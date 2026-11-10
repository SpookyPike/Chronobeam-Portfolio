"""Deterministic payload fingerprint.

The fingerprint seals the EXACT minimized projection that a recipient will
see: preview endpoints return it, and submission creation verifies it against
a recomputation — changed facts produce a different payload and therefore a
``SUBMISSION_CHANGED`` conflict (re-preview), so an employee can never
confirm a package whose contents differ from what they reviewed.

Canonical form: sorted keys, compact separators, JSON-safe values
(``mode="json"`` serialization) — the same dict on both sides by
construction, so the hash is byte-stable across calls and processes.
"""

import hashlib
import json
from collections.abc import Mapping
from typing import Any

# The fingerprint must never cover the fields that carry it (self-reference)
# or employee-private findings that are deliberately excluded from packages.
FINGERPRINT_EXCLUDE = frozenset({"findings", "preview_fingerprint"})


def payload_dict(model: Any) -> dict[str, Any]:
    """The exact recipient-visible projection for a preview model or dict.

    Excludes employee-private findings (ACCESS-003: findings never become
    HR-visible automatically) and the self-referential fingerprint field.
    """
    data = model if isinstance(model, dict) else model.model_dump(mode="json")
    return {key: value for key, value in data.items() if key not in FINGERPRINT_EXCLUDE}


def payload_fingerprint(payload: Mapping[str, Any]) -> str:
    """sha256 over the canonical JSON of the recipient-visible payload."""
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def with_preview_fingerprint(model: Any) -> Any:
    """Attach the payload fingerprint to a preview response model.

    Both the preview path and the submission path call ``payload_dict`` +
    ``payload_fingerprint``, so the fingerprint the employee previews is by
    construction the fingerprint the submission verifies (D9, HIGH-3:
    employee preview == recipient payload).
    """
    model.preview_fingerprint = payload_fingerprint(payload_dict(model))
    return model
