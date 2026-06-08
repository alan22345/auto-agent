"""Unused and undeclared dependency detection for the code-graph pipeline
(ADR-016 Phase 10 §4b).

Exposes one pure function:

    compute_dependency_dead_code(
        imported, workspace, first_party_top_levels
    ) -> list[DeadCodeFinding]

where ``imported`` is a list of ``(source_file, module_target)`` pairs.
Language is routed by the source-file extension:

* ``.py`` / ``.pyi`` → Python manifest check only (pyproject.toml / requirements*.txt)
* ``.ts`` / ``.tsx`` / ``.js`` / ``.jsx`` / ``.mjs`` / ``.cjs`` / ``.vue`` / ``.svelte`` →
  JS manifest check only (package.json)
* Unknown / ``None`` extension → skipped (conservative — never guess language)

This prevents cross-language false positives in monorepos that have BOTH a
``pyproject.toml`` AND a ``package.json``: a Python import is NEVER checked
against ``package.json`` and a JS import is NEVER checked against
``pyproject.toml``.

Two finding kinds are produced:

``undeclared_dependency``
    An external package that is imported in source files but whose
    normalised name does not appear in the workspace's dependency manifest
    (``pyproject.toml`` / ``requirements*.txt`` dependencies or
    ``package.json`` dependencies + devDependencies).  This is the
    *higher-confidence* finding because a missing declaration is very likely
    a real problem.

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

c) **Nested manifests are read and unioned** — all ``pyproject.toml``,
   ``requirements*.txt``, and ``package.json`` files found anywhere in the
   workspace (excluding ``node_modules``, ``.git``, ``.venv``, build/cache
   dirs — see ``_DEFAULT_EXCLUDE_DIRS`` in ``pipeline.py``) are read, and
   their declared deps are unioned into a single declared set.  A dep declared
   in *any* manifest (root or nested) counts as declared.  Trade-off: this is
   a conservative union across all packages in the monorepo — a dep declared
   in one sub-package suppresses undeclared findings for all sub-packages.
   This accepts false negatives (cross-package precision lost) to eliminate
   cross-package false positives, which is the right bias for an autonomous
   task proposer.

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

g) **Language routing** — each import is routed to ONLY the manifest matching
   its source-file language.  Cross-language routing (e.g. checking a Python
   import against package.json) is never performed.
"""

from __future__ import annotations

import re
import sys

from shared.types import DeadCodeFinding

# ---------------------------------------------------------------------------
# Source-file extension sets for language routing
# ---------------------------------------------------------------------------
_PY_EXTS: frozenset[str] = frozenset({".py", ".pyi"})
_JS_EXTS: frozenset[str] = frozenset(
    {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte"}
)

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
    - Strip a leading ``node:`` protocol prefix (e.g. ``node:path`` -> ``path``,
      ``node:fs/promises`` -> ``fs``) so Node built-ins are recognised by
      ``_NODE_BUILTINS`` regardless of whether the caller uses the protocol form.
    - Skip relative (``./``, ``../``) and absolute (``/``) specifiers.
    - Scoped packages: ``@scope/pkg/sub`` -> ``@scope/pkg``.
    - Bare package: ``lodash/merge`` -> ``lodash``.

    Returns ``None`` if the specifier should be skipped.
    """
    if not module_target.startswith("module:"):
        return None
    spec = module_target[len("module:") :]
    # Strip node: protocol prefix — node:path -> path, node:fs/promises -> fs
    if spec.startswith("node:"):
        spec = spec[len("node:") :]
        # Take only the part before any sub-path (e.g. fs/promises -> fs)
        spec = spec.split("/")[0]
        return spec  # always a builtin candidate; caller checks _NODE_BUILTINS
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
    """Read Python deps from ALL ``pyproject.toml`` and ``requirements*.txt`` files
    found under *workspace*.

    Walks the workspace directory tree, skipping excluded dirs (``node_modules``,
    ``.git``, ``.venv``, build/cache dirs — same set used by
    ``pipeline.walk_files``), and unions the declared deps from every
    ``pyproject.toml`` and ``requirements*.txt`` found.

    Returns ``(runtime_deps, all_deps)`` as sets of PEP-503-normalised
    names.  ``runtime_deps`` comes from ``[project].dependencies`` and
    ``[tool.poetry.dependencies]`` (Python key excluded) and from
    ``requirements*.txt`` files.  ``all_deps`` is the same set (extras/groups
    are not read in v1).

    Returns ``(set(), set())`` if no Python manifest is found anywhere.
    """
    import os

    from agent.graph_analyzer.pipeline import _DEFAULT_EXCLUDE_DIRS

    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            tomllib = None  # type: ignore[assignment]

    def _parse_pyproject(toml_path: str) -> set[str]:
        if tomllib is None:
            return set()
        try:
            with open(toml_path, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            return set()

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
        return normalized

    def _parse_requirements(req_path: str) -> set[str]:
        """Parse a requirements*.txt file and return normalised package names.

        Skips:
        - Comment lines (# ...)
        - Include flags (-r, -c)
        - Editable installs (-e)
        - Blank lines

        For each valid line, strips everything from the first of
        `` ; < > = ! ~ [ `` to get the bare package name, then PEP-503-normalises.
        """
        normalized: set[str] = set()
        try:
            with open(req_path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    # Skip -r / -c / -e flags
                    if line.startswith("-"):
                        continue
                    # Strip inline comments
                    line = line.split("#")[0].strip()
                    if not line:
                        continue
                    # Split at first version/extras/env-marker delimiter
                    # delimiters: space ; < > = ! ~ [
                    import re as _re

                    bare = _re.split(r"[\s;<>=!~\[]", line, maxsplit=1)[0].strip()
                    if bare:
                        normalized.add(_pep503_normalize(bare))
        except Exception:
            pass
        return normalized

    union: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(workspace):
        # Prune excluded dirs in-place so os.walk doesn't descend into them
        dirnames[:] = [d for d in dirnames if d not in _DEFAULT_EXCLUDE_DIRS]
        if "pyproject.toml" in filenames:
            union |= _parse_pyproject(os.path.join(dirpath, "pyproject.toml"))
        # Read any requirements*.txt files in this directory
        for fname in filenames:
            if fname.startswith("requirements") and fname.endswith(".txt"):
                union |= _parse_requirements(os.path.join(dirpath, fname))

    return union, union.copy()


def _read_js_deps(workspace: str) -> tuple[set[str], set[str]]:
    """Read JS deps from ALL ``package.json`` files found under *workspace*.

    Walks the workspace directory tree, skipping excluded dirs (``node_modules``,
    ``.git``, build/cache dirs — same set used by ``pipeline.walk_files``), and
    unions the declared deps from every ``package.json`` found.

    Returns ``(runtime_deps, all_deps)`` where ``runtime_deps`` is the union of
    ``dependencies`` across all manifests and ``all_deps`` adds ``devDependencies``.

    Returns ``(set(), set())`` if no ``package.json`` is found anywhere.
    """
    import json
    import os

    from agent.graph_analyzer.pipeline import _DEFAULT_EXCLUDE_DIRS

    runtime_union: set[str] = set()
    all_union: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(workspace):
        # Prune excluded dirs in-place so os.walk doesn't descend into them
        dirnames[:] = [d for d in dirnames if d not in _DEFAULT_EXCLUDE_DIRS]
        if "package.json" in filenames:
            try:
                with open(os.path.join(dirpath, "package.json")) as f:
                    data = json.load(f)
            except Exception:
                continue
            runtime_deps: set[str] = set(data.get("dependencies", {}).keys())
            dev_deps: set[str] = set(data.get("devDependencies", {}).keys())
            runtime_union |= runtime_deps
            all_union |= runtime_deps | dev_deps

    return runtime_union, all_union


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
    imported: list[tuple[str | None, str]],
    workspace: str,
    first_party_top_levels: set[str],
) -> list[DeadCodeFinding]:
    """Detect unused and undeclared external dependencies.

    Parameters
    ----------
    imported:
        Pre-resolution list of ``(source_file, module_target)`` pairs captured
        **before** ``_resolve_module_imports_to_files`` drops external targets
        from the graph.  ``source_file`` may be ``None`` if the edge has no
        file provenance.  Language is inferred from the source-file extension:
        - Python (``.py`` / ``.pyi``) → checked against pyproject.toml /
          requirements*.txt only.
        - JS/TS (``.ts`` / ``.tsx`` / ``.js`` / ``.jsx`` / ``.mjs`` / ``.cjs``
          / ``.vue`` / ``.svelte``) → checked against package.json only.
        - Unknown / ``None`` → skipped (conservative).
    workspace:
        Absolute path to the workspace root.  Used to read
        ``pyproject.toml``, ``requirements*.txt``, and/or ``package.json``.
    first_party_top_levels:
        Top-level module names that are first-party (i.e. they map to a
        file node in the graph).  These are never flagged as undeclared
        even if they appear in the import set.

    Returns
    -------
    list[DeadCodeFinding]
        Deterministically sorted by ``(kind, target)``.  May be empty.
        Deduplicated: the same ``(kind, target)`` pair appears at most once.
    """
    import os

    from agent.graph_analyzer.pipeline import _DEFAULT_EXCLUDE_DIRS

    # Determine whether ANY pyproject.toml / requirements*.txt / package.json
    # exists anywhere in the workspace (nested manifests count — monorepos may
    # have no root manifest).
    has_pyproject = False
    has_package_json = False
    for _dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = [d for d in dirnames if d not in _DEFAULT_EXCLUDE_DIRS]
        if not has_pyproject and (
            "pyproject.toml" in filenames
            or any(f.startswith("requirements") and f.endswith(".txt") for f in filenames)
        ):
            has_pyproject = True
        if not has_package_json and "package.json" in filenames:
            has_package_json = True
        if has_pyproject and has_package_json:
            break  # found both, no need to keep walking

    # If neither manifest is present there is nothing useful to compare
    # against — skip to avoid spurious findings.
    if not has_pyproject and not has_package_json:
        return []

    # ------------------------------------------------------------------
    # Read manifests
    # ------------------------------------------------------------------
    py_runtime_deps, py_all_deps = _read_python_deps(workspace)
    js_runtime_deps, js_all_deps = _read_js_deps(workspace)

    # A name that is ALSO a declared dependency is NOT purely first-party.
    # A first-party module sharing a leaf name with a declared package
    # (e.g. ``agent/llm/anthropic.py`` vs the ``anthropic`` dep) must not
    # shadow the real ``import anthropic``, or the declared dep is wrongly
    # flagged unused. Prefer the declared (external) interpretation. (Fix E)
    _declared_norm = {_pep503_normalize(d) for d in py_all_deps} | {
        _pep503_normalize(d) for d in js_all_deps
    }
    first_party_top_levels = {
        n for n in first_party_top_levels if _pep503_normalize(n) not in _declared_norm
    }

    # ------------------------------------------------------------------
    # Collect imported external top-levels, routed by SOURCE FILE language
    # ------------------------------------------------------------------
    # imported_py_raw: Python imports (from .py/.pyi files) before alias + normalize
    # imported_js_raw: JS imports (from .ts/.tsx/.js/.jsx/etc.) raw specifiers
    imported_py_raw: set[str] = set()
    imported_js_raw: set[str] = set()

    for source_file, target in imported:
        if not target.startswith("module:"):
            continue
        specifier = target[len("module:") :]

        # Determine language from source file extension.
        # None or unrecognised extension → skip (conservative — never guess).
        if source_file is None:
            continue
        _dot_idx = source_file.rfind(".")
        ext = source_file[_dot_idx:].lower() if _dot_idx >= 0 else ""
        if not ext:
            continue

        if ext in _PY_EXTS:
            # Python import — check against pyproject.toml / requirements*.txt only
            if not has_pyproject:
                continue
            # Skip absolute or relative specifiers
            if specifier.startswith("/") or specifier.startswith("."):
                continue
            top = specifier.split(".")[0]
            if not top:
                continue
            # Skip stdlib
            if _is_python_stdlib(top):
                continue
            # Skip first-party
            if top in first_party_top_levels:
                continue
            imported_py_raw.add(top)

        elif ext in _JS_EXTS:
            # JS/TS import — check against package.json only
            if not has_package_json:
                continue
            js_top = _top_level_js(target)
            if js_top is None:
                continue
            if js_top in _NODE_BUILTINS:
                continue
            if js_top in first_party_top_levels:
                continue
            imported_js_raw.add(js_top)
        # else: unknown extension → skip

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
    # Dedup by (kind, target) and sort deterministically.
    # ------------------------------------------------------------------
    seen: set[tuple[str, str]] = set()
    deduped: list[DeadCodeFinding] = []
    for f in findings:
        key = (f.kind, f.target)
        if key not in seen:
            seen.add(key)
            deduped.append(f)
    deduped.sort(key=lambda f: (f.kind, f.target))
    return deduped


__all__ = ["compute_dependency_dead_code"]
