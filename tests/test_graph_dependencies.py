"""Unit tests for compute_dependency_dead_code (ADR-016 Phase 10 §4b).

All tests use hand-built inputs plus a tmp_path workspace where
pyproject.toml / package.json are written directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from agent.graph_analyzer.dependencies import compute_dependency_dead_code

if TYPE_CHECKING:
    from shared.types import DeadCodeFinding

# Module-level Path use to keep ruff TC003 happy (Path used at runtime below).
_HERE = Path(__file__).parent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_pyproject(tmp_path: Path, runtime_deps: list[str]) -> None:
    """Write a minimal PEP 621 pyproject.toml with given runtime deps."""
    deps = "\n".join(f'    "{d}",' for d in runtime_deps)
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "testpkg"\nversion = "0.1.0"\ndependencies = [\n{deps}\n]\n'
    )


def write_package_json(
    tmp_path: Path,
    deps: dict[str, str] | None = None,
    dev_deps: dict[str, str] | None = None,
) -> None:
    """Write a minimal package.json."""
    data: dict = {"name": "test", "version": "1.0.0"}
    if deps:
        data["dependencies"] = deps
    if dev_deps:
        data["devDependencies"] = dev_deps
    (tmp_path / "package.json").write_text(json.dumps(data))


def findings_of_kind(findings: list[DeadCodeFinding], kind: str) -> list[str]:
    return [f.target for f in findings if f.kind == kind]


# ---------------------------------------------------------------------------
# Python tests
# ---------------------------------------------------------------------------


def test_declared_and_imported_not_flagged(tmp_path: Path) -> None:
    """Declared 'requests', imported 'module:requests.adapters' -> neither flagged."""
    write_pyproject(tmp_path, ["requests>=2.0"])
    result = compute_dependency_dead_code(
        imported_module_targets=["module:requests.adapters"],
        workspace=str(tmp_path),
        first_party_top_levels=set(),
    )
    unused = findings_of_kind(result, "unused_dependency")
    undeclared = findings_of_kind(result, "undeclared_dependency")
    assert "requests" not in unused, "requests is declared and imported, must not be unused"
    assert "requests" not in undeclared, "requests is declared, must not be undeclared"


def test_declared_but_not_imported_flagged_unused(tmp_path: Path) -> None:
    """Declared 'unusedpkg' not imported -> unused_dependency."""
    write_pyproject(tmp_path, ["unusedpkg>=1.0"])
    result = compute_dependency_dead_code(
        imported_module_targets=[],
        workspace=str(tmp_path),
        first_party_top_levels=set(),
    )
    unused = findings_of_kind(result, "unused_dependency")
    assert "unusedpkg" in unused, f"Expected unusedpkg in unused, got {unused}"


def test_imported_but_not_declared_flagged_undeclared(tmp_path: Path) -> None:
    """Imported 'module:somepkg' not declared (not stdlib/first-party) -> undeclared."""
    write_pyproject(tmp_path, ["requests>=2.0"])
    result = compute_dependency_dead_code(
        imported_module_targets=["module:somepkg"],
        workspace=str(tmp_path),
        first_party_top_levels=set(),
    )
    undeclared = findings_of_kind(result, "undeclared_dependency")
    assert "somepkg" in undeclared, f"Expected somepkg in undeclared, got {undeclared}"


def test_stdlib_import_not_flagged(tmp_path: Path) -> None:
    """Stdlib import 'module:os' -> neither unused nor undeclared."""
    write_pyproject(tmp_path, ["requests>=2.0"])
    result = compute_dependency_dead_code(
        imported_module_targets=["module:os", "module:sys", "module:collections"],
        workspace=str(tmp_path),
        first_party_top_levels=set(),
    )
    undeclared = findings_of_kind(result, "undeclared_dependency")
    assert "os" not in undeclared, "os is stdlib, must not be undeclared"
    assert "sys" not in undeclared
    assert "collections" not in undeclared


def test_first_party_import_not_flagged(tmp_path: Path) -> None:
    """First-party 'module:myapp.utils' -> neither flagged."""
    write_pyproject(tmp_path, ["requests>=2.0"])
    result = compute_dependency_dead_code(
        imported_module_targets=["module:myapp.utils"],
        workspace=str(tmp_path),
        first_party_top_levels={"myapp"},
    )
    undeclared = findings_of_kind(result, "undeclared_dependency")
    assert "myapp" not in undeclared, "first-party import must not be undeclared"


def test_alias_pyyaml_declared_yaml_imported(tmp_path: Path) -> None:
    """Declared 'pyyaml', imported 'module:yaml' -> NOT unused (alias match)."""
    write_pyproject(tmp_path, ["pyyaml>=6.0"])
    result = compute_dependency_dead_code(
        imported_module_targets=["module:yaml"],
        workspace=str(tmp_path),
        first_party_top_levels=set(),
    )
    unused = findings_of_kind(result, "unused_dependency")
    assert "pyyaml" not in unused, (
        f"pyyaml aliased from yaml import must not be unused, got {unused}"
    )
    # yaml itself must not be undeclared (it's aliased to pyyaml which IS declared)
    undeclared = findings_of_kind(result, "undeclared_dependency")
    assert "yaml" not in undeclared, (
        f"yaml aliased to pyyaml must not be undeclared, got {undeclared}"
    )


def test_tooling_packages_not_flagged_unused(tmp_path: Path) -> None:
    """pip, setuptools, wheel are declared but never imported -> NOT unused."""
    write_pyproject(tmp_path, ["pip", "setuptools", "wheel"])
    result = compute_dependency_dead_code(
        imported_module_targets=[],
        workspace=str(tmp_path),
        first_party_top_levels=set(),
    )
    unused = findings_of_kind(result, "unused_dependency")
    assert "pip" not in unused
    assert "setuptools" not in unused
    assert "wheel" not in unused


def test_types_stub_not_flagged_unused(tmp_path: Path) -> None:
    """types-requests declared, not imported -> NOT flagged unused (stub skip)."""
    write_pyproject(tmp_path, ["types-requests>=2.0"])
    result = compute_dependency_dead_code(
        imported_module_targets=[],
        workspace=str(tmp_path),
        first_party_top_levels=set(),
    )
    unused = findings_of_kind(result, "unused_dependency")
    assert "types-requests" not in unused, "stub types-requests must not be flagged unused"


# ---------------------------------------------------------------------------
# JS tests
# ---------------------------------------------------------------------------


def test_js_declared_react_not_unused(tmp_path: Path) -> None:
    """package.json deps react; imported module:react -> react not unused."""
    write_package_json(tmp_path, deps={"react": "^18.0.0"})
    result = compute_dependency_dead_code(
        imported_module_targets=["module:react"],
        workspace=str(tmp_path),
        first_party_top_levels=set(),
    )
    unused = findings_of_kind(result, "unused_dependency")
    assert "react" not in unused, f"react is imported, must not be unused, got {unused}"


def test_js_devdeps_not_flagged_unused(tmp_path: Path) -> None:
    """jest in devDependencies NOT flagged unused (dev excluded from unused check)."""
    write_package_json(
        tmp_path,
        deps={"react": "^18.0.0"},
        dev_deps={"jest": "^29.0.0"},
    )
    result = compute_dependency_dead_code(
        imported_module_targets=["module:react"],
        workspace=str(tmp_path),
        first_party_top_levels=set(),
    )
    unused = findings_of_kind(result, "unused_dependency")
    assert "jest" not in unused, "devDependency jest must not be flagged unused"


def test_js_relative_import_skipped(tmp_path: Path) -> None:
    """module:./local relative import -> skipped (not flagged undeclared)."""
    write_package_json(tmp_path, deps={"react": "^18.0.0"})
    result = compute_dependency_dead_code(
        imported_module_targets=["module:./local", "module:../sibling", "module:/absolute"],
        workspace=str(tmp_path),
        first_party_top_levels=set(),
    )
    undeclared = findings_of_kind(result, "undeclared_dependency")
    assert not any(t.startswith(".") for t in undeclared), "relative imports must not be undeclared"
    assert not any(t.startswith("/") for t in undeclared), "absolute imports must not be undeclared"


def test_js_types_at_types_not_flagged_unused(tmp_path: Path) -> None:
    """@types/node declared in deps, not imported -> NOT flagged unused."""
    write_package_json(tmp_path, deps={"@types/node": "^20.0.0", "react": "^18.0.0"})
    result = compute_dependency_dead_code(
        imported_module_targets=["module:react"],
        workspace=str(tmp_path),
        first_party_top_levels=set(),
    )
    unused = findings_of_kind(result, "unused_dependency")
    assert "@types/node" not in unused, "@types/node is a stub, must not be flagged unused"


def test_js_scoped_package_subpath(tmp_path: Path) -> None:
    """@scope/pkg/sub imported -> top-level @scope/pkg extracted."""
    write_package_json(tmp_path, deps={"@scope/pkg": "^1.0.0"})
    result = compute_dependency_dead_code(
        imported_module_targets=["module:@scope/pkg/sub"],
        workspace=str(tmp_path),
        first_party_top_levels=set(),
    )
    unused = findings_of_kind(result, "unused_dependency")
    undeclared = findings_of_kind(result, "undeclared_dependency")
    assert "@scope/pkg" not in unused, "@scope/pkg is imported via subpath, must not be unused"
    assert "@scope/pkg" not in undeclared


# ---------------------------------------------------------------------------
# Mixed Python + JS
# ---------------------------------------------------------------------------


def test_no_manifest_returns_empty(tmp_path: Path) -> None:
    """When no manifest exists, return empty list."""
    result = compute_dependency_dead_code(
        imported_module_targets=["module:requests"],
        workspace=str(tmp_path),
        first_party_top_levels=set(),
    )
    assert result == [], f"Expected empty, got {result}"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_output_is_deterministic(tmp_path: Path) -> None:
    """Calling twice with the same inputs produces identical results."""
    write_pyproject(tmp_path, ["requests>=2.0", "unusedpkg>=1.0", "pyyaml>=6.0"])
    imports = ["module:requests.adapters", "module:yaml", "module:somepkg", "module:os"]
    first = compute_dependency_dead_code(imports, str(tmp_path), set())
    second = compute_dependency_dead_code(imports, str(tmp_path), set())
    assert first == second, "compute_dependency_dead_code must be deterministic"
    # Also check the order is sorted by (kind, target)
    keys = [(f.kind, f.target) for f in first]
    assert keys == sorted(keys), "Output must be sorted by (kind, target)"


# ---------------------------------------------------------------------------
# Poetry pyproject.toml format
# ---------------------------------------------------------------------------


def test_poetry_pyproject_deps_read(tmp_path: Path) -> None:
    """[tool.poetry.dependencies] is read; 'python' key excluded."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.poetry.dependencies]\n"
        'python = "^3.11"\n'
        'requests = "^2.0"\n'
        'unusedpoetrypkg = "^1.0"\n'
    )
    result = compute_dependency_dead_code(
        imported_module_targets=["module:requests"],
        workspace=str(tmp_path),
        first_party_top_levels=set(),
    )
    unused = findings_of_kind(result, "unused_dependency")
    undeclared = findings_of_kind(result, "undeclared_dependency")
    # requests is imported and declared -> not unused, not undeclared
    assert "requests" not in unused
    assert "requests" not in undeclared
    # unusedpoetrypkg is declared but not imported -> unused
    assert "unusedpoetrypkg" in unused
    # python key must not produce any finding
    assert "python" not in unused
    assert "python" not in undeclared


# ---------------------------------------------------------------------------
# Fix 1: TS @/ path alias must NOT be flagged as undeclared
# ---------------------------------------------------------------------------


def test_ts_path_alias_not_undeclared(tmp_path: Path) -> None:
    """@/components/Button and @/lib are Next.js/Vite path aliases, not npm packages.

    They must never produce undeclared_dependency findings.
    Real scoped packages (@scope/pkg declared -> not undeclared; @scope/other
    NOT declared -> correctly undeclared) must still work.
    """
    write_package_json(
        tmp_path,
        deps={"react": "^18.0.0", "@scope/pkg": "^1.0.0"},
    )
    result = compute_dependency_dead_code(
        imported_module_targets=[
            "module:@/components/Button",
            "module:@/lib",
            "module:@scope/pkg",
            "module:@scope/other",
            "module:react",
        ],
        workspace=str(tmp_path),
        first_party_top_levels=set(),
    )
    undeclared = findings_of_kind(result, "undeclared_dependency")
    # Path aliases must never be flagged
    assert "@/components/Button" not in undeclared, (
        f"@/components/Button is a path alias, must not be undeclared; got {undeclared}"
    )
    assert "@/lib" not in undeclared, (
        f"@/lib is a path alias, must not be undeclared; got {undeclared}"
    )
    # Declared real scoped package must not be undeclared
    assert "@scope/pkg" not in undeclared, (
        f"@scope/pkg is declared, must not be undeclared; got {undeclared}"
    )
    # Undeclared real scoped package MUST be flagged (proves real scopes still work)
    assert "@scope/other" in undeclared, (
        f"@scope/other is not declared, must be undeclared; got {undeclared}"
    )


# ---------------------------------------------------------------------------
# Fix 2: Google/Azure namespace packages — no double FP
# ---------------------------------------------------------------------------


def test_google_namespace_no_double_fp(tmp_path: Path) -> None:
    """pyproject declares google-cloud-storage; source imports google.cloud.storage.

    Must produce NEITHER unused_dependency(google-cloud-storage)
    NOR undeclared_dependency(google).
    """
    write_pyproject(tmp_path, ["google-cloud-storage>=2.0"])
    result = compute_dependency_dead_code(
        imported_module_targets=["module:google.cloud.storage"],
        workspace=str(tmp_path),
        first_party_top_levels=set(),
    )
    unused = findings_of_kind(result, "unused_dependency")
    undeclared = findings_of_kind(result, "undeclared_dependency")
    assert "google-cloud-storage" not in unused, (
        f"google-cloud-storage is a namespace pkg for google.*, must not be unused; got {unused}"
    )
    assert "google" not in undeclared, (
        f"google is a namespace root, must not be undeclared; got {undeclared}"
    )


def test_namespace_root_unused_suppressed(tmp_path: Path) -> None:
    """google-auth declared but NOT imported; google.cloud.storage IS imported.

    google-auth must NOT be flagged unused because 'google' is a namespace root
    and google-auth starts with 'google-'. This is intentional conservatism —
    we cannot distinguish google-auth from google-cloud-storage at import time.
    """
    write_pyproject(tmp_path, ["google-auth>=2.0", "google-cloud-storage>=2.0"])
    result = compute_dependency_dead_code(
        imported_module_targets=["module:google.cloud.storage"],
        workspace=str(tmp_path),
        first_party_top_levels=set(),
    )
    unused = findings_of_kind(result, "unused_dependency")
    # Intentional conservatism: google-auth suppressed because namespace root 'google' is imported
    assert "google-auth" not in unused, (
        f"google-auth suppressed by namespace root conservatism; got {unused}"
    )
    assert "google-cloud-storage" not in unused, (
        f"google-cloud-storage is a namespace pkg for google.*, must not be unused; got {unused}"
    )


# ---------------------------------------------------------------------------
# Fix 3: python-pptx / python-docx / python-slugify alias map
# ---------------------------------------------------------------------------


def test_python_pptx_alias(tmp_path: Path) -> None:
    """Declares python-pptx; imports module:pptx -> neither unused nor undeclared."""
    write_pyproject(tmp_path, ["python-pptx>=0.6"])
    result = compute_dependency_dead_code(
        imported_module_targets=["module:pptx"],
        workspace=str(tmp_path),
        first_party_top_levels=set(),
    )
    unused = findings_of_kind(result, "unused_dependency")
    undeclared = findings_of_kind(result, "undeclared_dependency")
    assert "python-pptx" not in unused, (
        f"python-pptx aliased from pptx import must not be unused; got {unused}"
    )
    assert "pptx" not in undeclared, (
        f"pptx aliased to python-pptx must not be undeclared; got {undeclared}"
    )


# ---------------------------------------------------------------------------
# Fix 4: Nested manifest reading (monorepo support)
# ---------------------------------------------------------------------------


def test_nested_package_json_dep_not_undeclared(tmp_path: Path) -> None:
    """No root package.json; nested web-next/package.json declares next and clsx.

    Both must NOT be flagged as undeclared — the nested manifest is read and
    unioned into the declared set.
    """
    nested_dir = tmp_path / "web-next"
    nested_dir.mkdir()
    (nested_dir / "package.json").write_text(
        json.dumps({"name": "web-next", "dependencies": {"next": "*", "clsx": "*"}})
    )
    result = compute_dependency_dead_code(
        imported_module_targets=["module:next", "module:clsx"],
        workspace=str(tmp_path),
        first_party_top_levels=set(),
    )
    undeclared = findings_of_kind(result, "undeclared_dependency")
    assert "next" not in undeclared, (
        f"next is declared in nested package.json, must not be undeclared; got {undeclared}"
    )
    assert "clsx" not in undeclared, (
        f"clsx is declared in nested package.json, must not be undeclared; got {undeclared}"
    )


def test_nested_pyproject_dep_not_undeclared(tmp_path: Path) -> None:
    """No root pyproject.toml; nested pkg_a/pyproject.toml declares requests.

    importing module:requests must NOT be flagged undeclared.
    """
    nested_dir = tmp_path / "pkg_a"
    nested_dir.mkdir()
    (nested_dir / "pyproject.toml").write_text(
        '[project]\nname = "pkg_a"\nversion = "0.1.0"\ndependencies = ["requests>=2.0"]\n'
    )
    result = compute_dependency_dead_code(
        imported_module_targets=["module:requests"],
        workspace=str(tmp_path),
        first_party_top_levels=set(),
    )
    undeclared = findings_of_kind(result, "undeclared_dependency")
    assert "requests" not in undeclared, (
        f"requests declared in nested pyproject.toml must not be undeclared; got {undeclared}"
    )


def test_node_builtin_prefix_not_undeclared(tmp_path: Path) -> None:
    """node:path and node:fs imports must NOT be flagged undeclared.

    The ``node:`` protocol prefix must be stripped before the builtin check so
    that ``node:path`` resolves to ``path`` (a known Node builtin).
    """
    write_package_json(tmp_path, deps={"react": "^18.0.0"})
    result = compute_dependency_dead_code(
        imported_module_targets=["module:node:path", "module:node:fs", "module:react"],
        workspace=str(tmp_path),
        first_party_top_levels=set(),
    )
    undeclared = findings_of_kind(result, "undeclared_dependency")
    assert "node:path" not in undeclared, (
        f"node:path is a Node builtin, must not be undeclared; got {undeclared}"
    )
    assert "path" not in undeclared, (
        f"path (from node:path) must not be undeclared; got {undeclared}"
    )
    assert "node:fs" not in undeclared, (
        f"node:fs is a Node builtin, must not be undeclared; got {undeclared}"
    )
    assert "fs" not in undeclared, f"fs (from node:fs) must not be undeclared; got {undeclared}"


def test_excluded_dir_manifests_ignored(tmp_path: Path) -> None:
    """node_modules/somepkg/package.json must NOT be read.

    A dep declared ONLY inside node_modules must still be flagged undeclared
    — we must not pollute the declared set from excluded directories.
    """
    # Put a root package.json so the JS path is active
    write_package_json(tmp_path, deps={"react": "^18.0.0"})
    # Also put a package.json inside node_modules declaring a junk dep
    nm_dir = tmp_path / "node_modules" / "somepkg"
    nm_dir.mkdir(parents=True)
    (nm_dir / "package.json").write_text(
        json.dumps({"name": "somepkg", "dependencies": {"junkdep-only-in-node-modules": "*"}})
    )
    result = compute_dependency_dead_code(
        imported_module_targets=["module:junkdep-only-in-node-modules"],
        workspace=str(tmp_path),
        first_party_top_levels=set(),
    )
    undeclared = findings_of_kind(result, "undeclared_dependency")
    assert "junkdep-only-in-node-modules" in undeclared, (
        f"dep only declared inside node_modules must be flagged undeclared; got {undeclared}"
    )
