"""Unused and undeclared dependency detection for the code-graph pipeline
(ADR-016 Phase 10 §4b).

Exposes one pure function:

    compute_dependency_dead_code(
        imported_module_targets, workspace, first_party_top_levels
    ) -> list[DeadCodeFinding]

Two finding kinds are produced:

``undeclared_dependency``
    An external package that is imported in source files but whose
    normalised name does not appear in the workspace's dependency manifest
    (``pyproject.toml`` dependencies or ``package.json`` dependencies +
    devDependencies).  This is the *higher-confidence* finding because a
    missing declaration is very likely a real problem.

``unused_dependency``
    A *runtime* declared dependency (``[project.dependencies]`` / Poetry
    ``[tool.poetry.dependencies]``; ``package.json`` ``dependencies`` only —
    NOT ``devDependencies``) whose normalised name matches no imported
    external package.  This detection is deliberately **conservative**:
    several categories of false-positive are suppressed (see below).

Conservative approach
---------------------
The ``unused_dependency`` finding is FP-prone because:

a) **Import-name vs package-name mismatches** — e.g. the ``pyyaml`` package
   is imported as ``yaml``.  A small alias map covers the most common cases,
   but unmapped import/package-name mismatches can still produce a paired
   unused+undeclared finding.  When in doubt, suppress, so the alias map
   errs on the side of adding entries.

b) **Plugin / entry-point dependencies** — packages like ``pytest``,
   build backends (``hatchling``, ``setuptools``), and Celery task modules are
   declared in the manifest but are never directly imported in application
   code.  ``devDependencies`` (JS) and a small Python tooling skip-set suppress
   the most common instances, but runtime plugins (e.g. SQLAlchemy dialect
   packages) may still be falsely flagged.

c) **Workspace-root manifests only** — only the root-level ``pyproject.toml``
   and ``package.json`` are read.  Monorepo workspaces with nested
   ``package.json`` files or PEP 517 sub-packages are not analysed.  This can
   produce false ``undeclared_dependency`` findings for packages declared in a
   nested manifest.

d) **Optional / extra dependency groups not analysed** — ``[project.optional-
   dependencies]``, ``[tool.poetry.group.*]``, and ``package.json``
   ``peerDependencies`` / ``optionalDependencies`` are ignored.  A package
   declared only in an extras group will be flagged as undeclared if it is
   imported.

e) **Stub-only packages** — ``types-*`` (Python) and ``@types/*`` (JS)
   packages are declaration files; they are never imported directly and are
   unconditionally excluded from ``unused_dependency`` findings.

f) **Namespace-package roots** (``google``, ``azure``, etc.) — these roots
   cannot be reliably mapped to a single PyPI package name.  Namespace roots
   are suppressed conservatively: no ``undeclared_dependency`` is emitted for
   them, and no ``unused_dependency`` is emitted for any declared package whose
   normalised name starts with ``<root>-`` (e.g. ``google-cloud-storage``,
   ``google-auth``).  This accepts false negatives to eliminate double FPs.
"""

from __future__ import annotations

import re
import sys

from shared.types import DeadCodeFinding

# ---------------------------------------------------------------------------
# Node built-ins that are never third-party packages
# ---------------------------------------------------------------------------
_NODE_BUILTINS: frozenset[str] = frozenset(
    {
        "assert",
        "buffer",
        "child_process",
        "cluster",
        "console",
        "constants",
        "crypto",
        "dgram",
        "dns",
        "domain",
        "events",
        "fs",
        "http",
        "http2",
        "https",
        "module",
        "net",
        "os",
        "path",
        "perf_hooks",
        "process",
        "punycode",
        "querystring",
        "readline",
        "repl",
        "stream",
        "string_decoder",
        "timers",
        "tls",
        "trace_events",
        "tty",
        "url",
        "util",
        "v8",
        "vm",
        "wasi",
        "worker_threads",
        "zlib",
    }
)

# ---------------------------------------------------------------------------
# Python tooling packages: never flagged as unused even if not imported.
# These are present in manifests for build/tooling reasons, not runtime import.
# ---------------------------------------------------------------------------
_PYTHON_TOOLING_SKIP: frozenset[str] = frozenset(
    {
        "pip",
        "setuptools",
        "wheel",
        "build",
        "hatchling",
        "hatch-vcs",
        "flit",
        "flit-core",
        "poetry",
        "poetry-core",
        "maturin",
        "cython",
    }
)

# ---------------------------------------------------------------------------
# Namespace-package roots: top-level module names that belong to PEP 420 /
# implicit namespace packages and cannot be reliably mapped to a single PyPI
# package name.  Suppressed conservatively — see module docstring §f.
# ---------------------------------------------------------------------------
_NAMESPACE_ROOTS: frozenset[str] = frozenset(
    {"google", "azure", "ruamel", "zope", "sphinxcontrib", "backports"}
)

# ---------------------------------------------------------------------------
# Alias map: import-name -> normalised package-name
# Covers the most common import-name vs package-name mismatches for Python.
# JS import specifiers are always the package name, so no aliasing is needed.
# ---------------------------------------------------------------------------
_PYTHON_IMPORT_ALIASES: dict[str, str] = {
    "yaml": "pyyaml",
    "cv2": "opencv-python",
    "PIL": "pillow",
    "sklearn": "scikit-learn",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
    "jwt": "pyjwt",
    "jose": "python-jose",
    "dateutil": "python-dateutil",
    "attr": "attrs",
    # NOTE: "google.cloud" alias removed — top-level extraction strips to "google",
    # which is now suppressed via _NAMESPACE_ROOTS (the old alias was unreachable).
    "serial": "pyserial",
    "usb": "pyusb",
    "OpenSSL": "pyopenssl",
    "Crypto": "pycryptodome",
    "wx": "wxpython",
    "gi": "pygobject",
    "gtk": "pygobject",
    "skimage": "scikit-image",
    "Bio": "biopython",
    "magic": "python-magic",
    # python-* package name mismatches
    "pptx": "python-pptx",
    "docx": "python-docx",
    "slugify": "python-slugify",
}


def _pep503_normalize(name: str) -> str:
    """Normalise a Python package name per PEP 503.

    Lowercases and replaces runs of ``-``, ``_``, or ``.`` with ``-``.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def _strip_python_version_specifier(dep: str) -> str:
    """Return the bare package name from a PEP 508 dependency string.

    Strips version specifiers (``>=``, ``==``, ``~=``, etc.), extras
    (``[standard]``), and environment markers.

    Examples::

        "requests>=2.0"   -> "requests"
        "uvicorn[standard]>=0.18" -> "uvicorn"
        "python"          -> "python"
    """
    # Strip environment markers
    dep = dep.split(";")[0].strip()
    # Strip extras and version specifiers: find first [ or comparison operator
    m = re.match(r"^([A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?)", dep)
    if m:
        return m.group(1)
    return dep.strip()


def _top_level_python(module_target: str) -> str | None:
    """Extract the top-level segment from a ``module:<dotted>`` target.

    Returns ``None`` if the target is not a ``module:`` string.
    """
    if not module_target.startswith("module:"):
        return None
    dotted = module_target[len("module:") :]
    return dotted.split(".")[0]


def _top_level_js(module_target: str) -> str | None:
    """Extract the top-level package specifier from a ``module:<specifier>`` target.

    Rules:
    - Skip relative (``./``, ``../``) and absolute (``/``) specifiers.
    - Scoped packages: ``@scope/pkg/sub`` -> ``@scope/pkg``.
    - Bare package: ``lodash/merge`` -> ``lodash``.

    Returns ``None`` if the specifier should be skipped.
    """
    if not module_target.startswith("module:"):
        return None
    spec = module_target[len("module:") :]
    # Skip relative and absolute specifiers
    if spec.startswith(".") or spec.startswith("/"):
        return None
    if spec.startswith("@"):
        # Scoped: @scope/pkg or @scope/pkg/sub -> @scope/pkg
        # A real npm scope cannot be empty — specs like "@/components/Button"
        # are Next.js/Vite path aliases, not packages.  Detect them by checking
        # whether the segment before the first "/" is just "@" (empty scope).
        parts = spec.split("/")
        if len(parts) >= 2 and parts[0] != "@":
            return f"{parts[0]}/{parts[1]}"
        return None  # bare "@", "@/..." path alias, or malformed
    # Plain: lodash/merge -> lodash
    return spec.split("/")[0]


def _read_python_deps(workspace: str) -> tuple[set[str], set[str]]:
    """Read Python deps from ``pyproject.toml`` at workspace root.

    Returns ``(runtime_deps, all_deps)`` as sets of PEP-503-normalised
    names.  ``runtime_deps`` comes from ``[project].dependencies`` and
    ``[tool.poetry.dependencies]`` (Python key excluded).  ``all_deps``
    is the same set (extras/groups are not read in v1).

    Returns ``(set(), set())`` if no ``pyproject.toml`` is present.
    """
    import os

    toml_path = os.path.join(workspace, "pyproject.toml")
    if not os.path.isfile(toml_path):
        return set(), set()

    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return set(), set()

    try:
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return set(), set()

    raw_names: list[str] = []

    # PEP 621: [project].dependencies
    project = data.get("project") or {}
    pep621_deps = project.get("dependencies") or []
    if isinstance(pep621_deps, list):
        raw_names.extend(pep621_deps)

    # Poetry: [tool.poetry.dependencies]
    tool = data.get("tool") or {}
    poetry = tool.get("poetry") or {}
    poetry_deps = poetry.get("dependencies") or {}
    if isinstance(poetry_deps, dict):
        for k in poetry_deps:
            if k.lower() == "python":
                continue
            raw_names.append(k)

    normalized: set[str] = set()
    for raw in raw_names:
        if not isinstance(raw, str):
            continue
        name = _strip_python_version_specifier(raw)
        if name:
            normalized.add(_pep503_normalize(name))

    return normalized, normalized.copy()


def _read_js_deps(workspace: str) -> tuple[set[str], set[str]]:
    """Read JS deps from ``package.json`` at workspace root.

    Returns ``(runtime_deps, all_deps)`` where ``runtime_deps`` is from
    ``dependencies`` only and ``all_deps`` is ``dependencies + devDependencies``.

    Returns ``(set(), set())`` if no ``package.json`` is present.
    """
    import json
    import os

    pkg_path = os.path.join(workspace, "package.json")
    if not os.path.isfile(pkg_path):
        return set(), set()

    try:
        with open(pkg_path) as f:
            data = json.load(f)
    except Exception:
        return set(), set()

    runtime_deps: set[str] = set(data.get("dependencies", {}).keys())
    dev_deps: set[str] = set(data.get("devDependencies", {}).keys())
    all_deps = runtime_deps | dev_deps
    return runtime_deps, all_deps


def _is_python_stdlib(name: str) -> bool:
    """Return True if *name* is a Python stdlib module (3.10+)."""
    stdlib = getattr(sys, "stdlib_module_names", None)
    if stdlib is not None:
        return name in stdlib
    # Fallback for Python < 3.10 — cover the most common cases
    return name in {
        "os",
        "sys",
        "re",
        "io",
        "abc",
        "ast",
        "csv",
        "enum",
        "json",
        "math",
        "time",
        "uuid",
        "copy",
        "gzip",
        "hash",
        "html",
        "http",
        "logging",
        "pathlib",
        "pickle",
        "queue",
        "random",
        "shutil",
        "socket",
        "string",
        "struct",
        "subprocess",
        "tempfile",
        "threading",
        "traceback",
        "typing",
        "unittest",
        "urllib",
        "xml",
        "zipfile",
        "collections",
        "contextlib",
        "dataclasses",
        "datetime",
        "functools",
        "hashlib",
        "hmac",
        "inspect",
        "itertools",
        "operator",
        "platform",
        "pprint",
        "signal",
        "sqlite3",
        "textwrap",
        "weakref",
        "tomllib",
        "tomli",
    }


def compute_dependency_dead_code(
    imported_module_targets: list[str],
    workspace: str,
    first_party_top_levels: set[str],
) -> list[DeadCodeFinding]:
    """Detect unused and undeclared external dependencies.

    Parameters
    ----------
    imported_module_targets:
        Pre-resolution list of ``module:<specifier>`` import-edge targets
        captured **before** ``_resolve_module_imports_to_files`` drops
        external targets from the graph.
    workspace:
        Absolute path to the workspace root.  Used to read
        ``pyproject.toml`` and/or ``package.json``.
    first_party_top_levels:
        Top-level module names that are first-party (i.e. they map to a
        file node in the graph).  These are never flagged as undeclared
        even if they appear in the import set.

    Returns
    -------
    list[DeadCodeFinding]
        Deterministically sorted by ``(kind, target)``.  May be empty.
    """
    import os

    has_pyproject = os.path.isfile(os.path.join(workspace, "pyproject.toml"))
    has_package_json = os.path.isfile(os.path.join(workspace, "package.json"))

    # If neither manifest is present there is nothing useful to compare
    # against — skip to avoid spurious findings.
    if not has_pyproject and not has_package_json:
        return []

    # ------------------------------------------------------------------
    # Read manifests
    # ------------------------------------------------------------------
    py_runtime_deps, py_all_deps = _read_python_deps(workspace)
    js_runtime_deps, js_all_deps = _read_js_deps(workspace)

    # ------------------------------------------------------------------
    # Collect imported external top-levels
    # ------------------------------------------------------------------
    # For Python: top-level segment, after alias resolution + normalisation
    # For JS: top-level specifier (as-is for declared check; normalised for Python)

    # We track two sets:
    # imported_py_normalized: PEP-503-normalised package names
    # imported_js_raw: raw JS specifiers
    imported_py_raw: set[str] = set()  # before alias + normalize
    imported_js_raw: set[str] = set()

    for target in imported_module_targets:
        if not target.startswith("module:"):
            continue
        specifier = target[len("module:") :]

        # Heuristic: if the workspace has a pyproject.toml treat bare
        # Python-like identifiers as Python; if it has package.json treat
        # npm-style specifiers (including @scope/) as JS.  When both
        # manifests are present, apply both heuristics and let the sets
        # deduplicate naturally.

        # Python heuristic: no @ prefix, no ./ or /
        if has_pyproject and not specifier.startswith("@") and not specifier.startswith("/"):
            top = specifier.split(".")[0]
            if not top:
                continue
            # Skip relative (unlikely in module: targets, but guard anyway)
            if top.startswith("."):
                continue
            # Skip stdlib
            if _is_python_stdlib(top):
                continue
            # Skip first-party
            if top in first_party_top_levels:
                continue
            imported_py_raw.add(top)

        # JS heuristic: scoped packages, or explicitly JS workspace
        if has_package_json:
            js_top = _top_level_js(target)
            if js_top is None:
                continue
            if js_top in _NODE_BUILTINS:
                continue
            if js_top in first_party_top_levels:
                continue
            imported_js_raw.add(js_top)

    # Determine which namespace roots are present among imported top-levels.
    # Used below to suppress undeclared findings for namespace roots and
    # unused findings for declared packages whose name starts with <root>-.
    active_namespace_roots: set[str] = imported_py_raw & _NAMESPACE_ROOTS

    # Normalise Python imports through alias map then PEP 503
    def _normalize_py_import(raw: str) -> str:
        aliased = _PYTHON_IMPORT_ALIASES.get(raw, raw)
        return _pep503_normalize(aliased)

    imported_py_normalized: set[str] = {_normalize_py_import(r) for r in imported_py_raw}
    # JS normalised: JS package names are their own specifiers; normalise for
    # matching against declared names (package.json keys are already canonical)
    imported_js_normalized: set[str] = {_pep503_normalize(s) for s in imported_js_raw}

    # All imported external (normalised)
    all_imported_normalized = imported_py_normalized | imported_js_normalized

    # ------------------------------------------------------------------
    # 4. undeclared_dependency findings
    # ------------------------------------------------------------------
    findings: list[DeadCodeFinding] = []

    # Python undeclared: imported but not in py_all_deps
    # We can only produce this finding when a pyproject.toml is present,
    # because without it we can't distinguish "not declared" from "declared
    # in another manifest".
    if has_pyproject:
        for raw in sorted(imported_py_raw):
            norm = _normalize_py_import(raw)
            # Skip if it matches any declared Python dep
            if norm in py_all_deps:
                continue
            # Skip stdlib (belt-and-braces after the earlier filter)
            if _is_python_stdlib(raw):
                continue
            # Skip first-party
            if raw in first_party_top_levels:
                continue
            # Skip namespace-package roots: cannot reliably map to a single
            # PyPI package — suppress to avoid undeclared FP (see §f in docstring)
            if raw in _NAMESPACE_ROOTS:
                continue
            findings.append(
                DeadCodeFinding(
                    kind="undeclared_dependency",
                    target=raw,
                    file=None,
                    reason="imported but not declared in pyproject.toml",
                )
            )

    # JS undeclared: imported but not in js_all_deps
    if has_package_json:
        for raw in sorted(imported_js_raw):
            if raw in _NODE_BUILTINS:
                continue
            if raw in first_party_top_levels:
                continue
            norm = _pep503_normalize(raw)
            # Check against all declared (deps + devDeps)
            if raw in js_all_deps or norm in {_pep503_normalize(d) for d in js_all_deps}:
                continue
            findings.append(
                DeadCodeFinding(
                    kind="undeclared_dependency",
                    target=raw,
                    file=None,
                    reason="imported but not declared in package.json",
                )
            )

    # ------------------------------------------------------------------
    # 5. unused_dependency findings (CONSERVATIVE)
    # ------------------------------------------------------------------

    # Python unused: runtime deps not imported
    if has_pyproject:
        for dep_norm in sorted(py_runtime_deps):
            # Skip tooling packages (never imported at runtime)
            if dep_norm in {_pep503_normalize(t) for t in _PYTHON_TOOLING_SKIP}:
                continue
            # Skip stub-only packages: types-*
            if dep_norm.startswith("types-"):
                continue
            # Check if this dep is covered by any imported package
            if dep_norm in imported_py_normalized:
                continue
            # Also check if the raw import name (un-aliased) would match
            # e.g. declared 'requests', imported 'requests' -> matches
            if dep_norm in all_imported_normalized:
                continue
            # Namespace-root suppression (conservative, see §f in docstring):
            # if any imported top-level is a namespace root, suppress unused
            # findings for declared packages starting with "<root>-".
            # E.g. "google" imported -> suppress "google-cloud-storage", "google-auth", etc.
            if any(dep_norm.startswith(f"{root}-") for root in active_namespace_roots):
                continue
            findings.append(
                DeadCodeFinding(
                    kind="unused_dependency",
                    target=dep_norm,
                    file=None,
                    reason="declared in pyproject.toml but never imported",
                )
            )

    # JS unused: runtime deps (dependencies only, NOT devDependencies) not imported
    if has_package_json:
        for dep in sorted(js_runtime_deps):
            dep_norm = _pep503_normalize(dep)
            # Skip @types/* stub packages
            if dep.startswith("@types/"):
                continue
            # Skip if name starts with types- (unlikely in JS but guard)
            if dep.startswith("types-"):
                continue
            # Check against imported JS set
            if dep in imported_js_raw or dep_norm in imported_js_normalized:
                continue
            findings.append(
                DeadCodeFinding(
                    kind="unused_dependency",
                    target=dep,
                    file=None,
                    reason="declared in package.json dependencies but never imported",
                )
            )

    # ------------------------------------------------------------------
    # Sort deterministically and return.
    # ------------------------------------------------------------------
    findings.sort(key=lambda f: (f.kind, f.target))
    return findings


__all__ = ["compute_dependency_dead_code"]
