"""Prompt templates for Claude Code — planning, coding, reviewing, and PR review response."""

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

IMPORTANT: Output the plan as plain text in your response. Do NOT use the plan tool or write to any files. Just print the plan directly.
{clarification_instructions}
## Task
Title: {title}
Description: {description}

Do NOT write any code. Plan only. Output the plan as text.
"""

CODING_PROMPT = """\
## Instructions
1. Read the README and CLAUDE.md (if they exist) to understand repo conventions.
2. Implement the task following the repo's existing patterns and style.
3. Run the test suite and fix any failures you introduce.
4. Self-review your changes before committing.
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

### Security
- No hardcoded secrets, tokens, or credentials.
- Validate inputs at system boundaries.
- No SQL injection, XSS, or command injection vulnerabilities.

## After implementation
{ci_checks_section}
1. Review your own diff — look for: off-by-one errors, missing edge cases, accidental debug code, incomplete error handling.
2. Commit with a clear message: what changed, why, and (for bug fixes) what the root cause was.
"""

REVIEW_PROMPT = """\
## Review checklist
1. **Root cause**: If this is a bug fix, does it fix the root cause or just mask the symptom?
2. **Correctness**: Does the logic handle all edge cases? Any off-by-one errors?
3. **Security**: Any injection vulnerabilities, hardcoded secrets, or auth bypasses?
4. **Tests**: Are new behaviors covered by tests? Do existing tests still pass?
5. **Style**: Does the code follow the repo's existing patterns?
6. **Performance**: Any obvious performance issues (N+1 queries, unbounded loops)?

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
   - **Correctness**: Does the logic handle edge cases? Off-by-one errors? Race conditions?
   - **Root cause**: If this is a bug fix, does it fix the root cause or just the symptom?
   - **Security**: Injection vulnerabilities, hardcoded secrets, auth bypasses, XSS?
   - **Tests**: Are new behaviors covered? Do existing tests still pass? Run the test suite.
   - **Style**: Does the code follow the repo's existing patterns and conventions?
   - **Performance**: N+1 queries, unbounded loops, missing pagination?
   - **Completeness**: Does the PR fully address the task description?
4. Post your review on the PR using `gh pr review`:
   - If everything looks good: `gh pr review --approve -b "LGTM: <brief summary>"`
   - If changes needed: `gh pr review --request-changes -b "<detailed feedback>"`
   - For inline comments use: `gh pr review --comment -b "<feedback>"`

Be thorough but fair. Focus on real issues, not style nitpicks.
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


def _repo_context(repo_summary: str | None) -> str:
    if not repo_summary:
        return ""
    return f"\n## Repo context (cached summary — skip re-exploring known areas)\n{repo_summary}\n"


def build_planning_prompt(title: str, description: str, repo_summary: str | None = None) -> str:
    return PLANNING_PROMPT.format(
        title=title, description=description,
        clarification_instructions=CLARIFICATION_INSTRUCTIONS,
    ) + _repo_context(repo_summary)


def _ci_checks_section(ci_checks: str | None) -> str:
    if not ci_checks:
        return "1. Run the full test suite and fix any failures."
    commands = "\n".join(f"   - `{cmd.strip()}`" for cmd in ci_checks.strip().splitlines() if cmd.strip())
    return (
        "**MANDATORY: Run these CI checks before committing (these are the exact checks that CI will run):**\n"
        f"{commands}\n"
        "   Fix ANY failures before committing. Do NOT push code that fails these checks."
    )


def build_coding_prompt(
    title: str, description: str, plan: str | None = None,
    repo_summary: str | None = None, ci_checks: str | None = None,
) -> str:
    plan_section = f"\n## Approved plan\n{plan}\n" if plan else ""
    return CODING_PROMPT.format(
        title=title, description=description, plan_section=plan_section,
        clarification_instructions=CLARIFICATION_INSTRUCTIONS,
        ci_checks_section=_ci_checks_section(ci_checks),
    ) + _repo_context(repo_summary)


def build_review_prompt(base_branch: str = "main") -> str:
    return REVIEW_PROMPT.format(base_branch=base_branch)


def build_pr_independent_review_prompt(
    title: str, description: str, pr_url: str, base_branch: str,
) -> str:
    return PR_INDEPENDENT_REVIEW_PROMPT.format(
        title=title, description=description, pr_url=pr_url, base_branch=base_branch,
    )


def build_pr_review_response_prompt(title: str, description: str, comments: str) -> str:
    return PR_REVIEW_RESPONSE_PROMPT.format(
        title=title, description=description, comments=comments
    )
