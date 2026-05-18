"""Python parser public-surface inference (ADR-016 Phase 5 §7).

The parser exposes :attr:`ParseResult.public_symbols` — node ids the parser
considers part of the file's public surface for cross-area consumption.
Convention-based: ``__all__`` overrides everything; otherwise a module-level
function/class is public iff its name does not start with ``_`` AND its
containing file is not implicitly private (``_*.py``, ``tests/`` tree,
``test_*.py``).
"""

from __future__ import annotations

from agent.graph_analyzer.parsers.python import PythonParser


def _parse(text: str, *, rel_path: str = "pkg/mod.py", area: str = "pkg"):
    return PythonParser().parse_file(
        rel_path=rel_path,
        area=area,
        source=text.encode(),
    )


class TestPublicSymbolsByName:
    def test_top_level_function_without_underscore_is_public(self) -> None:
        result = _parse("def public_helper():\n    pass\n")
        assert "pkg/mod.py::public_helper" in result.public_symbols

    def test_top_level_function_with_underscore_is_private(self) -> None:
        result = _parse(
            "def _private_helper():\n    pass\ndef public():\n    pass\n",
        )
        assert "pkg/mod.py::_private_helper" not in result.public_symbols
        assert "pkg/mod.py::public" in result.public_symbols

    def test_top_level_class_without_underscore_is_public(self) -> None:
        result = _parse("class Animal:\n    def speak(self):\n        pass\n")
        assert "pkg/mod.py::Animal" in result.public_symbols

    def test_top_level_class_with_underscore_is_private(self) -> None:
        result = _parse("class _Internal:\n    pass\n")
        assert "pkg/mod.py::_Internal" not in result.public_symbols

    def test_method_not_in_public_symbols(self) -> None:
        # Only top-level symbols matter for cross-area visibility — methods
        # carry their containing class's privacy.
        result = _parse("class Animal:\n    def speak(self): pass\n")
        method_ids = [s for s in result.public_symbols if ".speak" in s]
        assert method_ids == []


class TestDunderAll:
    def test_underscore_name_in_dunder_all_is_public(self) -> None:
        result = _parse(
            "__all__ = ['_internal_but_exposed']\ndef _internal_but_exposed():\n    pass\n",
        )
        assert "pkg/mod.py::_internal_but_exposed" in result.public_symbols

    def test_dunder_all_with_only_listed_names_drops_unlisted_public(self) -> None:
        # When __all__ is present, *only* the names it lists should be
        # considered public — that's the explicit Python convention.
        result = _parse(
            "__all__ = ['kept']\n"
            "def kept():\n    pass\n"
            "def also_public_but_not_listed():\n    pass\n",
        )
        assert "pkg/mod.py::kept" in result.public_symbols
        assert "pkg/mod.py::also_public_but_not_listed" not in result.public_symbols

    def test_dunder_all_with_tuple_works(self) -> None:
        result = _parse(
            "__all__ = ('_x', 'y')\ndef _x():\n    pass\ndef y():\n    pass\n",
        )
        assert "pkg/mod.py::_x" in result.public_symbols
        assert "pkg/mod.py::y" in result.public_symbols

    def test_dunder_all_with_garbage_is_ignored(self) -> None:
        # If __all__ is some expression we can't statically parse as a list
        # of string literals, fall back to convention-based.
        result = _parse(
            "__all__ = something()\ndef public():\n    pass\ndef _private():\n    pass\n",
        )
        assert "pkg/mod.py::public" in result.public_symbols
        assert "pkg/mod.py::_private" not in result.public_symbols


class TestImplicitlyPrivateFiles:
    def test_underscore_filename_marks_everything_private(self) -> None:
        result = _parse(
            "def public_name():\n    pass\n",
            rel_path="pkg/_internal.py",
        )
        assert result.public_symbols == set()

    def test_tests_directory_marks_everything_private(self) -> None:
        result = _parse(
            "def something():\n    pass\n",
            rel_path="tests/test_thing.py",
        )
        assert result.public_symbols == set()

    def test_top_level_tests_dir_marks_everything_private(self) -> None:
        result = _parse(
            "def something():\n    pass\n",
            rel_path="tests/sub/inner.py",
        )
        assert result.public_symbols == set()

    def test_test_prefix_module_marks_everything_private(self) -> None:
        result = _parse(
            "def thing():\n    pass\n",
            rel_path="pkg/test_thing.py",
        )
        assert result.public_symbols == set()

    def test_underscore_filename_dunder_all_still_private(self) -> None:
        # An ``_*.py`` file is structurally private — even if it tries
        # to expose names via ``__all__``, the file itself is the gate.
        result = _parse(
            "__all__ = ['exposed']\ndef exposed():\n    pass\n",
            rel_path="pkg/_internal.py",
        )
        assert result.public_symbols == set()
