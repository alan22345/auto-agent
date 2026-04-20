# Repo Map in Graph Memory — Design Spec

## Summary

Persist the AST-based repo map in graph memory so it's built once per repo and incrementally updated. Uses `git diff` against a stored commit SHA to detect staleness and only re-parse changed files.

## Current State

`build_repo_map()` in `agent/context/repo_map.py` walks the entire repo, parses every Python/JS file via AST, builds a text map. Cached in-memory per `SystemPromptBuilder` instance but rebuilt from scratch on every new agent run.

## New Behavior

### On task start (system prompt build)

1. Look up a `repo-map` memory node linked to the repo's project node
2. If found, extract `last_commit_sha` from the node content
3. Run `git diff --name-only <last_commit_sha>..HEAD` in the workspace
4. If no changed files — use the stored map as-is
5. If changed files — re-parse only those files, patch the map, update the memory node with new SHA
6. If SHA missing from history (force push/rebase) or node not found — full rebuild, store result

### After task completion

Check `WorkspaceState.files_written` and `files_edited` for modified files. Re-parse those files, patch the stored map, update the memory node with the current HEAD SHA.

### Graph structure

```
(project:<repo-name>) --[has-repo-map]--> (repo-map:<repo-name>)
```

The `repo-map` node's content format:
```
commit:<sha>
---
<existing repo map text>
```

First line stores the commit SHA. Separator `---`. Rest is the map text (same format as today).

### Staleness detection

- Store the commit SHA the map was built/updated at
- On next task start, `git diff --name-only <stored_sha>..HEAD`
- Empty diff = fresh, use as-is
- Non-empty diff = re-parse only those files
- SHA not in history = full rebuild

### Incremental update

Extract a helper that parses a single file and returns its `FileEntry`. To patch the map:
1. Parse the full stored map text back into `FileEntry` objects (simple text parsing — each indented file path starts an entry)
2. For each changed file: remove its old entry, parse the new version, insert the new entry
3. For deleted files: remove the entry
4. Re-format and store

### Fallback

If incremental update fails for any reason (parse error, git error), fall back to a full rebuild. Never serve a broken map.

## Files Changed

| File | Change |
|------|--------|
| `agent/context/repo_map.py` | Add `parse_single_file()`, `parse_map_text()`, `patch_map()`, `format_map_with_sha()` |
| `agent/context/system.py` | Make `_build_repo_map` async, add graph memory read/write for repo map |
| `agent/main.py` | After task completion, trigger incremental repo map update |
| `tests/test_repo_map.py` | Tests for incremental update, SHA parsing, staleness detection |
