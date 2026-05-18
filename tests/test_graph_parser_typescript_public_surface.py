"""TypeScript parser public-surface inference (ADR-016 Phase 5 §7).

Convention-based: an ``export``-ed top-level symbol is public unless its
file path contains an ``internal/`` or ``private/`` segment, or its
filename starts with ``_``. Non-exported symbols are always private.
"""

from __future__ import annotations

from agent.graph_analyzer.parsers.typescript import TypeScriptParser


def _parse(text: str, *, rel_path: str = "area/mod.ts", area: str = "area"):
    return TypeScriptParser().parse_file(
        rel_path=rel_path,
        area=area,
        source=text.encode(),
    )


class TestPublicSymbolsExports:
    def test_exported_function_is_public(self) -> None:
        result = _parse("export function foo() {}\n")
        assert "area/mod.ts::foo" in result.public_symbols

    def test_non_exported_function_is_private(self) -> None:
        result = _parse("function bar() {}\n")
        assert "area/mod.ts::bar" not in result.public_symbols

    def test_exported_class_is_public(self) -> None:
        result = _parse("export class Animal {}\n")
        assert "area/mod.ts::Animal" in result.public_symbols

    def test_non_exported_class_is_private(self) -> None:
        result = _parse("class Internal {}\n")
        assert "area/mod.ts::Internal" not in result.public_symbols

    def test_exported_const_is_public(self) -> None:
        result = _parse("export const FOO = 1;\n")
        assert "area/mod.ts::FOO" in result.public_symbols

    def test_exported_type_alias_is_public(self) -> None:
        result = _parse("export type Foo = { x: number };\n")
        assert "area/mod.ts::Foo" in result.public_symbols


class TestImplicitlyPrivatePaths:
    def test_internal_path_segment_marks_everything_private(self) -> None:
        result = _parse(
            "export function publicLooking() {}\n",
            rel_path="area/internal/foo.ts",
        )
        assert result.public_symbols == set()

    def test_private_path_segment_marks_everything_private(self) -> None:
        result = _parse(
            "export function publicLooking() {}\n",
            rel_path="area/private/foo.ts",
        )
        assert result.public_symbols == set()

    def test_underscore_filename_marks_everything_private(self) -> None:
        result = _parse(
            "export function publicLooking() {}\n",
            rel_path="area/_helpers.ts",
        )
        assert result.public_symbols == set()

    def test_tsx_underscore_filename_marks_everything_private(self) -> None:
        result = _parse(
            "export function Component() { return null; }\n",
            rel_path="area/_widget.tsx",
        )
        assert result.public_symbols == set()


class TestNotConfusingNamespaces:
    def test_internal_substring_in_middle_does_not_match(self) -> None:
        # ``internalapi/foo.ts`` is NOT under an ``internal/`` segment —
        # the rule matches path *segments*, not substrings.
        result = _parse(
            "export function foo() {}\n",
            rel_path="area/internalapi/foo.ts",
        )
        assert "area/internalapi/foo.ts::foo" in result.public_symbols
