"""Employee-controlled disclosure module (ACCESS-001..016).

Security core: access requests, scoped grants over frozen append-only month
snapshots, recipient registry, fingerprint seam, and the delete guard for
disclosed months. Purpose-specific submission packages live in this module
too, implemented on top of the core.
"""

from app.disclosure.fingerprint import payload_fingerprint
from app.disclosure.guards import assert_not_disclosed
from app.disclosure.models import (
    AccessRequest,
    AccessRequestStatus,
    DisclosureGrant,
    SubmissionPackage,
    SubmissionPackageStatus,
    SubmissionRecipient,
)

__all__ = [
    "AccessRequest",
    "AccessRequestStatus",
    "DisclosureGrant",
    "SubmissionPackage",
    "SubmissionPackageStatus",
    "SubmissionRecipient",
    "assert_not_disclosed",
    "payload_fingerprint",
]
