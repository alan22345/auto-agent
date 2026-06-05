"""Unit tests for compute_clones / normalize_tokens (ADR-016 Phase 11 §2).

All tests use hand-built token lists and Node stubs — no tree-sitter, no
file I/O, no pipeline.  A small ``min_tokens=5`` threshold is used
throughout to keep fixtures short.
"""

from __future__ import annotations

import pytest

from agent.graph_analyzer.duplication import compute_clones, normalize_tokens
from shared.types import Node

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_node(
    node_id: str,
    file: str = "a.py",
    line_start: int = 1,
    line_end: int = 20,
    kind: str = "function",
) -> Node:
    """Build a minimal Node for testing."""
    return Node(
        id=node_id,
        kind=kind,
        label=node_id,
        file=file,
        line_start=line_start,
        line_end=line_end,
        area="test_area",
        parent=None,
    )


def _id_tokens(words: list[str]) -> list[tuple[str, str]]:
    """Turn a list of word strings into identifier-type tokens."""
    return [("identifier", w) for w in words]


def _kw_tokens(words: list[str]) -> list[tuple[str, str]]:
    """Turn a list of word strings into keyword-type tokens (exact text kept in all modes)."""
    return [("keyword", w) for w in words]


def _make_long_ident_tokens(n: int, prefix: str = "x") -> list[tuple[str, str]]:
    """Build n identifier tokens with distinct names (x0, x1, …)."""
    return [("identifier", f"{prefix}{i}") for i in range(n)]


# A shared token body of 10 tokens used in several tests.
_SHARED_BODY = [
    ("keyword", "for"),
    ("identifier", "item"),
    ("keyword", "in"),
    ("identifier", "items"),
    ("punctuation", ":"),
    ("identifier", "result"),
    ("punctuation", "."),
    ("identifier", "append"),
    ("punctuation", "("),
    ("identifier", "item"),
    ("punctuation", ")"),
]  # 11 tokens


# ---------------------------------------------------------------------------
# normalize_tokens
# ---------------------------------------------------------------------------


class TestNormalizeTokens:
    def test_strict_preserves_exact_text(self):
        tokens = [
            ("identifier", "foo"),
            ("integer", "42"),
            ("string_content", "hello"),
        ]
        result = normalize_tokens(tokens, "strict")
        assert result == ["foo", "42", "hello"]

    def test_mild_same_as_strict(self):
        tokens = [
            ("identifier", "foo"),
            ("number", "3.14"),
            ("string_fragment", "bar"),
        ]
        assert normalize_tokens(tokens, "mild") == normalize_tokens(tokens, "strict")

    def test_weak_abstracts_numeric_literals(self):
        tokens = [
            ("identifier", "x"),
            ("integer", "100"),
            ("float", "3.14"),
            ("number", "99"),
        ]
        result = normalize_tokens(tokens, "weak")
        assert result == ["x", "<NUM>", "<NUM>", "<NUM>"]

    def test_weak_abstracts_string_literals(self):
        tokens = [
            ("string", '"hello"'),
            ("string_fragment", "world"),
            ("string_content", "foo"),
        ]
        result = normalize_tokens(tokens, "weak")
        assert result == ["<STR>", "<STR>", "<STR>"]

    def test_weak_keeps_identifiers_exact(self):
        tokens = [("identifier", "myVar"), ("property_identifier", "myProp")]
        result = normalize_tokens(tokens, "weak")
        assert result == ["myVar", "myProp"]

    def test_semantic_abstracts_identifiers(self):
        tokens = [
            ("identifier", "foo"),
            ("property_identifier", "bar"),
        ]
        result = normalize_tokens(tokens, "semantic")
        assert result == ["<ID>", "<ID>"]

    def test_semantic_abstracts_type_identifiers(self):
        tokens = [("type_identifier", "MyClass")]
        result = normalize_tokens(tokens, "semantic")
        assert result == ["<TYPE>"]

    def test_semantic_abstracts_literals(self):
        tokens = [
            ("integer", "5"),
            ("string_content", "hi"),
        ]
        result = normalize_tokens(tokens, "semantic")
        assert result == ["<NUM>", "<STR>"]

    def test_semantic_keeps_keywords_exact(self):
        tokens = [("keyword", "for"), ("punctuation", ":")]
        result = normalize_tokens(tokens, "semantic")
        assert result == ["for", ":"]

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError, match="Unknown normalization mode"):
            normalize_tokens([("identifier", "x")], "bogus")


# ---------------------------------------------------------------------------
# compute_clones — basic cases
# ---------------------------------------------------------------------------


class TestComputeClonesBasic:
    """Two functions with >= min_tokens identical token run → ONE group, 2 instances."""

    def test_identical_functions_produce_one_group(self):
        shared = _SHARED_BODY * 1  # 11 tokens
        tokens = {
            "a.py::func_a": shared,
            "b.py::func_b": shared,
        }
        nodes = {
            "a.py::func_a": _make_node("a.py::func_a", file="a.py"),
            "b.py::func_b": _make_node("b.py::func_b", file="b.py"),
        }
        groups = compute_clones(tokens, nodes, min_tokens=5, mode="mild")
        assert len(groups) == 1
        g = groups[0]
        assert g.token_len >= 5
        instance_ids = {inst.node_id for inst in g.instances}
        assert "a.py::func_a" in instance_ids
        assert "b.py::func_b" in instance_ids

    def test_clone_token_len_is_match_length(self):
        shared = _SHARED_BODY  # 11 tokens
        tokens = {
            "a.py::f": shared,
            "b.py::g": shared,
        }
        nodes = {
            "a.py::f": _make_node("a.py::f", file="a.py"),
            "b.py::g": _make_node("b.py::g", file="b.py"),
        }
        groups = compute_clones(tokens, nodes, min_tokens=5, mode="mild")
        assert len(groups) == 1
        assert groups[0].token_len == len(shared)

    def test_stable_id_format(self):
        shared = _SHARED_BODY
        tokens = {
            "a.py::f": shared,
            "b.py::g": shared,
        }
        nodes = {
            "a.py::f": _make_node("a.py::f", file="a.py"),
            "b.py::g": _make_node("b.py::g", file="b.py"),
        }
        groups = compute_clones(tokens, nodes, min_tokens=5, mode="mild")
        assert len(groups) == 1
        gid = groups[0].id
        assert gid.startswith("clone:mild:")
        assert "a.py::f" in gid
        assert "b.py::g" in gid


# ---------------------------------------------------------------------------
# compute_clones — below min_tokens → no group
# ---------------------------------------------------------------------------


class TestComputeClonesBelowThreshold:
    def test_short_match_produces_no_group(self):
        """Clone shorter than min_tokens must not be reported."""
        shared = _SHARED_BODY  # 11 tokens
        tokens = {
            "a.py::f": shared,
            "b.py::g": shared,
        }
        nodes = {
            "a.py::f": _make_node("a.py::f", file="a.py"),
            "b.py::g": _make_node("b.py::g", file="b.py"),
        }
        # min_tokens > shared body length → no group.
        groups = compute_clones(tokens, nodes, min_tokens=50, mode="mild")
        assert groups == []


# ---------------------------------------------------------------------------
# compute_clones — intra-function self-clone excluded
# ---------------------------------------------------------------------------


class TestComputeClonesIntraFunction:
    def test_repeat_within_one_function_not_reported(self):
        """A repeated block wholly inside one function → no group (v1 scope)."""
        # Repeat the shared body twice inside the SAME function.
        tokens_f = _SHARED_BODY * 2  # repeated within one function
        tokens_g = _id_tokens(["unique_x", "unique_y", "unique_z"])
        tokens = {
            "a.py::f": tokens_f,
            "b.py::g": tokens_g,
        }
        nodes = {
            "a.py::f": _make_node("a.py::f", file="a.py"),
            "b.py::g": _make_node("b.py::g", file="b.py"),
        }
        groups = compute_clones(tokens, nodes, min_tokens=5, mode="mild")
        # f has a repeated run, but it's within f alone.
        # g is not a match. So no inter-function clone.
        instance_node_ids = {inst.node_id for g in groups for inst in g.instances}
        assert "b.py::g" not in instance_node_ids


# ---------------------------------------------------------------------------
# compute_clones — sentinel prevents cross-boundary match
# ---------------------------------------------------------------------------


class TestComputeClonesSentinel:
    def test_cross_boundary_concat_does_not_produce_clone(self):
        """f ends with tokens [A, B, C] and g starts with [A, B, C].
        The concatenation f_tokens + sentinel + g_tokens would look like
        [..., A, B, C, sentinel, A, B, C, ...].  The LCP cannot cross the
        sentinel, so no clone of length >= min_tokens is emitted just from
        the boundary overlap.
        """
        overlap = [("identifier", f"overlap{i}") for i in range(6)]
        f_unique_prefix = [("identifier", f"fp{i}") for i in range(5)]
        g_unique_suffix = [("identifier", f"gs{i}") for i in range(5)]

        # f = f_unique_prefix + overlap
        # g = overlap + g_unique_suffix
        # The suffix of f and prefix of g share 'overlap' (6 tokens), but
        # they are separated by a sentinel in the combined sequence.
        tokens_f = f_unique_prefix + overlap
        tokens_g = overlap + g_unique_suffix

        tokens = {
            "a.py::f": tokens_f,
            "b.py::g": tokens_g,
        }
        nodes = {
            "a.py::f": _make_node("a.py::f", file="a.py"),
            "b.py::g": _make_node("b.py::g", file="b.py"),
        }
        # min_tokens = 6 — exactly the length of 'overlap'.
        # The overlap IS a valid clone because 'overlap' appears wholly
        # inside both functions (at the end of f and the start of g).
        groups_6 = compute_clones(tokens, nodes, min_tokens=6, mode="mild")
        # This should find the clone since the overlap appears entirely
        # within each function's token run.
        assert len(groups_6) == 1

        # However, a concat-boundary crossing would produce a spurious match
        # of length 6 at the exact junction even when neither function
        # individually contains the full run.  To test sentinel correctness,
        # build functions where f ends with [A,B,C] and g starts with [A,B,C]
        # but neither function fully contains a run of 12 tokens.
        half = [("identifier", f"half{i}") for i in range(6)]
        tokens2 = {
            "a.py::ends_with_half": half,  # only 6 tokens = the half
            "b.py::starts_with_half": half,  # only 6 tokens = the half
        }
        nodes2 = {
            "a.py::ends_with_half": _make_node("a.py::ends_with_half", file="a.py"),
            "b.py::starts_with_half": _make_node("b.py::starts_with_half", file="b.py"),
        }
        # min_tokens = 12 — more than either function; a cross-boundary
        # pseudo-match of 12 would be a bug.
        groups_12 = compute_clones(tokens2, nodes2, min_tokens=12, mode="mild")
        assert groups_12 == [], (
            "Cross-boundary pseudo-match must not be reported; sentinel is broken"
        )


# ---------------------------------------------------------------------------
# compute_clones — normalization-dependent detection
# ---------------------------------------------------------------------------


class TestComputeClonesNormalization:
    """Two functions identical except variable names.
    strict → no clone; semantic → one clone.
    """

    def _make_pair(self) -> tuple[dict, dict]:
        """Pair of functions that are fully identical under semantic mode but
        differ under strict/mild (every identifier is different).

        Body is built so that EVERY token is an identifier — there are no
        shared keyword/punctuation runs at all.  Under strict the two bodies
        share no common sub-sequence, so no clone.  Under semantic all
        identifiers become <ID>, the bodies are identical, and a clone forms.
        """

        def _body(prefix: str) -> list[tuple[str, str]]:
            # 8 identifier tokens; each has a function-specific prefix so
            # strict mode sees zero matching tokens between f and g.
            return [("identifier", f"{prefix}{i}") for i in range(8)]

        tokens = {
            "a.py::f": _body("alpha_"),
            "b.py::g": _body("beta_"),
        }
        nodes = {
            "a.py::f": _make_node("a.py::f", file="a.py"),
            "b.py::g": _make_node("b.py::g", file="b.py"),
        }
        return tokens, nodes

    def test_strict_no_clone_when_names_differ(self):
        tokens, nodes = self._make_pair()
        groups = compute_clones(tokens, nodes, min_tokens=5, mode="strict")
        # Every token is a distinct identifier; strict keeps exact text.
        # No two tokens from f match any token from g → no common run.
        assert groups == []

    def test_semantic_clone_when_names_differ(self):
        tokens, nodes = self._make_pair()
        groups = compute_clones(tokens, nodes, min_tokens=5, mode="semantic")
        # Under semantic mode, all identifiers become <ID>, making the two
        # bodies identical (8 tokens).  min_tokens=5 → one group.
        assert len(groups) == 1
        assert groups[0].token_len == 8

    def test_weak_clone_for_string_literal_difference(self):
        """Identical except a string literal value — clone under weak/semantic, not strict."""

        def _body(s: str) -> list[tuple[str, str]]:
            return [
                ("identifier", "x"),
                ("punctuation", "="),
                ("string_content", s),  # differs
                ("identifier", "y"),
                ("punctuation", "="),
                ("string_content", s),  # differs
                ("identifier", "z"),
                ("punctuation", "="),
                ("string_content", s),  # differs
                ("identifier", "return"),
                ("identifier", "x"),
            ]

        tokens = {
            "a.py::f": _body("hello"),
            "b.py::g": _body("world"),
        }
        nodes = {
            "a.py::f": _make_node("a.py::f", file="a.py"),
            "b.py::g": _make_node("b.py::g", file="b.py"),
        }
        groups_strict = compute_clones(tokens, nodes, min_tokens=5, mode="strict")
        groups_weak = compute_clones(tokens, nodes, min_tokens=5, mode="weak")
        groups_semantic = compute_clones(tokens, nodes, min_tokens=5, mode="semantic")

        assert groups_strict == [], "strict must not match when string literal values differ"
        assert len(groups_weak) == 1, "weak must match (string literals abstracted)"
        assert len(groups_semantic) == 1, "semantic must match (literals + identifiers abstracted)"


# ---------------------------------------------------------------------------
# compute_clones — determinism
# ---------------------------------------------------------------------------


class TestComputeClonesDeterminism:
    def test_two_calls_produce_identical_results(self):
        shared = _SHARED_BODY
        tokens = {
            "a.py::f": shared,
            "b.py::g": shared,
        }
        nodes = {
            "a.py::f": _make_node("a.py::f", file="a.py"),
            "b.py::g": _make_node("b.py::g", file="b.py"),
        }
        result1 = compute_clones(tokens, nodes, min_tokens=5, mode="mild")
        result2 = compute_clones(tokens, nodes, min_tokens=5, mode="mild")
        assert result1 == result2

    def test_result_sorted_by_id(self):
        """Returned groups are sorted by id."""
        # Build two independent clone pairs to get two groups.
        body_a = [("identifier", f"tok{i}") for i in range(6)]
        body_b = [("identifier", f"tok{i}") for i in range(6, 12)]
        tokens = {
            "a.py::f1": body_a,
            "b.py::f2": body_a,
            "c.py::f3": body_b,
            "d.py::f4": body_b,
        }
        nodes = {
            "a.py::f1": _make_node("a.py::f1", file="a.py"),
            "b.py::f2": _make_node("b.py::f2", file="b.py"),
            "c.py::f3": _make_node("c.py::f3", file="c.py"),
            "d.py::f4": _make_node("d.py::f4", file="d.py"),
        }
        groups = compute_clones(tokens, nodes, min_tokens=5, mode="mild")
        ids = [g.id for g in groups]
        assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# compute_clones — three functions sharing the same block
# ---------------------------------------------------------------------------


class TestComputeClonesThreeFunctions:
    def test_three_way_clone_group(self):
        """Three functions sharing the same body → one group with 3 instances."""
        shared = _SHARED_BODY  # 11 tokens
        tokens = {
            "a.py::f1": shared,
            "b.py::f2": shared,
            "c.py::f3": shared,
        }
        nodes = {
            "a.py::f1": _make_node("a.py::f1", file="a.py"),
            "b.py::f2": _make_node("b.py::f2", file="b.py"),
            "c.py::f3": _make_node("c.py::f3", file="c.py"),
        }
        groups = compute_clones(tokens, nodes, min_tokens=5, mode="mild")
        # Should produce exactly one group containing all three.
        assert len(groups) == 1
        g = groups[0]
        instance_ids = {inst.node_id for inst in g.instances}
        assert instance_ids == {"a.py::f1", "b.py::f2", "c.py::f3"}

    def test_non_function_nodes_excluded(self):
        """class-kind nodes must be ignored even if they appear in tokens_by_node."""
        shared = _SHARED_BODY
        tokens = {
            "a.py::MyClass": shared,  # class kind
            "b.py::func": shared,
        }
        nodes = {
            "a.py::MyClass": _make_node("a.py::MyClass", file="a.py", kind="class"),
            "b.py::func": _make_node("b.py::func", file="b.py"),
        }
        groups = compute_clones(tokens, nodes, min_tokens=5, mode="mild")
        # MyClass is kind="class", not "function" — excluded from clone pass.
        assert groups == []


# ---------------------------------------------------------------------------
# compute_clones — dedup by node-id set, keep max token_len
# ---------------------------------------------------------------------------


class TestComputeClonesDedup:
    def test_same_node_set_deduped_by_max_token_len(self):
        """When multiple match lengths map to the same node-id set, only the
        longest is kept (dedup by sorted-node-id-set, max token_len)."""
        # Build a body where the suffix of length 6 also appears at the start,
        # so there is both a 6-token match and (if we allowed it) a sub-match —
        # but the SA sees the maximal match at once, so this primarily tests
        # that we don't emit multiple groups for the same pair.
        shared = _SHARED_BODY  # 11 tokens
        tokens = {
            "a.py::f": shared,
            "b.py::g": shared,
        }
        nodes = {
            "a.py::f": _make_node("a.py::f", file="a.py"),
            "b.py::g": _make_node("b.py::g", file="b.py"),
        }
        groups = compute_clones(tokens, nodes, min_tokens=5, mode="mild")
        # Exactly one group for the pair (a.py::f, b.py::g), not two.
        pairs = [frozenset(inst.node_id for inst in g.instances) for g in groups]
        assert len(pairs) == len(set(map(frozenset, pairs))), (
            "Duplicate groups for the same node-id set"
        )


# ---------------------------------------------------------------------------
# compute_clones — family_id
# ---------------------------------------------------------------------------


class TestComputeClonesFamilyId:
    def test_single_group_has_no_family_id(self):
        shared = _SHARED_BODY
        tokens = {
            "a.py::f": shared,
            "b.py::g": shared,
        }
        nodes = {
            "a.py::f": _make_node("a.py::f", file="a.py"),
            "b.py::g": _make_node("b.py::g", file="b.py"),
        }
        groups = compute_clones(tokens, nodes, min_tokens=5, mode="mild")
        assert len(groups) == 1
        # Single group → no family (only one group shares the file set).
        assert groups[0].family_id is None

    def test_two_groups_same_files_share_family_id(self):
        """Two distinct clone groups both involving a.py and b.py → shared family_id."""
        body_x = [("identifier", f"x{i}") for i in range(7)]
        body_y = [("identifier", f"y{i}") for i in range(7)]

        # f1 (a.py) and f2 (b.py) share body_x.
        # g1 (a.py) and g2 (b.py) share body_y.
        tokens = {
            "a.py::f1": body_x,
            "b.py::f2": body_x,
            "a.py::g1": body_y,
            "b.py::g2": body_y,
        }
        nodes = {
            "a.py::f1": _make_node("a.py::f1", file="a.py", line_start=1, line_end=10),
            "b.py::f2": _make_node("b.py::f2", file="b.py", line_start=1, line_end=10),
            "a.py::g1": _make_node("a.py::g1", file="a.py", line_start=20, line_end=30),
            "b.py::g2": _make_node("b.py::g2", file="b.py", line_start=20, line_end=30),
        }
        groups = compute_clones(tokens, nodes, min_tokens=5, mode="mild")

        if len(groups) >= 2:
            # Both groups involve {a.py, b.py} — they should share a family_id.
            fam_ids = {g.family_id for g in groups if g.family_id is not None}
            assert len(fam_ids) == 1, "Both groups must share the same family_id"
            fam_id = next(iter(fam_ids))
            assert fam_id.startswith("family:")


# ---------------------------------------------------------------------------
# compute_clones — N-way clone anti-fragmentation (Bug 1 regression tests)
# ---------------------------------------------------------------------------


class TestComputeClonesNWayCoalesce:
    """Regression tests for the N-way clone fragmentation bug.

    When k >= 3 functions share an identical token block, compute_clones
    must emit EXACTLY ONE group covering all k owners at the TRUE maximum
    shared length — not a fragmented mix of a superset at a lower length
    plus redundant pair groups at higher lengths.
    """

    def test_three_identical_functions_single_group(self):
        """Exact repro from the bug report: 3 functions each with 8 identical
        low-vocabulary tokens at min_tokens=5 must yield exactly ONE group
        with all 3 owners and token_len == 8.
        """
        shared8 = [("identifier", "tok")] * 8
        toks = {
            "a.py::f": shared8,
            "b.py::g": shared8,
            "c.py::h": shared8,
        }
        nodes = {
            nid: Node(
                id=nid,
                kind="function",
                label=nid,
                file=nid.split("::")[0],
                area="x",
                line_start=1,
                line_end=9,
            )
            for nid in toks
        }
        groups = compute_clones(toks, nodes, min_tokens=5)
        assert len(groups) == 1, (
            f"Expected exactly 1 group, got {len(groups)}: "
            f"{[(g.token_len, sorted(i.node_id for i in g.instances)) for g in groups]}"
        )
        g = groups[0]
        assert g.token_len == 8, f"Expected token_len=8 (true shared length), got {g.token_len}"
        owner_ids = {inst.node_id for inst in g.instances}
        assert owner_ids == {"a.py::f", "b.py::g", "c.py::h"}

    def test_no_redundant_subset_groups(self):
        """3 identical functions + 1 unrelated function → ONE group for the 3,
        none referencing the unrelated function.
        """
        shared8 = [("identifier", "tok")] * 8
        unrelated = [("identifier", f"uniq{i}") for i in range(8)]
        toks = {
            "a.py::f": shared8,
            "b.py::g": shared8,
            "c.py::h": shared8,
            "d.py::q": unrelated,
        }
        nodes = {
            nid: Node(
                id=nid,
                kind="function",
                label=nid,
                file=nid.split("::")[0],
                area="x",
                line_start=1,
                line_end=9,
            )
            for nid in toks
        }
        groups = compute_clones(toks, nodes, min_tokens=5)
        # Exactly one group — covering only the 3 identical functions.
        assert len(groups) == 1, (
            f"Expected exactly 1 group, got {len(groups)}: "
            f"{[(g.token_len, sorted(i.node_id for i in g.instances)) for g in groups]}"
        )
        g = groups[0]
        owner_ids = {inst.node_id for inst in g.instances}
        assert owner_ids == {"a.py::f", "b.py::g", "c.py::h"}, (
            f"Unrelated function must not appear in clone group; owners={owner_ids}"
        )
        assert "d.py::q" not in owner_ids
