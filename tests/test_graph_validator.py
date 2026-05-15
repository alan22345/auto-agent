"""Citation + target validator tests (ADR-016 Phase 3 §validator).

Two functions under test:

* ``validate_citation(workspace_path, edge)`` — re-opens the cited file,
  reads ``evidence.line`` ±2, returns True iff ``evidence.snippet``
  (stripped) is a substring of any line in that window.
* ``validate_target(edge, nodes)`` — returns True iff ``edge.target``
  matches a node id in the supplied list.

Both validators must be unconditional: there is no "skip in dev" flag.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent.graph_analyzer.validator import validate_citation, validate_target
from shared.types import Edge, EdgeEvidence, Node

if TYPE_CHECKING:
    from pathlib import Path


def _edge(*, snippet: str, line: int, file: str = "a.py", target: str = "x") -> Edge:
    return Edge(
        source="src",
        target=target,
        kind="calls",
        evidence=EdgeEvidence(file=file, line=line, snippet=snippet),
        source_kind="llm",
    )


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return p


class TestValidateCitation:
    def test_exact_match_on_cited_line(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "a.py",
            "line one\nhandler = HANDLERS[event_type]\nline three\n",
        )
        edge = _edge(snippet="handler = HANDLERS[event_type]", line=2)
        assert validate_citation(str(tmp_path), edge) is True

    def test_fuzzy_match_off_by_one_line(self, tmp_path: Path) -> None:
        # LLM cited line 3, but snippet actually lives on line 2.
        _write(
            tmp_path,
            "a.py",
            "line one\nhandler = HANDLERS[event_type]\nline three\n",
        )
        edge = _edge(snippet="handler = HANDLERS[event_type]", line=3)
        assert validate_citation(str(tmp_path), edge) is True

    def test_fuzzy_match_off_by_two_lines(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "a.py",
            "line one\nhandler = HANDLERS[event_type]\nline three\nline four\n",
        )
        # +2 from actual line 2 is line 4 — fuzzy match must catch it.
        edge = _edge(snippet="handler = HANDLERS[event_type]", line=4)
        assert validate_citation(str(tmp_path), edge) is True

    def test_match_outside_fuzzy_window_fails(self, tmp_path: Path) -> None:
        # Snippet is line 2 but LLM cited line 10 — outside ±2.
        _write(
            tmp_path,
            "a.py",
            "line one\nhandler = HANDLERS[event_type]\nx\nx\nx\nx\nx\nx\nx\nx\n",
        )
        edge = _edge(snippet="handler = HANDLERS[event_type]", line=10)
        assert validate_citation(str(tmp_path), edge) is False

    def test_snippet_not_in_file_fails(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.py", "line one\nline two\nline three\n")
        edge = _edge(snippet="this snippet is fabricated", line=2)
        assert validate_citation(str(tmp_path), edge) is False

    def test_snippet_with_trailing_whitespace_matches(self, tmp_path: Path) -> None:
        # ``snippet`` carries trailing whitespace from the LLM but the
        # file content does not. ``strip()`` makes it match.
        _write(tmp_path, "a.py", "result = foo()\n")
        edge = _edge(snippet="  result = foo()  ", line=1)
        assert validate_citation(str(tmp_path), edge) is True

    def test_missing_file_returns_false(self, tmp_path: Path) -> None:
        edge = _edge(snippet="anything", line=1, file="does_not_exist.py")
        assert validate_citation(str(tmp_path), edge) is False

    def test_line_zero_or_negative_returns_false(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.py", "x = 1\n")
        edge = _edge(snippet="x = 1", line=0)
        assert validate_citation(str(tmp_path), edge) is False

    def test_partial_substring_match(self, tmp_path: Path) -> None:
        # LLM trimmed a trailing comment; substring match still works.
        _write(tmp_path, "a.py", "handler = HANDLERS[name]  # registry lookup\n")
        edge = _edge(snippet="handler = HANDLERS[name]", line=1)
        assert validate_citation(str(tmp_path), edge) is True


class TestValidateTarget:
    def test_target_is_known_node(self) -> None:
        nodes = [
            Node(id="agent/foo.py::Foo.bar", kind="function", label="bar", area="agent"),
        ]
        edge = _edge(snippet="x", line=1, target="agent/foo.py::Foo.bar")
        assert validate_target(edge, nodes) is True

    def test_target_unknown_returns_false(self) -> None:
        nodes = [
            Node(id="agent/foo.py::Foo.bar", kind="function", label="bar", area="agent"),
        ]
        edge = _edge(snippet="x", line=1, target="agent/foo.py::nope")
        assert validate_target(edge, nodes) is False

    def test_empty_nodes_returns_false(self) -> None:
        edge = _edge(snippet="x", line=1, target="anything")
        assert validate_target(edge, []) is False
