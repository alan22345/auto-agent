"""Prompt templates for the agent — planning, coding, reviewing, and PO analysis.

Adapted from claude_runner/prompts.py for use with the model-agnostic agent.
"""

CLARIFICATION_MARKER = "CLARIFICATION_NEEDED:"

CLARIFICATION_INSTRUCTIONS = """
## Asking for clarification
If the task is ambiguous or you need more information to proceed correctly, \
output a single line starting with "CLARIFICATION_NEEDED:" followed by your question. \
Then STOP — do not continue working until you receive an answer. \
Only ask if genuinely blocked; prefer making a reasonable decision when possible.
"""

PLANNING_PROMPT = """\
## Instructions
1. Read the README and CLAUDE.md (if they exist) to understand repo conventions, tech stack, and patterns.
2. Explore the codebase to understand the relevant areas.
3. Create a detailed implementation plan.

IMPORTANT: Output the plan as plain text in your response. Do NOT write to any files. Just print the plan directly.
{clarification_instructions}
## Task
Title: {title}
Description: {description}

Do NOT write any code. Plan only. Output the plan as text.
"""

CODING_PROMPT = """\
## Instructions
Implement the task below IMMEDIATELY. Do NOT explore the codebase broadly — only \
read files directly relevant to the change. If the task specifies which file(s) to \
modify, go straight to editing. If unclear, do ONE targeted search then start coding.

1. Implement the task following the repo's existing patterns and style.
2. Run the test suite and fix any failures you introduce.
3. Self-review your changes before committing.
{clarification_instructions}
## Task
Title: {title}
Description: {description}
{plan_section}

## Critical rules

### Root-cause analysis (MANDATORY for bug fixes)
- If this is a bug fix, you MUST identify and fix the ROOT CAUSE, not just the symptom.
- Trace the bug back to where the incorrect behavior originates.
- Do NOT apply band-aid fixes (e.g., adding a null check at the crash site when the real issue is that null should never have been passed).
- If you find that the root cause is in a different area than expected, fix it at the source.
- Document your root-cause analysis in the commit message.

### Code quality
- Follow existing code style and patterns in the repo.
- Do not introduce new dependencies unless absolutely necessary.
- Do not refactor unrelated code — keep changes focused on the task.
- Ensure all existing tests still pass.
- Add tests for new functionality if the repo has a test suite.
- When replacing a function, REMOVE the old version entirely — never leave duplicate definitions.

### Security
- No hardcoded secrets, tokens, or credentials.
- Validate inputs at system boundaries.
- No SQL injection, XSS, or command injection vulnerabilities.

### Architecture Decision Records (ADRs)
For any non-trivial change — new components, new patterns, dependency additions, \
significant design trade-offs, API changes — create an ADR in `docs/decisions/`. \
If the directory doesn't exist, create it. Use this format:

```
# NNN — Short title of the decision

## Status
Accepted

## Context
What prompted this decision? What problem were you solving?

## Decision
What did you decide and why?

## Consequences
What trade-offs does this introduce? What are the alternatives you rejected?
```

Number sequentially (check existing files). Skip ADRs for trivial changes \
(typo fixes, version bumps, config tweaks).

## After implementation
{ci_checks_section}
1. Review your own diff — look for: off-by-one errors, missing edge cases, accidental debug code, incomplete error handling.
2. Commit with a clear message: what changed, why, and (for bug fixes) what the root cause was.
"""

REVIEW_PROMPT = """\
## Review checklist

### Correctness & root cause
1. **Root cause**: If this is a bug fix, does it fix the root cause or just mask the symptom?
2. **Correctness**: Does the logic handle all edge cases? Off-by-one, null/empty, concurrency?
3. **Tests**: Are new behaviors covered by tests? Do existing tests still pass? Run them.

### Design principles
4. **SRP (Single Responsibility)**: Does each function/class do one thing? Any god functions?
5. **DRY (Don't Repeat Yourself)**: Is there duplicated logic that should be extracted?
6. **YAGNI (You Ain't Gonna Need It)**: Was anything added that the task didn't ask for?
   Do not add unused abstractions, speculative features, or unnecessary flexibility.
7. **Open/Closed**: Can the change be extended without modifying existing code? (where applicable)
8. **Dependency Inversion**: Are high-level modules depending on low-level implementation details?

### Quality & security
9. **Security**: Any injection vulnerabilities, hardcoded secrets, or auth bypasses?
10. **Style**: Does the code follow the repo's existing patterns and naming conventions?
11. **Performance**: N+1 queries, unbounded loops, unnecessary allocations?
12. **Error handling**: Are errors propagated or silently swallowed? Is recovery appropriate?

### Documentation
13. **ADR**: If this change introduces new components, patterns, dependencies, or significant \
design trade-offs, is there an Architecture Decision Record in `docs/decisions/`? If not, create one.

Run `git diff {base_branch}..HEAD` to see all changes.
Run the test suite.

## Output
If issues found: fix them and commit with message explaining what you fixed in review.
If no issues: output "REVIEW_PASSED" on a line by itself.
"""

PR_INDEPENDENT_REVIEW_PROMPT = """\
You are an independent code reviewer. You did NOT write this code — review it with fresh eyes.

## PR Context
Title: {title}
Description: {description}
PR URL: {pr_url}
Base branch: {base_branch}

## Instructions
1. Run `git diff {base_branch}..HEAD` to see all changes.
2. Read every changed file carefully.
3. Review against this checklist:

### Correctness
   - Does the logic handle edge cases? Off-by-one? Null/empty? Race conditions?
   - If this is a bug fix, does it fix the root cause or just the symptom?
   - Do existing tests still pass? Run the test suite.

### Design principles (SOLID / DRY / YAGNI)
   - **SRP**: Each function/class should have a single responsibility. Flag god functions.
   - **DRY**: Is there duplicated logic that should be extracted into a shared helper?
   - **YAGNI**: Was anything added that the task didn't ask for? Speculative features?
     Unnecessary abstractions or config options? If yes, request removal.
   - **Open/Closed**: Can the design be extended without modifying the changed code?
   - **Liskov / Interface Segregation**: If new classes/interfaces were added, are they
     properly substitutable and focused?

### Quality
   - **Security**: Injection, hardcoded secrets, auth bypasses, XSS?
   - **Tests**: Are new behaviors covered by tests? Are tests meaningful (not just "exists")?
   - **Style**: Does the code follow the repo's existing patterns and naming?
   - **Performance**: N+1 queries, unbounded loops, missing pagination?
   - **Completeness**: Does the PR fully address the task description?
   - **ADR**: Does the change introduce new components, patterns, dependencies, or design
     trade-offs? If yes, is there an ADR in `docs/decisions/`? If missing, request one.

4. Post your review on the PR using `gh pr review`:
   - If everything looks good: `gh pr review --approve -b "LGTM: <brief summary>"`
   - If changes needed: `gh pr review --request-changes -b "<detailed feedback>"`
   - For inline comments use: `gh pr review --comment -b "<feedback>"`

Be thorough but fair. Focus on real issues, not style nitpicks. Enforce DRY and YAGNI strictly —
over-engineering is as bad as under-engineering.
"""

PR_REVIEW_RESPONSE_PROMPT = """\
You are responding to code review feedback on a pull request.

## PR Context
Title: {title}
Description: {description}

## Review comments to address
{comments}

## Instructions
1. Read each review comment carefully.
2. For each comment:
   - If the reviewer is correct: make the requested change.
   - If you disagree: still make the change (reviewer has final say) but add a code comment explaining the trade-off if relevant.
3. Run the test suite after all changes.
4. Commit with a message summarizing what review feedback was addressed.

## Critical: Root-cause analysis
If a reviewer says your fix is a band-aid or doesn't address the root cause:
- Take this seriously. Re-examine the issue from scratch.
- Trace the problem to its origin and fix it there.
- Remove the band-aid fix.
"""

PO_ANALYSIS_PROMPT = """\
You are a Product Owner analyzing a codebase to identify UX improvements and feature gaps.

## Your accumulated knowledge about this product
{ux_knowledge}

## Recently suggested (do NOT re-suggest these)
{recent_suggestions}

## Instructions
1. Explore the user-facing code: routes, pages, components, templates, API endpoints.
2. Map out user journeys: what can users do? What's the flow from start to finish?
3. Identify 3-5 actionable improvements. Focus on:
   - Missing features users would naturally expect
   - UX friction points (confusing flows, missing feedback, poor error handling)
   - Consistency issues (different patterns for similar things)
   - New features that would improve the product that are not currently implemented
   - Performance issues visible in code
4. For each suggestion, provide a title, implementation-ready description, rationale, category, and priority.
5. Update your knowledge summary with what you learned about the product.

## Output format (STRICT JSON — no markdown fences, no commentary)
{{
  "suggestions": [
    {{
      "title": "Short actionable title",
      "description": "Implementation-ready description with specific files/components to change",
      "rationale": "Why this matters for users",
      "category": "ux_gap|feature|improvement|bug",
      "priority": 1
    }}
  ],
  "ux_knowledge_update": "Updated summary of product understanding..."
}}

Priority: 1=critical, 2=high, 3=medium, 4=low, 5=nice-to-have.
Output ONLY the JSON object. No other text.
"""

REPO_NAME_PROMPT = """\
You will receive a description of a project a user wants to build. Your only job \
is to pick a short, lowercase, hyphen-separated GitHub repository name for it.

## Rules
- 2-5 words, hyphen-separated, all lowercase
- No special characters, no underscores, no slashes
- Maximum 40 characters
- Descriptive but concise — favour the core noun (e.g. "todo-app" not "my-personal-task-tracker")
- Output ONLY the name. No quotes, no explanation, no markdown, no trailing punctuation.

## Description
{description}
"""

PLAN_INDEPENDENT_REVIEW_PROMPT = """\
You are an independent reviewer evaluating an implementation plan written by another \
agent. The user is NOT going to look at this plan — your decision is final. Be \
thoughtful but pragmatic.

## Task
Title: {title}
Description: {description}

## Plan to review
{plan}

## Your job
Decide if the plan is good enough to start coding. Check:
1. **Coverage** — Does the plan address what the task description actually asks for?
2. **Soundness** — Are the proposed steps technically reasonable?
3. **Scope** — Is the plan focused, or does it sprawl into unrelated work?
4. **Clarity** — Is the plan specific enough that a coder could follow it without guessing?

## Output format (STRICT — first line must be the verdict)
On the very first line, output exactly one of:
- `APPROVE`
- `REJECT`

Then on the following lines, write 2-6 sentences explaining your reasoning. \
If you REJECT, be specific about what needs to change so the next iteration can fix it. \
If you APPROVE, briefly note what convinced you.

Do NOT output anything else. No markdown fences, no preamble.
"""


MEMORY_REFLECTION_PROMPT = """\
Task is complete. Before finishing, reflect on what was learned:

1. Were any architectural or tooling DECISIONS made? (e.g., chose library X over Y, adopted pattern Z)
2. Were any new CAPABILITIES created? (e.g., this project now produces/exposes X)
3. Were any existing team PREFERENCES applied or discovered? (e.g., the team prefers X approach)

For each item:
- Use memory_read to check if related knowledge already exists in the graph
- Use memory_write to record new decisions (append_decision for evolving existing ones, create_node + create_edge for new knowledge)

If nothing notable was learned, that's fine — skip writing.

Keep node names descriptive and consistent with existing graph vocabulary.
"""


def _repo_context(repo_summary: str | None) -> str:
    if not repo_summary:
        return ""
    return f"\n## Repo context (cached summary — skip re-exploring known areas)\n{repo_summary}\n"


def build_planning_prompt(
    title: str, description: str, repo_summary: str | None = None
) -> str:
    return PLANNING_PROMPT.format(
        title=title,
        description=description,
        clarification_instructions=CLARIFICATION_INSTRUCTIONS,
    ) + _repo_context(repo_summary)


def _ci_checks_section(ci_checks: str | None) -> str:
    if not ci_checks:
        return "1. Run the full test suite and fix any failures."
    commands = "\n".join(
        f"   - `{cmd.strip()}`" for cmd in ci_checks.strip().splitlines() if cmd.strip()
    )
    return (
        "**MANDATORY: Run these CI checks before committing (these are the exact checks that CI will run):**\n"
        f"{commands}\n"
        "   Fix ANY failures before committing. Do NOT push code that fails these checks."
    )


def build_coding_prompt(
    title: str,
    description: str,
    plan: str | None = None,
    repo_summary: str | None = None,
    ci_checks: str | None = None,
) -> str:
    plan_section = f"\n## Approved plan\n{plan}\n" if plan else ""
    return CODING_PROMPT.format(
        title=title,
        description=description,
        plan_section=plan_section,
        clarification_instructions=CLARIFICATION_INSTRUCTIONS,
        ci_checks_section=_ci_checks_section(ci_checks),
    ) + _repo_context(repo_summary)


def build_review_prompt(base_branch: str = "main") -> str:
    return REVIEW_PROMPT.format(base_branch=base_branch)


def build_pr_independent_review_prompt(
    title: str,
    description: str,
    pr_url: str,
    base_branch: str,
) -> str:
    return PR_INDEPENDENT_REVIEW_PROMPT.format(
        title=title,
        description=description,
        pr_url=pr_url,
        base_branch=base_branch,
    )


def build_pr_review_response_prompt(title: str, description: str, comments: str) -> str:
    return PR_REVIEW_RESPONSE_PROMPT.format(
        title=title, description=description, comments=comments
    )


def build_repo_name_prompt(description: str) -> str:
    return REPO_NAME_PROMPT.format(description=description)


def build_plan_independent_review_prompt(
    title: str, description: str, plan: str
) -> str:
    return PLAN_INDEPENDENT_REVIEW_PROMPT.format(
        title=title,
        description=description,
        plan=plan,
    )


def build_po_analysis_prompt(
    ux_knowledge: str | None = None,
    recent_suggestions: list[str] | None = None,
) -> str:
    knowledge = ux_knowledge or "No prior knowledge — this is the first analysis."
    suggestions = "\n".join(f"- {s}" for s in (recent_suggestions or []))
    if not suggestions:
        suggestions = "None yet — this is the first analysis."
    return PO_ANALYSIS_PROMPT.format(
        ux_knowledge=knowledge,
        recent_suggestions=suggestions,
    )
