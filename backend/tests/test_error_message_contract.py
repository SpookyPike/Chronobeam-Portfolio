"""Every API refusal states something a person can read (BACKEND_SPEC §11).

The envelope's ``message`` is **human-readable by contract** — the frontend shows
it verbatim to the employee (``frontend/src/lib/notice.tsx::refusalText``), which
is the designed path rather than a leak. What it must never be is an internal
identifier: ``"end_local must be after start_local."`` names an API field, and
``f"{field} cannot be cleared."`` interpolates one. An employee cannot act on
either.

This is a STATIC guard, not an endpoint test: the defect class is how a string is
*built*, and a behavioural test would only cover the paths someone remembered to
call. Two rules:

1. **The message must be a plain string literal.** Not an f-string, not
   ``"…".format(x)``, not ``"%s …" % x``, not a caught exception's ``str(exc)``.
   A refusal is a fixed sentence; anything machine-readable belongs in
   ``details``, where the client renders it in its own words.
2. **The literal must not begin with a field path** — the shape every
   developer-written field identifier takes.

Rule 1 is stated as "must be a literal" rather than "must not interpolate a
field", because the narrower rule is evadable by renaming the carrier: an earlier
version allowed interpolation when the expression was a name from a small set
(``field``, ``name``, ``key``, …), and renaming that variable to ``label`` walked
straight past it. It was verified by injecting exactly that evasion. Banning
non-literals costs nothing — the codebase contains zero legitimate ones — and
cannot be evaded by finding a new way to spell a field name.

Every envelope path is inspected, not just the helper calls: the message argument
of each API error helper (at its own signature's index), a ``message=`` keyword,
and a direct ``HTTPException(detail={"message": …})`` mapping. The last of those
was missed by the first version of this guard, which is how
``worktime/service.py``'s DST message went unnoticed.

Scope is deliberate. Only values that become an API ``message`` are read, so
internal exceptions (``OvertimeAuthorizationDecisionRefused``, ``ValueError``) and
operator diagnostics (``app/backup.py``) are excluded **by construction** rather
than by an allowlist that could rot.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app"

#: Each API-facing refusal helper and the INDEX of its message argument. The
#: signatures differ, so the index cannot be inferred: treating every positional
#: argument as a message made the error CODE look like a message.
API_ERROR_CALLS: dict[str, int] = {
    "api_error": 2,  # (status, code, message, details=None)
    "_error": 1,  # (code, message, status_code=422)
    "_validation_error": 1,  # (code, message)
    "_submission_scope_error": 0,  # (message)
    "_invalid_month": 0,  # (message)
    "_invalid_date": 0,  # (message)
    "_blocked_submission_data": 0,  # (message)
}

#: The envelope key that carries the human-readable sentence.
MESSAGE_KEYWORD = "message"

#: A message is a field-name leak when it STARTS with a snake_case identifier
#: followed by a verb — the shape every developer-written field path takes.
FIELD_NAME_MESSAGE = re.compile(
    r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*(?:\.[a-z][a-z0-9_]*)? "
    r"(?:must|is|are|should|cannot|can not|has|have|was|were|does|do|belongs|requires|needs)\b"
)

#: Whole messages that legitimately begin with a lower-case word that is not a
#: field path.
ALLOWED_MESSAGES = {"no policy edition"}


def discover_helpers(trees: Sequence[ast.AST]) -> dict[str, int]:
    """``API_ERROR_CALLS`` plus every function that CONSTRUCTS a message.

    A wrapper is a hole: ``_oops(msg)`` raising ``api_error(…, msg)`` means the
    call site ``_oops(f"{field} is bad")`` is where the sentence is built, and a
    guard that only knows the built-in helper names would never read it.

    So a function is a sink when one of its PARAMETERS flows into a message
    position, and it is discovered from **two** shapes, not one:

    * the message argument of a known sink call (``api_error(…, msg)``), and
    * the ``"message"`` value of a dict literal (``{"message": msg}``) — which is
      how ``error_body`` builds the envelope, and therefore how ``_callback_error``
      (which forwards into ``error_body``) is found too.

    Resolved to a fixed point, so wrappers of wrappers are covered. The list is
    DERIVED rather than hand-maintained: naming the helpers by hand is what let
    ``error_body`` and ``_callback_error`` go unread, and the next wrapper would
    recreate the hole.

    Takes the trees rather than reading the directory so the mechanism can be
    tested on a synthetic snippet: discovery that silently stopped resolving
    wrappers would keep this guard passing while reading less.
    """
    known = dict(API_ERROR_CALLS)
    for _ in range(10):  # fixed point; the chain is short in practice
        discovered: dict[str, int] = {}
        for tree in trees:
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                params = [a.arg for a in (*fn.args.posonlyargs, *fn.args.args)]
                for node in ast.walk(fn):
                    forwarded: ast.AST | None = None
                    if isinstance(node, ast.Call):
                        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                        index = known.get(name) if isinstance(name, str) else None
                        if index is not None and len(node.args) > index:
                            forwarded = node.args[index]
                    elif isinstance(node, ast.Dict):
                        for key, value in zip(node.keys, node.values, strict=False):
                            if isinstance(key, ast.Constant) and key.value == MESSAGE_KEYWORD:
                                forwarded = value
                    if isinstance(forwarded, ast.Name) and forwarded.id in params:
                        discovered[fn.name] = params.index(forwarded.id)
        if not discovered.keys() - known.keys():
            break
        known.update(discovered)
    return known


SOURCES = {
    str(path.relative_to(APP.parent)): path.read_text() for path in sorted(APP.rglob("*.py"))
}
TREES = [ast.parse(source, filename=name) for name, source in SOURCES.items()]
HELPERS = discover_helpers(TREES)


def _message_nodes(tree: ast.AST, helpers: dict[str, int] = HELPERS) -> list[tuple[int, ast.AST]]:
    """Every AST node that becomes an API-facing ``message``, with its line.

    Three shapes:

    * a positional argument at a known helper's own message index;
    * a ``message=`` keyword on **any** call — not only on a known helper, because
      a result object is built the same way (``OnboardingRejected(…, message=…)``)
      and its ``message`` is later forwarded into the envelope as
      ``result.message``. Reading only the helpers left that construction unread,
      and exempting the forwarding site then made it invisible (review R9);
    * a ``message`` key in any dict literal, which also covers a
      ``**{"message": …}`` splat and a dict built first and passed as ``detail=``.

    ``helpers`` is a parameter so the wrapper-discovery mechanism can be exercised
    against a synthetic snippet, where the wrapper is not in the repository's set.
    """
    found: list[tuple[int, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            index = helpers.get(name) if isinstance(name, str) else None
            if index is not None and len(node.args) > index:
                found.append((node.args[index].lineno, node.args[index]))
            found += [
                (kw.value.lineno, kw.value) for kw in node.keywords if kw.arg == MESSAGE_KEYWORD
            ]
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=False):
                if isinstance(key, ast.Constant) and key.value == MESSAGE_KEYWORD:
                    found.append((value.lineno, value))
    return found


def _bindings_by_node(tree: ast.AST) -> dict[int, dict[str, ast.AST]]:
    """For every node, the non-parameter names bound where that node can see them.

    Needed to tell a **parameter** (genuine forwarding — the sentence is built at
    the call site) from a **binding** (a value built here, which must still be a
    fixed sentence). Conflating the two was what made an earlier version of this
    guard report ~120 false positives, because every helper's ``message: str``
    parameter looked like a non-literal local.

    Module-level bindings count too: ``_PROBE_MESSAGE = "start_local" + " is
    invalid."`` used as a message is a field path, and a version of this guard that
    collected only function locals followed the name to nothing and passed.
    """
    module_level: dict[str, ast.AST] = {}
    for stmt in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for target in targets:
                if isinstance(target, ast.Name) and stmt.value:
                    module_level[target.id] = stmt.value

    by_node: dict[int, dict[str, ast.AST]] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = {a.arg for a in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)}
        bindings: dict[str, ast.AST] = dict(module_level)
        for stmt in ast.walk(fn):
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id not in params and stmt.value:
                        bindings[target.id] = stmt.value
        for child in ast.walk(fn):
            by_node[id(child)] = bindings
    # module-level nodes see the module-level bindings
    for child in ast.walk(tree):
        by_node.setdefault(id(child), module_level)
    return by_node


def _fixed_sentences(node: ast.AST) -> list[str] | None:
    """The complete sentences a FIXED message can be, or ``None`` if it is not one.

    A refusal may be a literal, a conditional between literals, or a
    concatenation of literals — all of which are fixed sentences. Everything else
    is rejected: an f-string (``JoinedStr``), ``.format()`` (a ``Call``),
    ``%``-formatting (a ``BinOp(Mod)``), ``str(exc)`` (a ``Call``), a dict
    subscript, or a name that does not resolve to a fixed value. Only ``Add`` is
    accepted among operators, so ``"prefix " + some_name`` is rejected.

    Concatenation returns the JOINED sentence, not its pieces. That distinction is
    the difference between catching a field name and missing one: the pieces of
    ``"linked_shift_assignment" + "_id is invalid."`` are individually harmless,
    and only the joined sentence begins with the field path. A conditional returns
    every branch, because each is a sentence in its own right.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        body = _fixed_sentences(node.body)
        orelse = _fixed_sentences(node.orelse)
        if body is None or orelse is None:
            return None
        return body + orelse
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _fixed_sentences(node.left)
        right = _fixed_sentences(node.right)
        if left is None or right is None:
            return None
        return [left_part + right_part for left_part in left for right_part in right]
    return None


def _scan_tree(
    tree: ast.AST, rel: str
) -> tuple[list[tuple[str, int, str]], list[tuple[str, int, str]]]:
    """(field-path sentences, messages that are not fixed sentences) for one tree.

    Separate from ``_scan`` so the mechanism can be exercised on a synthetic
    snippet — the repository does not currently contain every shape the guard must
    reject, and a rule that quietly stopped working would keep passing.
    """
    literals: list[tuple[str, int, str]] = []
    non_literals: list[tuple[str, int, str]] = []
    bindings_by_node = _bindings_by_node(tree)
    for line, node in _message_nodes(tree):
        if isinstance(node, ast.Name):
            # a parameter is forwarded (the sentence is built at the call site);
            # a BINDING — local or module-level — is built here and must still be
            # a fixed sentence, so follow it
            bound = bindings_by_node.get(id(node), {}).get(node.id)
            if bound is None:
                continue
            node, line = bound, getattr(bound, "lineno", line)
        # `something.message` hands on a sentence built elsewhere — and this
        # exemption is only sound because the construction IS read: every
        # `message=` keyword on any call, and every `"message"` dict value, is
        # scanned wherever it appears. Remove that and this line becomes the hole
        # it was in an earlier round, where a result object assembled its message
        # from a field and nothing looked at either end.
        if isinstance(node, ast.Attribute) and node.attr == MESSAGE_KEYWORD:
            continue
        sentences = _fixed_sentences(node)
        if sentences is None:
            non_literals.append((rel, line, ast.unparse(node)))
        else:
            literals += [(rel, line, sentence) for sentence in sentences]
    return literals, non_literals


def _scan() -> tuple[list[tuple[str, int, str]], list[tuple[str, int, str]]]:
    """(field-path sentences, messages that are not fixed sentences)."""
    literals: list[tuple[str, int, str]] = []
    non_literals: list[tuple[str, int, str]] = []
    for path in sorted(APP.rglob("*.py")):
        rel = str(path.relative_to(APP.parent))
        tree = ast.parse(path.read_text(), filename=str(path))
        found_literals, found_non_literals = _scan_tree(tree, rel)
        literals += found_literals
        non_literals += found_non_literals
    return literals, non_literals


LITERALS, NON_LITERALS = _scan()
#: ``NON_LITERALS`` is empty in a clean tree, which makes the parametrization below
#: collect zero cases and pytest report it as **1 skipped** in the suite summary.
#: That skip is the SUCCESS state, not a disabled guard: any `api_error(...)` built
#: from an f-string/`.format()`/`str(exc)` reappears here as a failing case. The
#: scan's own non-vacuity is pinned separately by
#: ``test_the_scan_finds_messages_at_all`` (it asserts the scan found >150 message
#: positions), so an empty set cannot be caused by a broken scan. This note exists
#: so the skip is not investigated a second time.


def test_local_wrapper_helpers_are_discovered() -> None:
    """A wrapper is where a leak can hide.

    ``_error`` and ``_invalid_month`` are local helpers, not built-ins, so the
    guard reads their call sites only because discovery found them. This is
    tested on a SYNTHETIC snippet rather than on the repository, because the
    repository's wrappers are all named in ``API_ERROR_CALLS`` already: a test
    that only asserted their presence would keep passing if discovery stopped
    resolving anything at all.

    The snippet is a wrapper of a wrapper, so it also proves the fixed point.
    """
    tree = ast.parse(
        "def inner(msg):\n    raise api_error(422, CODE, msg)\ndef outer(why):\n    inner(why)\n"
    )
    discovered = discover_helpers([tree])
    assert discovered["inner"] == 0
    assert discovered["outer"] == 0, "a wrapper of a wrapper was not resolved"


def test_a_wrapper_call_site_is_scanned() -> None:
    """The point of discovering wrappers: their call sites get read."""
    tree = ast.parse("def inner(msg):\n    raise api_error(422, CODE, msg)\n")
    helpers = discover_helpers([tree])
    assert "inner" in helpers
    call_site = ast.parse('inner(f"{field} is bad")')
    nodes = _message_nodes(call_site, helpers)
    assert len(nodes) == 1, "the wrapper's call site was not read"
    assert _fixed_sentences(nodes[0][1]) is None, "the interpolation was not seen"


def test_the_scan_finds_messages_at_all() -> None:
    """A guard that silently matches nothing would always pass."""
    total = sum(
        len(_message_nodes(ast.parse(f.read_text(), filename=str(f)))) for f in APP.rglob("*.py")
    )
    assert total > 150, f"the scan found only {total} API message positions"
    assert len(LITERALS) > 120, f"the scan found only {len(LITERALS)} API message literals"


@pytest.mark.parametrize(
    ("path", "line", "message"),
    [pytest.param(p, ln, m, id=f"{p}:{ln}") for p, ln, m in LITERALS],
)
def test_no_api_error_message_names_an_api_field(path: str, line: int, message: str) -> None:
    stripped = message.strip()
    if stripped in ALLOWED_MESSAGES:
        return
    assert not FIELD_NAME_MESSAGE.match(stripped), (
        f"{path}:{line} — an API refusal names a field instead of stating the rule: {message!r}"
    )


@pytest.mark.parametrize(
    ("path", "line", "expression"),
    [pytest.param(p, ln, e, id=f"{p}:{ln}") for p, ln, e in NON_LITERALS],
)
def test_every_api_error_message_is_a_fixed_sentence(path: str, line: int, expression: str) -> None:
    """The message must be a plain string literal.

    Live defects this catches: ``f"{field} cannot be cleared."`` and
    ``f"{name} falls in a DST …"`` interpolated an API field the employee has
    never seen; ``str(exc)`` forwarded whatever the raising library chose to say.
    ``.format()`` and ``%`` are the same defect spelled differently, which is why
    the rule is "must be a fixed sentence" and not "must not interpolate a
    field" — the narrower rule is evaded by renaming the carrier.
    """
    raise AssertionError(
        f"{path}:{line} — an API refusal is built from {expression!r} rather than being a "
        "fixed sentence; state the rule in fixed words and put machine-readable values "
        "in `details`"
    )


# ---------------------------------------------------------------------------
# The rules themselves, pinned. A guard whose rules silently weaken is worse
# than no guard, so each shape it must reject is asserted directly rather than
# only through the codebase it currently happens to scan.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        'f"{field} cannot be cleared."',  # interpolation
        'f"{label} cannot be cleared."',  # …under any carrier name
        '"{} cannot be cleared.".format(field)',  # .format
        '"%s cannot be cleared." % field',  # %-formatting
        "str(exc)",  # a caught exception's own text
        '"prefix " + name + " suffix"',  # concatenated with a name
        "REQUIRED_COPY[field]",  # a dict subscript
        "build_message(field)",  # a helper call
    ],
)
def test_the_guard_rejects_each_evasion_shape(source: str) -> None:
    node = ast.parse(source, mode="eval").body
    assert _fixed_sentences(node) is None, f"the guard would ACCEPT {source!r}"


@pytest.mark.parametrize(
    "source",
    [
        '"A fixed sentence."',
        '"A fixed sentence." " Continued."',  # implicit concatenation (folded)
        '"one " + "two"',  # explicit concatenation of literals
        '("twice" if ambiguous else "never")',  # a conditional between literals
    ],
)
def test_the_guard_accepts_a_fixed_sentence(source: str) -> None:
    node = ast.parse(source, mode="eval").body
    assert _fixed_sentences(node) is not None, f"the guard would REJECT {source!r}"


def test_a_concatenation_is_checked_as_one_sentence() -> None:
    """The pieces of a concatenation are individually harmless.

    ``"linked_shift_assignment" + "_id is invalid."`` begins with a field path
    only once JOINED — the first piece has no verb and the second starts with an
    underscore, so a guard that tested the pieces separately would pass while the
    response named an API field (review R9).
    """
    node = ast.parse('"linked_shift_assignment" + "_id is invalid."', mode="eval").body
    assert _fixed_sentences(node) == ["linked_shift_assignment_id is invalid."]


def test_a_module_level_binding_is_followed() -> None:
    """A sentence bound at module level is still built here.

    ``_PROBE_MESSAGE = "start_local" + " is invalid."`` used as a message is a
    field path, and a guard that collected only FUNCTION locals followed the name
    to nothing and passed. Verified by injecting exactly that (review R10).
    """
    tree = ast.parse(
        '_PROBE = "start_local" + " is invalid."\n'
        "def f():\n"
        "    raise _validation_error(CODE, _PROBE)\n"
    )
    found = _scan_tree(tree, "synthetic.py")
    assert any(FIELD_NAME_MESSAGE.match(sentence.strip()) for _, _, sentence in found[0]), (
        f"the module-level binding was not followed: {found[0]}"
    )
