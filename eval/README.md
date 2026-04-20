# Eval Suite

Benchmark the auto-agent against Claude Code CLI on the same 10 coding tasks. Both providers run in isolated git workspaces with identical fixtures, and a Bedrock-hosted Claude Sonnet 4.6 grades the output against the rubric.

## Running

```bash
# One-time setup
npm install -g promptfoo
.venv/bin/python3 -m pip install anthropic[bedrock] botocore[crt]

# Verify Bedrock auth (either AWS_BEARER_TOKEN_BEDROCK in .env or aws sso login)
aws sts get-caller-identity  # only needed if using IAM/SSO auth

# Run the eval
cd eval && promptfoo eval --no-cache

# Browse results in a local web UI
promptfoo view
```

Takes ~10–15 minutes. 4 tests run concurrently. Each test runs once per provider (20 total runs).

## Test cases

| # | Category | Task | Fixture |
|---|----------|------|---------|
| 0 | Bug fix | Fix pagination off-by-one + case-sensitive search | `python-bug-fix/` |
| 1 | Feature | Add retry decorator with exponential backoff | `python-add-feature/` |
| 2 | Refactor | Extract duplicated validation into reusable functions | `python-refactor/` |
| 3 | Testing | Write comprehensive Jest tests for calculator | `js-add-tests/` |
| 4 | Code gen | Generate LRU cache from scratch (O(1) ops) | _(inline)_ |
| 5 | Multi-file feature | Add user preferences to Flask app (model + routes + tests) | `flask-app/` |
| 6 | Cross-module bug | Root-cause analysis — bug in `pricing.py` manifests in `test_orders.py` | `cross-file-bug/` |
| 7 | Architecture | Improve naive polling job queue with trade-off justification | `architecture-tradeoff/` |
| 8 | Migration | Migrate sync API client to async with backwards compat | `api-migration/` |
| 9 | Perf vs readability | Optimize data pipeline without breaking clarity | `performance-vs-readability/` |

Tests 5–9 specifically probe multi-file coordination, root-cause reasoning, and architectural decision-making — the stuff that separates a production agent from a code-completion tool.

## Scoring

Each test has two assertions:

1. **Custom Python assertion** — deterministic checks (diff was produced, file changes exist, tool usage efficient, timing reasonable, specific fixes present)
2. **LLM rubric** — Bedrock Claude Sonnet 4.6 grades against a task-specific rubric (correctness, trade-off reasoning, backwards compatibility, over-engineering)

### Code quality assertion (`assertions/code_quality.py`)

Score components (1.0 max):
- 0.3 — files were modified
- 0.15 — reasonable diff size (1–500 lines)
- 0.2 — efficient tool usage (≤20 calls best, 20–40 okay, >40 penalized)
- 0.15 — low token usage
- 0.2 — fast completion (<60s best, <120s okay, else penalized)

### Architecture quality assertion (`assertions/architecture_quality.py`)

Used for tests 7–10. Rewards:
- Structural changes (new classes/functions, imports, decorators)
- Test preservation (existing tests not deleted/gutted)
- **Trade-off reasoning** — explicit mentions of "chose X because Y", alternatives, backwards-compat considerations
- Proportional changes (not over-engineered)
- Code quality signals (type hints, docstrings)
- No anti-patterns introduced (TODO/FIXME/HACK markers)

### Cross-file fix assertion (`assertions/cross_file_fix.py`)

Used for test 6. Verifies the fix is in `pricing.py` (root cause) and not a band-aid in `orders.py`.

## Providers

Both providers run in isolated temp workspaces with `.gitignore` excluding `node_modules`, `__pycache__`, etc. After the agent/CLI finishes:

1. `git add -A` stages everything respecting `.gitignore`
2. `git diff --cached` captures the changes
3. Files list is filtered and capped at 50 files × 5KB each
4. Total output hard-capped at 500KB (drops largest files first, then truncates diff)

This prevents multi-MB token storms from polluting the grader.

### `providers/agent_provider.py`

Runs the in-process agent with all features: repo map, tool cache, verification gate, exploration budget, structured test runner. Collects tool call distribution + unique files read for diagnostics.

### `providers/claude_cli_provider.py`

Runs `claude --print --dangerously-skip-permissions <task>` as a subprocess. 300s timeout per test.

## Fixtures

All fixtures are small (1–5 files) self-contained projects with a working test suite in the "before" state.

- `python-bug-fix/` — Flask-style app with 3 bugs in pagination + search
- `python-add-feature/` — utils module needing a retry decorator
- `python-refactor/` — handlers with duplicated validation logic
- `js-add-tests/` — calculator module needing Jest tests
- `flask-app/` — multi-file Flask + SQLAlchemy app (app, models, routes, tests)
- `cross-file-bug/` — pricing engine bug that manifests in order tests
- `architecture-tradeoff/` — job queue with naive polling that needs redesign
- `api-migration/` — sync HTTP client with ARCHITECTURE.md documenting constraints
- `performance-vs-readability/` — data pipeline with documented perf issues

## Adding a new test

1. Create a fixture directory under `eval/fixtures/<name>/` with the starting code + a passing test suite
2. Add a test entry to `promptfooconfig.yaml`:

```yaml
- description: "Short human-readable description"
  vars:
    fixture: "<your-fixture-name>"
    task: |
      Multi-line task description.
      Include any constraints (don't modify X, preserve Y, etc).
  assert:
    - type: python
      value: "file://assertions/code_quality.py"
    - type: llm-rubric
      value: |
        Rubric — what must be true for the work to pass.
      threshold: 0.7
```

3. (Optional) Write a task-specific assertion in `assertions/<name>.py` and reference it as another `type: python` assertion.
4. Run the eval and iterate on the rubric until it correctly discriminates good vs bad solutions.

## Known limitations

- **Test 7 (architecture)** is genuinely long and can exceed the 300s promptfoo timeout on slow networks. Both providers have timed out on this test.
- **Transient Bedrock 503s** during eval runs are retried up to 4× by the agent provider; severe throttling can still cause failures.
- **CLI provider** depends on Claude Max subscription and Claude CLI being authenticated on the host (`./scripts/auth.sh`).

## Previous results

Run on `feature/model-agnostic-agent` branch, 2026-04-16:

| Metric | Auto-Agent | Claude Code CLI |
|--------|-----------|-----------------|
| Tests passed | 8/10 | 9/10 |
| Average score (on passes) | 0.88 | 0.80 |
| Tests where agent beat CLI on score | 8 | — |

Agent beats CLI on score for every test both pass. CLI edges out in pass count by passing test 2 (refactor) at exactly the 0.77 threshold (agent scored identically but fell below due to grader variance).
