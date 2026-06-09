---
name: code-reads-like-prose
description: Use when writing or refactoring any function, method, or module — especially code that fetches/parses data over HTTP or DB, finds or filters items in a collection, takes many parameters, or wraps logic in loops and conditionals. Triggers on long parameter lists, comment-as-section-header blocks, and functions you can't understand from the name alone.
---

# Code Reads Like Prose

## Overview

**A function should read like a paragraph: its name tells you what, and its body is a short list of named steps at one level of abstraction.** When you read a function's name and its body, you should understand what happens *immediately* — without scanning loop bodies and `if` branches to reverse-engineer intent.

If you have to read the loops to know what the function does, the loops should have been named functions.

**The litmus test:** Read the function name, then read only the names it calls (not their bodies). Do you understand what it does? If no, you've inlined logic that wanted a name.

**Core corollary:** If you're about to write a comment that labels a block of code (`# fetch the repos`, `# keep only active ones`, `# candidate set: ...`), that comment is a function name. Extract the block and delete the comment.

## The Rules (firm — see "When NOT to" for judgment)

### 1. Name the I/O blob — don't inline fetch-and-parse

A block that calls an HTTP/DB endpoint and parses the result is a *thing with a name*. Inline it once and you'll inline it everywhere (it's how the same `GET /repos` loop ended up copy-pasted across three files).

```python
# ❌ inlined into the caller — and duplicated wherever repos are needed
async with httpx.AsyncClient() as client:
    resp = await client.get(f"{ORCHESTRATOR_URL}/repos")
    repos = resp.json()
repo_data = None
for r in repos:
    rd = RepoData.model_validate(r)
    if rd.id == repo_id:
        repo_data = rd
        break
if not repo_data:
    log.error(f"Repo '{repo_name}' (id={repo_id}) not found")
    return None

# ✅ one named owner, reused everywhere; the guard lives with the data
repo_data = await get_repo(repo_id)        # raises RepoNotFound, or returns None — its job, not the caller's
```

### 2. Name the predicate and the lookup

A filter condition with real logic, or a "find the one where…" loop, deserves a name. The caller then reads as a sentence.

```python
# ❌ reader must parse three conditions inline to know what "active" means
names = [r.name for r in repos
         if r.default_branch == "main" and r.harness_onboarded and is_github(r.url)]

# ✅ the predicate has a name; the list comp reads like English
def is_active_main_repo(repo: RepoData) -> bool:
    return repo.default_branch == "main" and repo.harness_onboarded and is_github_url(repo.url)

names = sorted(r.name for r in repos if is_active_main_repo(r))
```

### 3. A comment that labels a block = a missing function

```python
# ❌ comment narrates a block of raw code
# Candidate set: files with a defined density (cyclomatic_total + loc > 0)
# AND at least one commit in the window.
candidate_files: set[str] = set()
for f in file_cyclomatic_total:
    loc = file_loc.get(f, 0)
    if loc <= 0:
        continue
    commits = file_commit_timestamps.get(f, [])
    if not any(ts >= window_start_ts for ts in commits):
        continue
    candidate_files.add(f)

# ✅ the comment became the function name; the body reads as one step
candidates = candidate_files(file_cyclomatic_total, file_loc, file_commit_timestamps, window_start_ts)
if not candidates:
    return []
```

### 4. Group parameters past ~5 into a typed object

Many parameters — especially many optional/`None` ones — are a configuration object wearing a trench coat. Keyword-only args make the *call site* readable but the *signature* is still unreadable, and every caller repeats the plumbing.

```python
# ❌ ~20 params: impossible to read, every call site is a wall of kwargs
def create_agent(workspace, session_id=None, *, session=None, readonly=False,
                 with_web=False, with_browser=False, with_consult_architect=False,
                 max_turns=50, include_methodology=False, model_tier=None,
                 task_id=None, task_description=None, repo_name=None, complexity=None,
                 home_dir=None, org_id=None, dev_server_log_path=None, repo_id=None): ...

# ✅ cohesive groups become objects; the signature tells a story
def create_agent(workspace: str, tools: ToolConfig, task: TaskContext | None = None,
                 limits: AgentLimits = AgentLimits()) -> AgentLoop: ...
```

Group by cohesion (what travels together: the task's identity, the tool toggles, the run limits), not alphabetically.

**Special case — don't re-list the fields of the object you're building.** When a spec hands you "a function with options X, Y, Z…" or you're constructing an object that *already declares those fields with defaults*, do NOT re-enumerate them as parameters. Take the object (or a dedicated `Options` dataclass) and pass it through. A factory that copies 11 fields from its signature into an 11-field model is plumbing, not abstraction.

```python
# ❌ re-lists every field ReportJob already declares — 11 params of pure pass-through
def build_report_job(output_format="pdf", include_charts=True, max_rows=1000,
                     timezone="UTC", title="Report", author=None, recipients=None,
                     retry_count=3, verbose=False, theme="light", locale="en_US") -> ReportJob: ...

# ✅ the model already owns the fields and defaults — take options, or drop the factory entirely
def build_report_job(opts: ReportOptions) -> ReportJob: ...
# …and if the factory adds no logic beyond field-copying, delete it and construct ReportJob directly.
```

### 5. One altitude per function

A function body should not mix high-level orchestration with low-level loop mechanics. If one function both *decides what to do* and *grinds through the bytes to do it*, split it: the orchestrator calls named steps; each step does one low-level thing.

### 6. Put the docstring's narrative into the code

If your docstring lists the steps the body performs ("fetches X, filters to Y, returns Z sorted"), that's a sign the body should name those steps. Prose belongs in *function names*, not in a paragraph above raw code that re-encodes the same steps.

## Quick Reference

| Smell | Fix |
|-------|-----|
| Inline HTTP/DB fetch-and-parse | Extract `get_<thing>()` single owner |
| `for … if … : found = x; break` lookup | Extract `find_<thing>(items, key)` |
| Multi-condition `if`/comprehension filter | Name a predicate `is_<state>(x)` |
| `# label:` above a code block | Make the label the function name |
| `not found → log + return None` after a lookup | Fold the guard into the lookup function |
| >5 params, or many optional `None` params | Group cohesive params into a dataclass/Pydantic/config object |
| Docstring narrates steps the body inlines | Turn each narrated step into a named call |
| Body mixes orchestration + raw loops | Split by altitude: orchestrator calls named steps |

## When NOT to extract (judgment)

Firm rules, not blind ones. Don't over-correct into a maze of one-line wrappers:

- A **2–3 line block used once whose intent is obvious** can stay inline. Extract when it's reused, when it has a name worth saying, or when its mechanics obscure the caller.
- **Don't wrap a single library call** in a same-named function (`def get_repos(): return client.get(...)`) if it adds no name, no reuse, and no guard. The win is a *meaningful* name or a *collapsed duplication*, not indirection for its own sake.
- **3–4 genuinely cohesive parameters are fine** — don't force a config object onto `rect(x, y, w, h)`. The trigger is count *and* unrelatedness *and* optionality, not keyword args per se.
- Keyword-only args (`*`) are good and not the target — the target is the *number and unrelatedness* of the knobs.

Over-extraction has its own smell: if you can't name the extracted function without restating its whole body, it wasn't a real concept.

## Red Flags — STOP and extract/regroup

- You're writing a comment that labels the next 5 lines.
- You wrote a docstring listing steps, and the body performs exactly those steps inline.
- A function has a `for` loop containing an `if` containing the real work, and you'd have to read all three to know its purpose.
- A signature has more than ~5 parameters, or three-plus `= None` parameters.
- You copy-pasted a fetch/parse block from another file instead of importing a function.
- "I'll just inline it, it's only used here" — name it anyway if the name aids the reader, or you'll inline the second copy next week.

**All of these mean: the code does not yet read like prose. Extract the step, name the predicate, or group the params before moving on.**
