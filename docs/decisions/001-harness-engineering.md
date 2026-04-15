# [ADR-001] Add Harness Engineering Infrastructure

## Status

Accepted

## Context

This repository is managed by an autonomous AI coding agent (auto-agent). Without
guardrails, AI-generated code can drift in style, introduce unused imports, violate
module boundaries, or accumulate entropy over time. We need lightweight infrastructure
to keep the codebase consistent and maintainable.

## Decision

Add the following harness engineering infrastructure:

1. **CLAUDE.md** — Agent-facing guide with build commands, architectural constraints,
   module boundaries, and code style rules.
2. **Ruff linting** — Configured in `pyproject.toml` with strict rules for import
   sorting, naming, bugbear, and simplification. Migration files are excluded.
3. **Pre-commit hooks** — Via `.pre-commit-config.yaml` with ruff (lint + format),
   trailing whitespace, end-of-file fixer, large file detection, and debug statement
   checks.
4. **ADR directory** — `docs/decisions/` for recording architectural decisions.
5. **Entropy management config** — `.auto-agent/entropy.yml` defining idle-time
   checks for dead code, stale docs, naming drift, and large files.

## Consequences

- **Positive**: Consistent code style enforced automatically. Module boundary
  violations caught early. Architectural decisions documented for future reference.
  Entropy checked proactively during idle time.
- **Negative**: Developers must install pre-commit hooks (`pre-commit install`).
  Ruff may flag existing code on first run — these should be fixed incrementally,
  not all at once.
- **Trade-off**: We chose ruff over separate tools (black, isort, flake8) for
  simplicity — one tool handles linting and formatting.
