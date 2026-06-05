"""Tests for cyclomatic / cognitive / loc complexity on TypeScript functions.

Each fixture includes a hand-derivation comment for every non-trivial number
so the test is auditable without reading the implementation.

Confirmed tree-sitter node-type names used in _compute_complexity:
  if_statement       — if (else-if is a nested if_statement inside else_clause)
  else_clause        — else / else-if container
  for_statement      — traditional for(;;) loop
  for_in_statement   — both for-of AND for-in (same node type in tree-sitter TS)
  while_statement    — while
  do_statement       — do-while
  catch_clause       — catch
  ternary_expression — ternary (?:)
  binary_expression  — logical && / || (operator is a direct '&&'/'||' child)
  switch_case        — non-default case
  switch_default     — default (does NOT count for cyclomatic; 0 cognitive)
"""

from __future__ import annotations

from agent.graph_analyzer.parsers.typescript import TypeScriptParser


def _parse(text: str, *, rel_path: str = "src/mod.ts", area: str = "src"):
    return TypeScriptParser().parse_file(
        rel_path=rel_path,
        area=area,
        source=text.encode(),
    )


def _fn(result, label: str):
    """Return the function-kind node with the given label, or raise."""
    matches = [n for n in result.nodes if n.kind == "function" and n.label == label]
    assert len(matches) == 1, (
        f"Expected exactly one node with label={label!r}, got {[n.label for n in result.nodes if n.kind == 'function']}"
    )
    return matches[0]


class TestTrivialFunction:
    """A no-branch function: cyclomatic=1, cognitive=0."""

    SRC = """\
function trivial(x: number): number {
  return x + 1;
}
"""
    # loc: 3 lines (line 1 to line 3)
    # cyclomatic: 1 (base, no branches)
    # cognitive: 0 (no structural complexity)

    def test_cyclomatic(self) -> None:
        node = _fn(_parse(self.SRC), "trivial")
        assert node.cyclomatic == 1

    def test_cognitive(self) -> None:
        node = _fn(_parse(self.SRC), "trivial")
        assert node.cognitive == 0

    def test_loc(self) -> None:
        node = _fn(_parse(self.SRC), "trivial")
        assert node.loc == 3  # lines 1-3


class TestFixtureA:
    """Fixture A — nested ifs.

    function f(a: boolean, b: boolean): number {
      if (a) {
        if (b) {
          return 1;
        }
      }
      return 0;
    }

    cyclomatic = 3:  1 (base) + 1 (if a) + 1 (if b)
    cognitive  = 3:  if(a) contributes 1+0=1 @ nesting=0, then nesting becomes 1;
                     if(b) contributes 1+1=2 @ nesting=1. Total = 1+2 = 3.
    loc        = 8:  lines 1-8
    """

    SRC = """\
function f(a: boolean, b: boolean): number {
  if (a) {
    if (b) {
      return 1;
    }
  }
  return 0;
}
"""

    def test_cyclomatic(self) -> None:
        node = _fn(_parse(self.SRC), "f")
        assert node.cyclomatic == 3  # 1 + if + if

    def test_cognitive(self) -> None:
        node = _fn(_parse(self.SRC), "f")
        assert node.cognitive == 3  # if@0 -> 1; if@1 → 2; total = 3

    def test_loc(self) -> None:
        node = _fn(_parse(self.SRC), "f")
        assert node.loc == 8  # 8 lines


class TestFixtureB:
    """Fixture B — for-of + &&.

    function g(items: number[]): number {
      let total = 0;
      for (const x of items) {
        if (x > 0 && x < 10) {
          total += x;
        }
      }
      return total;
    }

    cyclomatic = 4:  1 (base) + 1 (for-of) + 1 (if) + 1 (&&)
    cognitive  = 4:  for-of @ nesting=0 -> 1+0=1, nesting→1;
                     if @ nesting=1 -> 1+1=2, nesting→2;
                     && -> flat 1.
                     Total = 1+2+1 = 4.
    loc        = 9:  lines 1-9
    """

    SRC = """\
function g(items: number[]): number {
  let total = 0;
  for (const x of items) {
    if (x > 0 && x < 10) {
      total += x;
    }
  }
  return total;
}
"""

    def test_cyclomatic(self) -> None:
        node = _fn(_parse(self.SRC), "g")
        assert node.cyclomatic == 4  # 1 + for-of + if + &&

    def test_cognitive(self) -> None:
        node = _fn(_parse(self.SRC), "g")
        assert node.cognitive == 4  # for@0->1 + if@1→2 + &&→1 = 4

    def test_loc(self) -> None:
        node = _fn(_parse(self.SRC), "g")
        assert node.loc == 9  # 9 lines


class TestFixtureC:
    """Fixture C — if / else if / else.

    function h(n: number): string {
      if (n > 0) {
        return "pos";
      } else if (n < 0) {
        return "neg";
      } else {
        return "zero";
      }
    }

    cyclomatic = 3:  1 (base) + 1 (if n>0) + 1 (else-if n<0);
                     plain else does NOT add; switch_default excluded.
    cognitive  = 3:  if @ nesting=0 -> +1, nesting→1;
                     else-if -> flat +1 (no nesting bonus, no increment);
                     plain else -> flat +1.
                     Total = 1+1+1 = 3.
    loc        = 9:  lines 1-9
    """

    SRC = """\
function h(n: number): string {
  if (n > 0) {
    return "pos";
  } else if (n < 0) {
    return "neg";
  } else {
    return "zero";
  }
}
"""

    def test_cyclomatic(self) -> None:
        node = _fn(_parse(self.SRC), "h")
        assert node.cyclomatic == 3  # 1 + if + else-if; plain else excluded

    def test_cognitive(self) -> None:
        node = _fn(_parse(self.SRC), "h")
        assert node.cognitive == 3  # if->1 + else-if flat→1 + else flat→1

    def test_loc(self) -> None:
        node = _fn(_parse(self.SRC), "h")
        assert node.loc == 9  # 9 lines


class TestNestedFunctionIsolation:
    """An outer function containing an inner function/arrow with its own if.

    The OUTER node's scores must EXCLUDE the inner function's branches.
    The INNER function must get its own independent scores.

    function outer(a: boolean): number {
      if (a) { return 1; }
      function inner(b: boolean): number {
        if (b) { return 2; }
        return 0;
      }
      return 0;
    }

    outer:
      cyclomatic = 2:  1 (base) + 1 (if a)  — inner's if does NOT count
      cognitive  = 1:  if(a) @ nesting=0 -> 1
      loc        = 8:  lines 1-8

    inner:
      cyclomatic = 2:  1 (base) + 1 (if b)
      cognitive  = 1:  if(b) @ nesting=0 -> 1
      loc        = 4:  lines 3-6 (function inner ... return 0; })
    """

    SRC = """\
function outer(a: boolean): number {
  if (a) { return 1; }
  function inner(b: boolean): number {
    if (b) { return 2; }
    return 0;
  }
  return 0;
}
"""

    def test_outer_cyclomatic_excludes_inner(self) -> None:
        node = _fn(_parse(self.SRC), "outer")
        # Only outer's own if(a) counts; inner's if(b) is NOT included
        assert node.cyclomatic == 2

    def test_outer_cognitive_excludes_inner(self) -> None:
        node = _fn(_parse(self.SRC), "outer")
        assert node.cognitive == 1

    def test_outer_loc(self) -> None:
        node = _fn(_parse(self.SRC), "outer")
        assert node.loc == 8  # lines 1-8

    def test_inner_has_own_scores(self) -> None:
        # Nested function_declaration nodes get a qualified label (outer.inner)
        # matching the Python parser's convention.
        node = _fn(_parse(self.SRC), "outer.inner")
        # inner has its own if(b): cyclomatic=2, cognitive=1
        assert node.cyclomatic == 2
        assert node.cognitive == 1

    def test_inner_loc(self) -> None:
        node = _fn(_parse(self.SRC), "outer.inner")
        assert node.loc == 4  # lines 3-6


class TestMiscBranches:
    """Miscellaneous branch-type coverage: do-while, ||, ternary, switch.

    Each sub-case is a separate method with its own source fixture and a
    hand-derivation comment for every expected number.
    """

    # ------------------------------------------------------------------
    # do-while containing a single if
    # ------------------------------------------------------------------
    #
    # function doWhileTest(n: number): number {
    #   let i = 0;
    #   do {
    #     if (i > 0) { i--; }
    #   } while (n > 0);
    #   return i;
    # }
    #
    # cyclomatic = 3:
    #   1 (base)
    #   + 1 (do_statement)
    #   + 1 (if_statement inside loop body)
    #
    # cognitive = 3:
    #   do_statement @ nesting=0 → +(1+0)=1, nesting→1
    #   if_statement @ nesting=1 → +(1+1)=2, nesting→2
    #   Total = 1 + 2 = 3
    _DO_WHILE_SRC = """\
function doWhileTest(n: number): number {
  let i = 0;
  do {
    if (i > 0) { i--; }
  } while (n > 0);
  return i;
}
"""

    def test_do_while_cyclomatic(self) -> None:
        node = _fn(_parse(self._DO_WHILE_SRC), "doWhileTest")
        assert node.cyclomatic == 3  # 1 + do_statement + if

    def test_do_while_cognitive(self) -> None:
        node = _fn(_parse(self._DO_WHILE_SRC), "doWhileTest")
        assert node.cognitive == 3  # do@0→1 + if@1→2 = 3

    # ------------------------------------------------------------------
    # || operator — must count identically to &&
    # ------------------------------------------------------------------
    #
    # function orTest(a: boolean, b: boolean): number {
    #   if (a || b) { return 1; }
    #   return 0;
    # }
    #
    # cyclomatic = 3:
    #   1 (base)
    #   + 1 (if_statement)
    #   + 1 (|| operator in binary_expression)
    #
    # cognitive = 2:
    #   if_statement @ nesting=0 → +(1+0)=1, nesting→1
    #   || → flat +1
    #   Total = 1 + 1 = 2
    _OR_SRC = """\
function orTest(a: boolean, b: boolean): number {
  if (a || b) { return 1; }
  return 0;
}
"""

    def test_or_operator_cyclomatic(self) -> None:
        node = _fn(_parse(self._OR_SRC), "orTest")
        assert node.cyclomatic == 3  # 1 + if + ||

    def test_or_operator_cognitive(self) -> None:
        node = _fn(_parse(self._OR_SRC), "orTest")
        assert node.cognitive == 2  # if@0→1 + ||→1 = 2

    # ------------------------------------------------------------------
    # ternary expression (?:)
    # ------------------------------------------------------------------
    #
    # function ternaryTest(c: boolean): number {
    #   const y = c ? 1 : 2;
    #   return y;
    # }
    #
    # cyclomatic = 2:
    #   1 (base)
    #   + 1 (ternary_expression)
    #
    # cognitive = 1:
    #   ternary_expression @ nesting=0 → +(1+0)=1
    #   Total = 1
    _TERNARY_SRC = """\
function ternaryTest(c: boolean): number {
  const y = c ? 1 : 2;
  return y;
}
"""

    def test_ternary_cyclomatic(self) -> None:
        node = _fn(_parse(self._TERNARY_SRC), "ternaryTest")
        assert node.cyclomatic == 2  # 1 + ternary

    def test_ternary_cognitive(self) -> None:
        node = _fn(_parse(self._TERNARY_SRC), "ternaryTest")
        assert node.cognitive == 1  # ternary@0→1

    # ------------------------------------------------------------------
    # switch with 2 non-default cases + 1 default
    # ------------------------------------------------------------------
    #
    # function switchTest(n: number): string {
    #   switch (n) {
    #     case 1: return "one";
    #     case 2: return "two";
    #     default: return "other";
    #   }
    # }
    #
    # cyclomatic = 3:
    #   1 (base)
    #   + 1 (switch_case for "case 1")
    #   + 1 (switch_case for "case 2")
    #   default (switch_default) is NOT counted
    #
    # cognitive = 0:
    #   switch_statement is not in _TS_COGNITIVE_NESTING_TYPES;
    #   switch_case / switch_default are also absent from that set.
    #   Documented deferral — mirrors Python parser's match/case treatment.
    #   Total = 0
    _SWITCH_SRC = """\
function switchTest(n: number): string {
  switch (n) {
    case 1: return "one";
    case 2: return "two";
    default: return "other";
  }
}
"""

    def test_switch_cyclomatic(self) -> None:
        node = _fn(_parse(self._SWITCH_SRC), "switchTest")
        assert node.cyclomatic == 3  # 1 + case1 + case2 (default excluded)

    def test_switch_cognitive(self) -> None:
        node = _fn(_parse(self._SWITCH_SRC), "switchTest")
        assert node.cognitive == 0  # switch contributes 0 to cognitive (documented deferral)
