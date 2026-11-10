"""F8.7 — the balance reminder stays an explicit NO-OP, with its precondition recorded.

The deferred actionable balance/year-end reminder has a stated precondition: the
balance must contain **no unresolved ``WT-FLEX-009`` components**. Concretely, the
edition resolving for the target month must carry **BOTH**

1. the corrected **paid-overtime** account-credit semantics (time remunerated
   rather than compensated in time must not credit the account), and
2. the corrected **OCD account-credit** semantics (the separately-gated OCD slice).

``phase-2-ordinary-v2`` alone is **not** sufficient. Neither prerequisite is live,
so F8-7 adds **no** reminder, counter, badge or call to action, and does not
relabel the balance "correct"/"final"/"settled". The deliverable is therefore this
guard: while the precondition is unmet, an actionable balance prompt must not
appear on a balance-bearing surface — and the in-code deferral that records why
must still be there.

The guard is deliberately **self-releasing**: if the prerequisites ever legitimately
land, ``_precondition_met`` flips and the scan stops applying — but the
"deferral is still documented" assertion fails instead, forcing a deliberate
decision rather than a silent vacuous pass.
"""

import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from app.policy.editions import (
    DEFAULT_ORDINARY_PARAMETERS,
    OCD_RULE_SET_V1,
    SOURCE_MANIFEST_HASH,
    OrdinaryTimeParameters,
    PolicyEdition,
    UnknownPolicyEdition,
    resolve_edition,
)
from app.policy.models import PolicyVersion, PolicyVersionStatus
from app.policy.service import POLICY_KEY as ORDINARY_POLICY_KEY
from app.policy.service import policy_version_for
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from tests.conftest import DEVELOPER_PRINCIPAL_HEADER

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The OCD credit's own policy key (``app/policy/service.py``'s purpose map). The
#: corrected OCD half can only arrive as a superseding edition of THIS key.
OCD_POLICY_KEY = "ocd-compensation"

#: The balance-bearing surfaces an actionable prompt would live on. Kept explicit:
#: a new surface must be added deliberately, and the deferral assertion below is
#: what forces that decision.
#:
#: ``review.tsx`` is here because ``MonthReviewPage`` renders "Balance at end of
#: this month" — it carries a balance, so an actionable prompt added there would
#: otherwise evade this guard entirely.
BALANCE_SURFACES = (
    REPO_ROOT / "frontend" / "src" / "pages" / "month.tsx",
    REPO_ROOT / "frontend" / "src" / "pages" / "dashboard.tsx",
    REPO_ROOT / "frontend" / "src" / "pages" / "review.tsx",
)

#: The in-code record of WHY there is no prompt. It is the deferral's own evidence,
#: so its comment block is stripped before the scan and asserted to still be there.
DEFERRAL_SENTINEL = "Deliberately NO balance reminder"

#: Wording that would make the balance actionable. It deliberately INCLUDES the
#: words the deferral note uses ("reminder", "call to action"): the note's own block
#: is stripped first, so only a NEW occurrence can match.
#:
#: A bare "badge" is deliberately NOT a marker: the surfaces legitimately use
#: ``source-badge`` (an interval's provenance) and ``private-badge`` (the privacy
#: marker), neither of which is a balance prompt. A badge ATTACHED to the balance is
#: matched by the patterns below instead.
ACTIONABLE_PATTERNS = (
    r"reminder",
    r"call to action",
    r"action required",
    r"you must",
    r"reduce your",
    r"outstanding balance",
    r"final balance",
    r"correct balance",
    r"settle your",
    r"balance[-_ ]?badge",
    r"badge[-_ ]?balance",
    r"balance[-_ ]?(alert|notice|prompt|counter)",
)

#: A month the ordinary edition covers, used to evaluate the precondition.
TARGET_MONTH = date(2026, 8, 1)


def _resolved_parameters(session: Session):
    """The parameters of the edition that resolves for the target month.

    Read through ``resolve_edition`` rather than the imported ``EDITIONS`` snapshot,
    so a monkeypatched registry (the simulation in the test below) is actually
    visible — reading the module-level name directly would silently ignore it.
    """
    version = policy_version_for(session, TARGET_MONTH)
    return resolve_edition(version.engine_rule_set).parameters


def _paid_overtime_is_corrected(parameters: object) -> bool:
    """``WT-FLEX-009``'s paid half: paid overtime must NOT credit the account."""
    if not isinstance(parameters, OrdinaryTimeParameters):
        return False
    return parameters.credit_by_work_type.get("OVERTIME_PAID") is False


def _ocd_credit_is_corrected(session: Session) -> bool:
    """``WT-FLEX-009``'s OCD half: a **superseding, resolvable** OCD edition resolves.

    Read from the effective-dated row, never hard-coded. The correction can only
    arrive as a new edition of the ``ocd-compensation`` policy — the OCD mechanism,
    conditional fingerprint key, ``EINV-15`` rewrite, bounded reopen and parameters
    land as ONE unit behind ``OPEN-02``/``OPEN-03``/``OPEN-04``/``OPEN-10`` — so the
    prerequisite is "the shipped ``phase-2-ocd-v1`` no longer resolves for this month".
    A row naming an unregistered rule set is a *phantom edition*, not a landed
    correction, so it counts as unmet.

    An earlier version returned a literal ``False``. That made the scan's
    self-release branch **unreachable**: however the database changed, the
    precondition could never flip, so the guard could not actually release. An
    independent review caught it; ``test_the_guard_releases_once_both_halves_land``
    now pins the reachable path.
    """
    row = session.scalar(
        select(PolicyVersion)
        .where(
            PolicyVersion.policy_key == OCD_POLICY_KEY,
            PolicyVersion.effective_from <= TARGET_MONTH,
            or_(
                PolicyVersion.effective_to.is_(None),
                PolicyVersion.effective_to >= TARGET_MONTH,
            ),
        )
        .order_by(PolicyVersion.effective_from.desc(), PolicyVersion.version.desc())
        .limit(1)
    )
    if row is None or row.engine_rule_set == OCD_RULE_SET_V1:
        return False
    try:
        resolve_edition(row.engine_rule_set)
    except UnknownPolicyEdition:
        return False
    return True


def _precondition_met(session: Session) -> bool:
    return _paid_overtime_is_corrected(_resolved_parameters(session)) and _ocd_credit_is_corrected(
        session
    )


def _publish_superseding_edition(
    session: Session, monkeypatch: pytest.MonkeyPatch, *, policy_key: str, edition: PolicyEdition
) -> None:
    """Publish ``edition`` the way a real migration MUST: as a HANDOVER.

    The predecessor's ``effective_to`` is closed the day before the boundary, so the
    range set stays **non-overlapping** — the state the restore verifier rejects
    otherwise. Registering the code alone changes nothing, because resolution reads
    the ROW; and a row beside an open-ended predecessor is an overlap, not a handover.
    (The same defect class an independent review found in the travel fixture in
    round 1, fixed here for the same reason.)
    """
    import app.policy.editions as editions_module

    predecessor = session.scalar(
        select(PolicyVersion)
        .where(
            PolicyVersion.policy_key == policy_key,
            PolicyVersion.effective_to.is_(None),
        )
        .order_by(PolicyVersion.effective_from.desc())
        .limit(1)
    )
    assert predecessor is not None, f"the shipped {policy_key} edition must be open-ended"
    predecessor.effective_to = TARGET_MONTH - timedelta(days=1)
    # Accumulate onto the LIVE registry, so two successive publications both survive.
    monkeypatch.setattr(
        "app.policy.editions.EDITIONS",
        {**editions_module.EDITIONS, edition.rule_set: edition},
    )
    session.add(
        PolicyVersion(
            policy_key=policy_key,
            version=edition.version,
            effective_from=TARGET_MONTH,
            effective_to=None,
            status=PolicyVersionStatus.PROVISIONAL,
            engine_rule_set=edition.rule_set,
            source_manifest_hash=SOURCE_MANIFEST_HASH,
        )
    )
    session.flush()
    # Pin the HANDOVER itself: exactly one open-ended edition may remain per key.
    # Overlapping ranges are what the restore verifier rejects, so a fixture that
    # silently produced them would teach the wrong migration.
    open_rows = session.scalars(
        select(PolicyVersion).where(
            PolicyVersion.policy_key == policy_key, PolicyVersion.effective_to.is_(None)
        )
    ).all()
    assert len(open_rows) == 1, (
        f"{policy_key} has {len(open_rows)} open-ended editions — that is an overlap, "
        "not a handover"
    )


def _strip_documented_deferrals(text: str) -> str:
    """Drop every comment block that carries the deferral sentinel.

    Only blocks CONTAINING the sentinel are removed, so a new note elsewhere cannot
    smuggle a prompt past the guard.
    """

    def _keep(match: re.Match[str]) -> str:
        return "" if DEFERRAL_SENTINEL in match.group(0) else match.group(0)

    return re.sub(r"/\*.*?\*/", _keep, text, flags=re.DOTALL)


# ---------------------------------------------------------------------------
# The precondition is unmet — asserted positively, not vacuously
# ---------------------------------------------------------------------------


def test_the_balance_reminder_precondition_is_unmet(db_session: Session) -> None:
    parameters = _resolved_parameters(db_session)
    assert isinstance(parameters, OrdinaryTimeParameters)
    assert parameters.credit_by_work_type["OVERTIME_PAID"] is True, (
        "the premise: the resolved edition still credits paid overtime"
    )
    assert _paid_overtime_is_corrected(parameters) is False
    assert _ocd_credit_is_corrected(db_session) is False
    assert _precondition_met(db_session) is False


def test_the_precondition_requires_both_corrected_editions(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ordinary-v2`` ALONE is insufficient — the OCD half is also required.

    The paid-overtime correction is simulated the way it would really arrive: a
    registered executable edition PLUS the ``policy_version`` row that resolves for
    the month. Registering the code alone would change nothing, because resolution
    reads the row — so the test first PROVES the simulation took effect, and only
    then asserts that the precondition is still unmet.
    """
    corrected = PolicyEdition(
        rule_set="test-ordinary-paid-corrected",
        version="test-paid-corrected",
        algorithm="ordinary-v1",
        parameters=OrdinaryTimeParameters(
            credit_by_work_type={
                **DEFAULT_ORDINARY_PARAMETERS.credit_by_work_type,
                "OVERTIME_PAID": False,
            }
        ),
        note="test-only paid-overtime correction",
    )
    _publish_superseding_edition(
        db_session, monkeypatch, policy_key=ORDINARY_POLICY_KEY, edition=corrected
    )

    # Positive control: the paid-overtime correction IS now live for the month.
    assert _paid_overtime_is_corrected(_resolved_parameters(db_session)) is True
    assert _ocd_credit_is_corrected(db_session) is False
    assert _precondition_met(db_session) is False, (
        "the OCD account-credit half is still absent, so the reminder must stay deferred"
    )


def test_the_guard_releases_once_both_halves_land(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The self-release path is REACHABLE — proven, not asserted in a docstring.

    This is the counterpart an independent review demanded: ``_precondition_met`` must
    be able to become ``True`` from real database state. Both corrections are published
    as handovers (a superseding registered edition PLUS the resolving row), and the
    guard must then release — the scan reports itself skipped, and the deferral-note
    guard is the one that would force a deliberate decision.

    Without this test the predicate could silently become a constant again, and the
    "self-releasing" claim would be unfalsifiable prose.
    """
    paid = PolicyEdition(
        rule_set="test-ordinary-paid-corrected",
        version="test-paid-corrected",
        algorithm="ordinary-v1",
        parameters=OrdinaryTimeParameters(
            credit_by_work_type={
                **DEFAULT_ORDINARY_PARAMETERS.credit_by_work_type,
                "OVERTIME_PAID": False,
            }
        ),
        note="test-only paid-overtime correction",
    )
    ocd = PolicyEdition(
        rule_set="test-ocd-credit-corrected",
        version="test-ocd-corrected",
        algorithm="ocd-v1",
        parameters=None,
        note="test-only OCD account-credit correction",
    )
    _publish_superseding_edition(
        db_session, monkeypatch, policy_key=ORDINARY_POLICY_KEY, edition=paid
    )
    _publish_superseding_edition(db_session, monkeypatch, policy_key=OCD_POLICY_KEY, edition=ocd)

    # Both halves are now live from real DB state — the release is reachable.
    assert _paid_overtime_is_corrected(_resolved_parameters(db_session)) is True
    assert _ocd_credit_is_corrected(db_session) is True
    assert _precondition_met(db_session) is True, (
        "both corrected editions resolve, so the precondition IS met and the scan must release"
    )


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


def test_no_actionable_balance_prompt_exists_on_a_balance_surface(db_session: Session) -> None:
    """A balance-bearing surface must not gain reminder/badge/action wording.

    The scan strips the documented deferral note (which names those words on
    purpose), so only a NEW occurrence can fail this test.

    **It applies only while the precondition is unmet.** Once both corrected credit
    editions legitimately resolve, a balance reminder is no longer misleading, so the
    scan must release itself and let
    ``test_the_deferral_is_still_documented_where_the_numbers_are`` take over (that one
    then goes red, making the note's removal deliberate). Without this precondition
    check the guard would keep failing after a legitimate implementation — claiming to
    be self-releasing while not being so, which is the defect an independent review
    found here.
    """
    if _precondition_met(db_session):
        pytest.skip(
            "both corrected credit editions resolve for the month, so a balance reminder "
            "is legitimate: this scan releases itself and the deferral-note guard takes over"
        )
    violations: list[str] = []
    for path in BALANCE_SURFACES:
        text = _strip_documented_deferrals(path.read_text(encoding="utf-8")).lower()
        violations.extend(
            f"{path.name}: {pattern!r}"
            for pattern in ACTIONABLE_PATTERNS
            if re.search(pattern, text)
        )
    assert not violations, (
        "an actionable balance prompt appeared while the WT-FLEX-009 components are "
        "still unresolved (F8-7 is a no-op): " + "; ".join(violations)
    )


def test_the_deferral_is_still_documented_where_the_numbers_are() -> None:
    """The honest form of "deferred": the reason is recorded AT the surface.

    This is the guard's release valve. When the prerequisites land, the scan above
    stops applying — and this one goes red instead, so removing the deferral note
    becomes a deliberate act rather than a silent one.
    """
    for path in BALANCE_SURFACES:
        text = path.read_text(encoding="utf-8")
        if DEFERRAL_SENTINEL in text:
            assert "WT-FLEX-009" in text, f"{path.name} must name the unresolved component"
            return
    pytest.fail(
        "no balance surface carries the WT-FLEX-009 deferral note; if the reminder was "
        "deliberately implemented, update F8-7's record and this guard together"
    )


def test_the_metrics_limits_are_reported_as_unenforced(db_client, db_session: Session) -> None:
    """A count that is not enforced must say so in the API payload, not only in prose.

    ``WT-FLEX-008`` stays recorded/not-enforced while the HALF-day counting detail is
    open, so the server must not present the figure as an entitlement.
    """
    response = db_client.get(
        "/api/v1/me/day-status", headers=DEVELOPER_PRINCIPAL_HEADER, params={"period": "2026-08"}
    )
    assert response.status_code == 200, response.text
    metrics = response.json()["compensation_metrics"]
    assert metrics["limits"]["enforced"] is False
    assert "not a limit we apply" in metrics["counting_note"]
    for token in ("OPEN-", "INST-", "MISSING-", "SRC-"):
        assert token not in metrics["counting_note"], (
            f"an internal register token ({token}) reached the employee-facing note"
        )
