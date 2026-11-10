"""F8 presentation ergonomics — `just demo` / `demo-open` / `demo-down`.

The demo bring-up is dev tooling, so the properties worth pinning are the ones a
typo would silently break *during a presentation*:

- the recipes the presenter is told to run actually exist;
- every persona origin maps to a subject the dev-auth seam knows (a typo would
  surface as a 401 in a live window, not as a failed build);
- the proxy's env-var allowlist and the script's persona list cannot drift apart,
  which is what keeps `CHRONOBEAM_DEV_PRINCIPAL` from ever naming a subject
  outside the demo set;
- nothing in the script kills processes it did not start.

The dev-auth seam's own ergonomics belong to the same file (acceptance ledger
B1): a persona provisioned through the product and *activated* must be openable
with its own subject, with no `identity_admin set-binding` and no fixture edit —
and the two ways it must NOT resolve (an inactive row, a production
configuration) are pinned here too.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from app.auth.deps import EMPLOYEE_BASELINE_CAPABILITIES
from app.auth.dev_principals import DEV_PRINCIPALS
from app.identity.models import Employee
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "demo" / "demo.sh"
JUSTFILE = ROOT / "Justfile"
VITE_CONFIG = ROOT / "frontend" / "vite.config.ts"

# The dev-seam header, spelled literally: this file pins the wire contract, so
# importing the constant would hide a rename.
DEV_HEADER = "X-Chronobeam-Dev-Principal"

MONTHS = "/api/v1/me/months/2026-07"
INTERVALS = "/api/v1/me/work-intervals"
IDENTITY = "/api/v1/me"

# A subject deliberately absent from the fixture allowlist: this is the
# "provisioned through the product" case the DB fallback exists for.
PROVISIONED_SUBJECT = "new-hire"

# Resolved, not a bare name, so the call cannot pick up a different `bash`.
BASH = shutil.which("bash")

PERSONA_LINE = re.compile(r'^\s*"(?P<label>[^:"]+):(?P<port>\d+):(?P<subject>[a-z0-9-]+)"$')


def _justfile() -> str:
    return JUSTFILE.read_text(encoding="utf-8")


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _personas() -> list[tuple[str, int, str]]:
    """The `label:port:subject` triples the demo binds an origin to."""
    found = [
        (match["label"], int(match["port"]), match["subject"])
        for line in _script().splitlines()
        if (match := PERSONA_LINE.match(line))
    ]
    assert found, "no persona mapping found in scripts/demo/demo.sh"
    return found


def _proxy_allowlist() -> set[str]:
    """The subjects `CHRONOBEAM_DEV_PRINCIPAL` may bind (frontend/vite.config.ts)."""
    source = VITE_CONFIG.read_text(encoding="utf-8")
    block = re.search(r"DEMO_PRINCIPALS\s*=\s*\[(?P<body>[^\]]*)\]", source)
    assert block is not None, "DEMO_PRINCIPALS allowlist not found in vite.config.ts"
    return set(re.findall(r'"([^"]+)"', block["body"]))


def test_the_presenter_recipes_exist() -> None:
    justfile = _justfile()
    for recipe in ("demo:", "demo-open mode:", "demo-down:", "demo-reseed:"):
        assert f"\n{recipe}" in justfile, f"missing Justfile recipe: {recipe}"
    # `demo-open` takes the presentation mode, so both modes are reachable.
    assert "scripts/demo/demo.sh open {{mode}}" in justfile


@pytest.mark.skipif(BASH is None, reason="bash is required to exercise the demo script")
def test_demo_open_rejects_an_unknown_mode() -> None:
    """The mode argument is validated before anything is started."""
    assert BASH is not None  # narrows the resolved path for the type checker
    result = subprocess.run(  # noqa: S603 - fixed argv, no caller-supplied input
        [BASH, str(SCRIPT), "open", "bogus"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert "usage: demo.sh" in result.stderr


def test_every_demo_persona_is_a_registered_dev_principal() -> None:
    for label, _port, subject in _personas():
        assert subject.startswith("demo-"), f"{label} is not a demo-only subject: {subject}"
        assert subject in DEV_PRINCIPALS, f"{label} is unknown to the dev-auth seam: {subject}"


def test_the_personas_have_distinct_origins() -> None:
    ports = [port for _label, port, _subject in _personas()]
    assert len(ports) == len(set(ports)), f"personas share an origin: {ports}"
    # Distinct origins are the whole isolation mechanism, so the count matters:
    # Employee, HR, Admin, the clean-ledger employee, the recipient, and the
    # fact-free onboarding employee (whose start-state form is the only one that
    # can be demonstrated — see the note above ``DEMO_PERSONAS``).
    assert len(ports) == 6, f"expected the six presentation personas, got {ports}"


def test_the_proxy_allowlist_matches_the_persona_list() -> None:
    subjects = {subject for _label, _port, subject in _personas()}
    assert _proxy_allowlist() == subjects


def test_the_demo_script_only_stops_what_it_started() -> None:
    source = _script()
    for broad_kill in ("pkill", "killall", "pgrep"):
        assert broad_kill not in source, (
            f"demo.sh must not signal unrelated processes ({broad_kill})"
        )
    # It records the pids it starts and signals those process groups.
    assert "PID_DIR" in source and "alive_pid" in source


# ---------------------------------------------------------------------------
# Dev-seam ergonomics: a provisioned + ACTIVATED employee is addressable by its
# own subject, so onboarding needs no CLI rebinding (acceptance ledger B1).
# ---------------------------------------------------------------------------


def _headers(subject: str) -> dict[str, str]:
    return {DEV_HEADER: subject}


def _provision(session: Session, subject: str, *, active: bool) -> Employee:
    """The product's own provisioning shape, minus the HTTP layer.

    Mirrors ``admin/service.py::provision_user``: a mapped subject, inactive
    unless a separate activation step says otherwise.
    """
    assert subject not in DEV_PRINCIPALS, "pick a subject the fixture allowlist does not carry"
    employee = Employee(
        legal_name=f"New Hire ({subject})",
        external_employee_id=f"emp-{subject}",
        idp_subject=subject,
        active=active,
    )
    session.add(employee)
    session.flush()
    return employee


def test_a_mapped_but_inactive_subject_is_refused(
    db_client: TestClient, db_session: Session
) -> None:
    """Activation is the gate: a mapped-but-INACTIVE row must not sign in.

    The fixture allowlist is code-controlled; this fallback is data-controlled,
    so it fails closed — otherwise the dev header would be sign-in-capable for a
    deactivated account (``deps.py``'s mapped-but-inactive invariant).
    """
    _provision(db_session, PROVISIONED_SUBJECT, active=False)

    for method, path in [
        ("GET", IDENTITY),
        ("GET", MONTHS),
        ("POST", INTERVALS),
    ]:
        response = db_client.request(method, path, headers=_headers(PROVISIONED_SUBJECT))
        assert response.status_code == 401, (method, path)
        assert response.json()["detail"]["code"] == "AUTH_UNKNOWN_PRINCIPAL", (method, path)


def test_an_activated_provisioned_subject_resolves_with_the_employee_baseline(
    db_client: TestClient, db_session: Session
) -> None:
    """Activation alone makes the row addressable — exactly the baseline caps.

    Nothing is added by resolving through the DB: the capabilities are the ones
    ``effective_capabilities`` derives for a mapped+active row, so the dev seam
    shows the real derivation and not a fixture privilege.
    """
    _provision(db_session, PROVISIONED_SUBJECT, active=True)

    identity = db_client.get(IDENTITY, headers=_headers(PROVISIONED_SUBJECT))
    assert identity.status_code == 200, identity.text
    body = identity.json()
    assert body["subject"] == PROVISIONED_SUBJECT
    assert body["display_name"] == f"New Hire ({PROVISIONED_SUBJECT})"
    assert set(body["capabilities"]) == {
        capability.value for capability in EMPLOYEE_BASELINE_CAPABILITIES
    }

    # The row is really reachable, not merely authenticated: its own (empty)
    # ledger answers as the employee's.
    month = db_client.get(MONTHS, headers=_headers(PROVISIONED_SUBJECT))
    assert month.status_code == 200, month.text
    assert month.json()["interval_count"] == 0


def test_an_unmapped_unknown_subject_is_refused(db_client: TestClient) -> None:
    """The fallback is a row lookup, not JIT provisioning: no row, no principal."""
    assert "ghost" not in DEV_PRINCIPALS
    response = db_client.get(MONTHS, headers=_headers("ghost"))
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_UNKNOWN_PRINCIPAL"


def test_production_ignores_the_dev_header(
    production_db_client_factory, db_session: Session
) -> None:
    """The same ACTIVE mapped subject is still ignored under ``is_production``.

    The production branch reads only the browser-session cookie, so the dev
    header cannot authenticate there — and the answer is AUTH_REQUIRED, proving
    the dev branch (and therefore the DB fallback) was never reached.
    """
    _provision(db_session, PROVISIONED_SUBJECT, active=True)
    client = production_db_client_factory()

    response = client.get(MONTHS, headers=_headers(PROVISIONED_SUBJECT))
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_REQUIRED"
