"""Tests for per-function complexity metrics (cyclomatic / cognitive / loc).

These tests assert the exact numbers promised by the Phase 8 contract.
The parser is invoked exactly as the existing parser tests do it.
"""

from __future__ import annotations

from agent.graph_analyzer.parsers.python import PythonParser


def _parse(text: str, *, rel_path: str = "pkg/mod.py", area: str = "pkg"):
    return PythonParser().parse_file(
        rel_path=rel_path,
        area=area,
        source=text.encode(),
    )


def _func(result, label: str):
    """Return the function node with the given label, or raise."""
    matches = [n for n in result.nodes if n.kind == "function" and n.label == label]
    assert len(matches) == 1, f"Expected 1 node with label {label!r}, got {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# Fixture A — nested ifs
# ---------------------------------------------------------------------------

FIXTURE_A = """\
def f(a, b):
    if a:
        if b:
            return 1
    return 0
"""


class TestFixtureA:
    def test_cyclomatic(self) -> None:
        result = _parse(FIXTURE_A)
        assert _func(result, "f").cyclomatic == 3

    def test_cognitive(self) -> None:
        result = _parse(FIXTURE_A)
        assert _func(result, "f").cognitive == 3

    def test_loc(self) -> None:
        result = _parse(FIXTURE_A)
        assert _func(result, "f").loc == 5


# ---------------------------------------------------------------------------
# Fixture B — for + if + and
# ---------------------------------------------------------------------------

FIXTURE_B = """\
def g(items):
    total = 0
    for x in items:
        if x > 0 and x < 10:
            total += x
    return total
"""


class TestFixtureB:
    def test_cyclomatic(self) -> None:
        result = _parse(FIXTURE_B)
        assert _func(result, "g").cyclomatic == 4

    def test_cognitive(self) -> None:
        result = _parse(FIXTURE_B)
        assert _func(result, "g").cognitive == 4

    def test_loc(self) -> None:
        result = _parse(FIXTURE_B)
        assert _func(result, "g").loc == 6


# ---------------------------------------------------------------------------
# Fixture C — if / elif / else
# ---------------------------------------------------------------------------

FIXTURE_C = """\
def h(n):
    if n > 0:
        return "pos"
    elif n < 0:
        return "neg"
    else:
        return "zero"
"""


class TestFixtureC:
    def test_cyclomatic(self) -> None:
        result = _parse(FIXTURE_C)
        assert _func(result, "h").cyclomatic == 3

    def test_cognitive(self) -> None:
        result = _parse(FIXTURE_C)
        assert _func(result, "h").cognitive == 3

    def test_loc(self) -> None:
        result = _parse(FIXTURE_C)
        assert _func(result, "h").loc == 7


# ---------------------------------------------------------------------------
# Trivial — no branches
# ---------------------------------------------------------------------------

FIXTURE_TRIVIAL = """\
def trivial(x):
    return x + 1
"""


class TestTrivial:
    def test_cyclomatic(self) -> None:
        result = _parse(FIXTURE_TRIVIAL)
        assert _func(result, "trivial").cyclomatic == 1

    def test_cognitive(self) -> None:
        result = _parse(FIXTURE_TRIVIAL)
        assert _func(result, "trivial").cognitive == 0

    def test_loc(self) -> None:
        result = _parse(FIXTURE_TRIVIAL)
        assert _func(result, "trivial").loc == 2


# ---------------------------------------------------------------------------
# Nested function — outer does NOT count inner's branches
# ---------------------------------------------------------------------------

FIXTURE_NESTED = """\
def outer(x):
    def inner(y):
        if y > 0:
            return y
        return 0
    return inner(x)
"""


class TestNestedFunction:
    def test_outer_cyclomatic_excludes_inner(self) -> None:
        """outer has no branches of its own; inner's if must not leak up."""
        result = _parse(FIXTURE_NESTED)
        assert _func(result, "outer").cyclomatic == 1

    def test_outer_cognitive_excludes_inner(self) -> None:
        result = _parse(FIXTURE_NESTED)
        assert _func(result, "outer").cognitive == 0

    def test_inner_cyclomatic(self) -> None:
        """inner itself gets its own score."""
        result = _parse(FIXTURE_NESTED)
        assert _func(result, "outer.inner").cyclomatic == 2

    def test_inner_cognitive(self) -> None:
        result = _parse(FIXTURE_NESTED)
        assert _func(result, "outer.inner").cognitive == 1


# ---------------------------------------------------------------------------
# Misc branches — while, except, list-comprehension if/for
# ---------------------------------------------------------------------------

FIXTURE_MISC = """\
def m(items):
    out = []
    i = 0
    while i < len(items):
        try:
            out = [x for x in items if x > 0]
        except ValueError:
            pass
        i += 1
    return out
"""

# Hand-derivation (cyclomatic = 1 + decision-point count):
#   while_statement            +1  → 2
#   except_clause              +1  → 3
#   for_in_clause (comprehension)  +1  → 4
#   if_clause     (comprehension)  +1  → 5
#   cyclomatic = 5
#
# Hand-derivation (cognitive = sum of per-node contributions):
#   while_statement  @ nesting=0 → 1 + 0 = 1  (nesting becomes 1 inside while)
#   try_statement    not in _COGNITIVE_NESTING_TYPES → 0  (nesting stays 1)
#   except_clause    @ nesting=1 → 1 + 1 = 2  (nesting becomes 2 inside except)
#   for_in_clause / if_clause inside comprehension: NOT in _COGNITIVE_NESTING_TYPES → 0
#   cognitive = 1 + 2 = 3


class TestMiscBranches:
    def test_cyclomatic(self) -> None:
        result = _parse(FIXTURE_MISC)
        assert _func(result, "m").cyclomatic == 5

    def test_cognitive(self) -> None:
        result = _parse(FIXTURE_MISC)
        assert _func(result, "m").cognitive == 3

    def test_loc(self) -> None:
        result = _parse(FIXTURE_MISC)
        assert _func(result, "m").loc == 10
