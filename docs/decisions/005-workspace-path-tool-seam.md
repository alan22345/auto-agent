# [ADR-005] Workspace path resolution as a single tool seam

## Status

Accepted

## Context

Five path-touching agent tools (`file_read`, `file_write`, `file_edit`,
`glob_tool`, `grep_tool`) each carried their own copy of the same
prefix-of-realpath sandboxing logic.

`file_read.py`, `file_write.py`, and `file_edit.py` defined identical static
`_resolve_path(file_path, workspace)` helpers. `glob_tool.py` and `grep_tool.py`
inlined a fourth and fifth variant — both subtly buggy: they called
`base.startswith(ws_real)` without the trailing `os.sep`. Consequence: a
workspace named `/work` would accept paths under `/workshop/...` because the
string `"workshop"` starts with `"work"`. The `file_*` variants correctly
appended `+ os.sep`.

This is the anti-pattern *DEEPENING.md* warns about: "pure-function-extracted-
only-for-testability with bugs at the call sites." Five copies of an invariant
guarantee the invariant won't actually be maintained — and indeed, two copies
diverged from the security check the others got right.

## Decision

Deepen `ToolContext` (in `agent/tools/base.py`) with a single `resolve(path)`
method that owns the sandboxing invariant. All path-touching tools route every
user-supplied path through it.

```python
@dataclass
class ToolContext:
    workspace: str
    ...

    def resolve(self, path: str) -> str | None:
        """Return a safe absolute realpath, or None if it escapes the sandbox."""
        ...
```

The interface is small ("hand me a relative or absolute path; I'll return a
safe absolute one or refuse"); the implementation owns the trailing-`os.sep`
guard and the realpath canonicalisation. Tools delete their `_resolve_path`
duplicates and become content-only — they validate the result and proceed.

## Consequences

- **Positive**: One place owns the sandbox invariant. The prefix-lookalike bug
  is gone — fixed for `glob_tool` and `grep_tool` automatically by routing them
  through the same code path the file tools used. Future path-touching tools
  inherit the check for free.
- **Positive**: The sandbox invariant is now testable at one interface:
  `tests/test_tool_context.py::TestResolve` includes a regression for the
  `/work` vs `/workshop/secret` case and a symlink-escape case.
- **Negative**: The five tools now have a structural dependency on
  `ToolContext.resolve`. This is fine — `ToolContext` is already the per-call
  context every tool receives.
- **Trade-off rejected**: A separate `Workspace` module with a `resolve` method
  injected as a port. Rejected because there is exactly one production
  implementation and no real second adapter — single-adapter ports are just
  indirection (per *DEEPENING.md*: "one adapter is hypothetical, two is real").
  The deepened `ToolContext` already exists; adding a method to it is the
  minimum-mass change.
