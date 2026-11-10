"""F8.4 — the rule catalogue cannot drift silently from the code.

``docs/RULE_CATALOGUE.md`` is the tracked reviewer index. Every rule row is
``| `RULE-ID` | Title | Basis | `STATUS` |``, and the basis is either a public
statute (``LAW-*``) or a sample policy shipped with this demonstration build
(``POL-*``).

Two properties are enforced, because those are the two that break silently:

1. **Coverage.** Every rule ID the code refers to has a catalogue row. The code is
   the authority here: a rule the engine emits but the index omits is invisible to
   review, and nothing else in the build would notice.
2. **Honest status.** The declared status agrees with the row's own basis:
   ``STATUTORY`` only with a cited law, a sample-policy status only with that
   policy cited. A ``POL-*`` sample rule must never be presented as a legal
   requirement.

Deliberately NOT checked: the exact ID inventory (a pin made correcting the index
harder than reading it) and per-rule prose, which no mechanical check can judge.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
APP = REPO_ROOT / "backend" / "app"
CATALOGUE = REPO_ROOT / "docs" / "RULE_CATALOGUE.md"

#: The rule-ID families. Only the prefix is stable, so this is the vocabulary both
#: the code scan and the catalogue parse are built on.
RULE_ID_RE = re.compile(
    r"\b(?:WT-CORE|WT-FLEX|WT-STATUS|SHIFT|OCD|ALLOW|REMOTE|VAC|TRAVEL|ACCESS|ABSENCE)"
    r"(?:-(?:SUNDAY|LOCAL))?-\d{3}\b"
)
#: The rule cell of a catalogue row: one backticked rule ID and nothing else.
RULE_CELL_RE = re.compile(r"^`([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d{3})`$")

STATUTORY = "STATUTORY"
POLICY = "POLICY"
PRODUCT = "PRODUCT"
PARTIAL = "PARTIAL"
BLOCKED = "BLOCKED"
#: The catalogue's own status vocabulary, in the order its legend lists them.
STATUSES = (STATUTORY, POLICY, PRODUCT, PARTIAL, BLOCKED)
#: Statuses whose basis must be a sample policy, never presented as law.
SAMPLE_STATUSES = (POLICY, PRODUCT)

LAW_RE = re.compile(r"\bLAW-\d{2}\b")
POL_RE = re.compile(r"\bPOL-\d{2}\b")
INVARIANT_RE = re.compile(r"\bEINV-\d{2}[a-z]?\b")


def _catalogue_text() -> str:
    assert CATALOGUE.is_file(), f"{CATALOGUE.relative_to(REPO_ROOT)} is missing"
    return CATALOGUE.read_text(encoding="utf-8")


def _rows() -> dict[str, tuple[str, str, str]]:
    """``{rule id: (title, basis, status)}`` for every rule row of the catalogue.

    A row counts only when its first cell is a single backticked rule ID, so the
    legend, the header and the separator rows are skipped without a second parser.
    """
    rows: dict[str, tuple[str, str, str]] = {}
    for line in _catalogue_text().splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 4:
            continue
        match = RULE_CELL_RE.match(cells[0])
        if match is None:
            continue
        rule_id = match.group(1)
        assert rule_id not in rows, f"duplicate catalogue row for {rule_id}"
        rows[rule_id] = (cells[1], cells[2], cells[3].strip("` "))
    return rows


def _code_rule_ids() -> set[str]:
    """Every rule ID the application code names."""
    assert APP.is_dir(), f"{APP.relative_to(REPO_ROOT)} is missing"
    rule_ids: set[str] = set()
    for path in sorted(APP.rglob("*.py")):
        rule_ids.update(RULE_ID_RE.findall(path.read_text(encoding="utf-8")))
    return rule_ids


def test_the_catalogue_describes_rules() -> None:
    """Anti-vacuity: every check below is silent if the parser reads nothing."""
    rows = _rows()
    assert len(rows) > 50, f"only {len(rows)} catalogue rows parsed — parser or format broke"


def test_every_rule_id_used_in_the_code_has_a_catalogue_row() -> None:
    """A rule the code emits but the index omits is invisible to review."""
    code_ids = _code_rule_ids()
    assert code_ids, "no rule IDs found in backend/app — the scan is broken, not the code"
    missing = sorted(code_ids - set(_rows()))
    assert not missing, (
        f"rule IDs used by the code with no catalogue row: {missing} — add the row or "
        "the rule stays unreviewable"
    )


def test_every_catalogue_row_is_complete_and_uses_the_status_vocabulary() -> None:
    """Every row names a title, a basis and one status from the legend."""
    offenders: list[str] = []
    for rule_id, (title, basis, status) in sorted(_rows().items()):
        if not title:
            offenders.append(f"{rule_id}: no title")
        if not basis:
            offenders.append(f"{rule_id}: no basis")
        if status not in STATUSES:
            offenders.append(f"{rule_id}: status {status!r} is not in {list(STATUSES)}")
    assert not offenders, "catalogue rows are incomplete or use an unknown status: " + "; ".join(
        offenders
    )


def test_the_declared_status_is_backed_by_the_rows_own_basis() -> None:
    """``STATUTORY`` needs a law; a sample-policy status needs its policy.

    This is the property that actually broke before: a sample rule presented as a
    legal requirement. A ``PARTIAL`` or ``BLOCKED`` row is a position about SOME
    basis, so it must cite at least one of the two registers.
    """
    offenders: list[str] = []
    for rule_id, (_title, basis, status) in sorted(_rows().items()):
        if status == STATUTORY and not LAW_RE.search(basis):
            offenders.append(f"{rule_id}: STATUTORY with no LAW-* basis ({basis!r})")
        elif status == PRODUCT and not (POL_RE.search(basis) or INVARIANT_RE.search(basis)):
            # A product control is either a stated policy or a named engineering
            # invariant; it is never a bare assertion.
            offenders.append(f"{rule_id}: PRODUCT with no POL-*/EINV-* basis ({basis!r})")
        elif status == POLICY and not POL_RE.search(basis):
            offenders.append(f"{rule_id}: {status} with no POL-* basis ({basis!r})")
        elif status not in (STATUTORY, *SAMPLE_STATUSES) and not (
            LAW_RE.search(basis) or POL_RE.search(basis)
        ):
            offenders.append(f"{rule_id}: {status} with no LAW-*/POL-* basis ({basis!r})")
    assert not offenders, "catalogue status does not match its basis: " + "; ".join(offenders)
