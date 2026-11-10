"""Canonical application release metadata."""

import os

APP_VERSION = "0.3.0"


def build_sha() -> str:
    """Return non-secret build provenance, or an empty value for local runs."""
    return os.environ.get("CHRONOBEAM_BUILD_SHA", "")
