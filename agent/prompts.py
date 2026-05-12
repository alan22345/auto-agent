"""Prompt templates for the agent — planning, coding, reviewing, and PO analysis."""

CLARIFICATION_MARKER = "CLARIFICATION_NEEDED:"
GRILL_DONE_MARKER = "GRILL_DONE:"
# Sentinel value stored as the final {question, answer} entry in
# tasks.intake_qa once the agent has emitted GRILL_DONE. Lets the gate
# function distinguish "grill complete with transcript" from "grill in
# progress" without adding another DB column.
GRILL_DONE_QUESTION_SENTINEL = "__grill_done__"

CLARIFICATION_INSTRUCTIONS = """
## Asking for clarification
If the task is ambiguous or you need more information to proceed correctly, \
output a single line starting with "CLARIFICATION_NEEDED:" followed by your question. \
Then STOP — do not continue working until you receive an answer. \
Only ask if genuinely blocked; prefer making a reasonable decision when possible.

The user can also ask YOU questions back instead of answering. If their reply \
contains a question, pushback, or "I don't know — what do you suggest?", \
address it in your next turn: write your answer/recommendation on the lines \
AFTER `CLARIFICATION_NEEDED:` (those lines are shown to the user) and then \
re-ask your original question (or rephrase it). The conversation continues \
until the user actually gives you the information you need or you have \
enough to proceed.
"""


# Grill-before-planning prompt — runs as a multi-round Q&A with the user before
# any plan is written. Each turn produces ONE clarifying question (or
# GRILL_DONE) so the orchestrator can route it through the existing
# AWAITING_CLARIFICATION state.
GRILL_PROMPT = """\
## You are in the GRILL phase

Before writing any plan, you grill the user to align on design — domain
language, seams, dependency category, what "done" means, and what NOT to
change. This is interactive: you ask ONE question per turn, the user
answers, and you continue. Do NOT output a plan yet.

Load these skills before your first question:
- `skill(name='grill-with-docs')` — the grilling protocol.
- `skill(name='improve-codebase-architecture')` — depth/seam vocabulary so
  questions probe the right places.

## Task
Title: {title}
Description: {description}

## Grill so far
{grill_history}

## What to do this turn

1. If you genuinely have enough to plan a sensible design AND you've asked at \
least 3 questions (or you've covered domain language, seams, "done", \
constraints, and the caller), output exactly one line:

       GRILL_DONE: <one-line rationale>

   …then STOP. The orchestrator will move you to the planning prompt.

2. Otherwise output exactly one clarifying question using the existing \
clarification mechanism. Provide your recommended answer. STOP after the \
question.

       CLARIFICATION_NEEDED: <question>
       Recommended answer: <your answer, with reasoning>

   If the user's previous "answer" is actually a question back to you \
(e.g. "what do you mean by X?" or "I don't know, you choose"), write your \
reply to them on the lines after `CLARIFICATION_NEEDED:` (those lines are \
shown to the user verbatim) and then re-ask the original question (or a \
refined version). Treat grilling as a real conversation — the user may \
push back, ask back, or defer to you, and you should respond, not march on.

Cover, across rounds: (a) domain language — terms in the task that conflict \
with or extend `CONTEXT.md`; (b) seams + dependency category (in-process / \
local-substitutable / remote-owned / true-external — see DEEPENING.md); \
(c) what "done" means concretely; (d) what NOT to change; (e) who the \
caller / consumer is. Aim for 3–7 questions total.

DO NOT output a plan in this turn. Either GRILL_DONE: or one CLARIFICATION_NEEDED:.
"""


PLANNING_PROMPT = """\
## Instructions
1. Read the README and CLAUDE.md (if they exist) to understand repo conventions, tech stack, and patterns.
2. Explore the codebase to understand the relevant areas.
3. Create a detailed implementation plan using the structure below.

IMPORTANT: Output the plan as plain text in your response. Do NOT write to any files. Just print the plan directly.
{clarification_instructions}
## Task
Title: {title}
Description: {description}
{grill_section}
## Required plan structure
Your plan MUST include these sections in this order:

### Goal
Restate what this task accomplishes in 2-3 concrete sentences. Prove you understand the user's intent.

### Acceptance Criteria
Bullet list of testable conditions that must be true when the task is done. Be specific — \
"works correctly" is not a criterion; "login succeeds on mobile Safari with valid credentials" is.

### Files to Modify
List every file you expect to create or modify, with a one-line note on what changes:
- `path/to/file.py` — add new endpoint for X
- `path/to/test.py` — add test coverage for X

### Implementation Phases
Break the work into sequential phases (## Phase 1, ## Phase 2, etc.). Each phase should:
- Have a clear, descriptive title
- List the specific changes to make
- Be independently committable

Frame the plan through the `improve-codebase-architecture` lens: identify the \
modules, interfaces, and seams involved; apply the deletion test to anything \
new; favour deep modules over shallow pass-throughs; name modules using the \
project's domain language.

Do NOT write any code. Plan only. Output the plan as text.
"""

# Coding prompt when a plan exists — implement immediately
CODING_PROMPT_WITH_PLAN = """\
## Instructions
Implement the task below IMMEDIATELY. Do NOT explore the codebase broadly — only \
read files directly relevant to the change. If the task specifies which file(s) to \
modify, go straight to editing. If unclear, do ONE targeted search then start coding.
{skill_directive}
1. Implement the task following the repo's existing patterns and style.
2. Run the test suite and fix any failures you introduce.
3. Self-review your changes before committing.
{clarification_instructions}
## Task
Title: {title}
Description: {description}
{intent_section}
{plan_section}

{critical_rules}
"""

# Coding prompt when NO plan exists — understand first, then code
CODING_PROMPT_NO_PLAN = """\
## Instructions
Before writing any code, briefly summarize what you are about to do in 2-3 sentences. \
State which files you expect to modify and what the end result should look like. \
Then implement.
{skill_directive}
1. Restate the task: what are you changing, where, and what does "done" look like?
2. Do ONE targeted search to find the relevant code.
3. Implement following the repo's existing patterns and style.
4. Run the test suite and fix any failures you introduce.
5. Self-review your changes before committing.
{clarification_instructions}
## Task
Title: {title}
Description: {description}
{intent_section}

{critical_rules}
"""

# Shared critical rules (used by both coding prompt variants)
_CRITICAL_RULES = """\
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

### Architecture (deepening lens)
Every module you write or change is judged by the depth/seam/locality lens \
(see the system prompt for the full vocabulary). For any change that introduces \
a new module or alters an existing module's interface:
- Apply the **deletion test** before adding anything new — if removing the new \
  module wouldn't concentrate complexity, don't add it.
- Prefer **deep modules** (small interface, large implementation). A wrapper \
  that just forwards calls is shallow — push the logic into the deeper module \
  or back into the caller instead of adding indirection.
- **One adapter is hypothetical, two is real.** Don't introduce a port at a \
  seam unless at least two adapters (typically production + test) justify it.
- Name modules using the project's `CONTEXT.md` domain language when present, \
  otherwise the clearest noun for what the module *does* — never \
  `FooBarHandler`, never abbreviated names, avoid generic `Service`/`Manager` \
  if a more specific term fits.
- In the commit message, name which deepening choice you made: \
  "kept depth", "deepened <module>", "new real seam (justified by adapters X, Y)", \
  or "no architecturally significant choice" if the change is purely local.

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
2. Commit with a clear message: what changed, why, and (for bug fixes) what the root cause was."""

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
{goal_section}
## Market context (brief from the market researcher)

### Product category
{brief_product_category}

### Competitors and what they offer
{brief_competitors}

### Themes from the market
{brief_findings}

### Modality opportunities (voice, vision, AI-native, multi-modal)
{brief_modality_gaps}

### Strategic / why-now themes
{brief_strategic_themes}

### Brief summary
{brief_summary}

## Your accumulated knowledge about this product
{ux_knowledge}

## Recently suggested (do NOT re-suggest these)
{recent_suggestions}

## Instructions

1. Explore the user-facing code: routes, pages, components, templates, API endpoints.
2. Map out user journeys: what can users do? What's the flow from start to finish?
3. Identify 3-5 actionable improvements. For EACH suggestion:
   - It must be motivated by at least one item from the Market context above
     OR be an obvious bug / UX defect (in which case category="bug" and
     evidence_urls=[]).
   - Prefer suggestions that introduce a new capability or modality the repo
     currently lacks over suggestions that polish what already exists.
   - Cite the URLs your suggestion draws from in `evidence_urls`.
   - Drop suggestions you cannot ground in either market evidence or a
     visible repo defect — those are the "button-sized" suggestions we
     don't want.
   {goal_directive}
4. Update your knowledge summary with what you learned about the product.

## Output format (STRICT JSON — no markdown fences, no commentary)
{{
  "suggestions": [
    {{
      "title": "Short actionable title",
      "description": "Implementation-ready description with specific files/components to change",
      "rationale": "Why this matters for users",
      "category": "ux_gap|feature|improvement|bug",
      "priority": 1,
      "evidence_urls": [
        {{"url": "https://...", "title": "Source title", "excerpt": "what was said"}}
      ]
    }}
  ],
  "ux_knowledge_update": "Updated summary of product understanding..."
}}

Priority: 1=critical, 2=high, 3=medium, 4=low, 5=nice-to-have.
Output ONLY the JSON object. No other text.
"""

ARCHITECTURE_ANALYSIS_PROMPT = """\
You are an architectural reviewer analysing this codebase for **deepening
opportunities** — refactors that turn shallow modules into deep ones, with
the aim of improving testability and AI-navigability.

## Load these skills first

Before exploring, load the architecture skill so your suggestions use its
vocabulary exactly:

- `skill(name='improve-codebase-architecture')` — depth, seam, leverage,
  locality, the deletion test, and the format for candidates.
- `skill(name='grill-with-docs')` — for any term you'd flag in suggestions.

## Your accumulated knowledge about this repo's architecture

{architecture_knowledge}

## Recently suggested (do NOT re-suggest these)

{recent_suggestions}

## Instructions

1. Read `CONTEXT.md` (if present) and ADRs in `docs/decisions/` to learn the
   project's domain language and prior decisions. Don't re-litigate decisions
   already recorded as ADRs.
2. Walk the codebase organically (use grep + file_read; dispatch a `subagent`
   if breadth helps). Note where you experience friction:
   - Modules whose interfaces are nearly as complex as their implementations
     (shallow).
   - Pure functions extracted only for testability — bugs hide at call sites.
   - Tightly coupled modules leaking across seams.
   - Untested modules, or modules hard to test through the current interface.
3. Apply the **deletion test** to anything you suspect is shallow.
4. Produce 3–5 ranked deepening opportunities. Use `LANGUAGE.md` vocabulary
   for the architecture (module/interface/seam/adapter/depth/leverage/
   locality) and `CONTEXT.md` vocabulary for the domain.
5. Update your knowledge summary with what you learned about the depth state.

## Output format (STRICT JSON — no markdown fences, no commentary)
{{
  "suggestions": [
    {{
      "title": "Short title (use the affected module name from CONTEXT.md if applicable)",
      "description": "Files involved + Problem + Solution + Benefits — plain English, deepening-lens vocabulary",
      "rationale": "Why this matters in terms of locality and leverage; how tests would improve",
      "category": "architecture",
      "priority": 1
    }}
  ],
  "architecture_knowledge_update": "Updated map of the repo's depth state..."
}}

Priority: 1=critical (load-bearing shallow seams), 2=high, 3=medium, 4=low, 5=nice-to-have.
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
- Use recall to check if related knowledge already exists in the graph
- Use remember to record new decisions or capabilities as facts on a named entity
- Use correct to update an existing fact when the situation has evolved (this preserves history)
- Use relate to link related entities together

If nothing notable was learned, that's fine — skip writing.

Keep entity names descriptive and consistent with existing graph vocabulary.
"""


def _repo_context(repo_summary: str | None) -> str:
    if not repo_summary:
        return ""
    return f"\n## Repo context (cached summary — skip re-exploring known areas)\n{repo_summary}\n"


def _grill_history(intake_qa: list[dict] | None) -> str:
    """Render the grill Q&A list as a readable transcript.

    Filters out the GRILL_DONE sentinel entry — that's bookkeeping for the
    state machine, not something the agent or the user should see.
    """
    if not intake_qa:
        return "(no questions asked yet)"
    visible = [
        qa for qa in intake_qa
        if qa.get("question") != GRILL_DONE_QUESTION_SENTINEL
    ]
    if not visible:
        return "(no questions asked yet)"
    lines = []
    for i, qa in enumerate(visible, start=1):
        question = (qa.get("question") or "").strip()
        answer = (qa.get("answer") or "").strip()
        lines.append(f"### Q{i}\n{question}\n\n**A{i}:** {answer}")
    return "\n\n".join(lines)


def build_grill_phase_prompt(
    title: str,
    description: str,
    intake_qa: list[dict] | None = None,
    repo_summary: str | None = None,
) -> str:
    """Build the grill-phase prompt: ONE question per turn, or GRILL_DONE.

    Used before any plan is written, so the agent aligns on design with the
    user via the existing AWAITING_CLARIFICATION round-trip. ``intake_qa`` is
    the running list of {question, answer} pairs; the prompt renders it so
    the agent doesn't repeat questions.
    """
    return GRILL_PROMPT.format(
        title=title,
        description=description,
        grill_history=_grill_history(intake_qa),
    ) + _repo_context(repo_summary)


def _grill_section_for_planning(intake_qa: list[dict] | None) -> str:
    """Inject grilled answers into the planning prompt, after grill exits."""
    if not intake_qa:
        return ""
    return (
        "\n## Pre-flight grill (Q&A from before planning)\n"
        f"{_grill_history(intake_qa)}\n\n"
        "Use these answers — do not re-ask.\n"
    )


PLANNING_AFFECTED_ROUTES_INSTRUCTION = """
## Affected routes
At the end of your plan, include a fenced JSON block listing the user-visible
routes this change affects, if any. Format:

```affected-routes
[
  {"method": "GET", "path": "/", "label": "short label"}
]
```

Rules:
- Include each route where the change makes user-visible output appear or change.
- If the change is purely backend, CLI, library, or docs with no rendered UI,
  emit an empty list: ```affected-routes\\n[]\\n```
- Use GET for page renders; only list other methods if they have a visual response.
"""


def build_planning_prompt(
    title: str,
    description: str,
    repo_summary: str | None = None,
    intake_qa: list[dict] | None = None,
) -> str:
    return (
        PLANNING_PROMPT.format(
            title=title,
            description=description,
            clarification_instructions=CLARIFICATION_INSTRUCTIONS,
            grill_section=_grill_section_for_planning(intake_qa),
        )
        + _repo_context(repo_summary)
        + PLANNING_AFFECTED_ROUTES_INSTRUCTION
    )


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


def _intent_section(intent: dict | None) -> str:
    if not intent:
        return ""
    parts = ["## Structured intent"]
    if intent.get("change_type"):
        parts.append(f"- **Type:** {intent['change_type']}")
    if intent.get("target_areas"):
        parts.append(f"- **Target areas:** {intent['target_areas']}")
    if intent.get("acceptance_criteria"):
        parts.append(f"- **Done when:** {intent['acceptance_criteria']}")
    if intent.get("constraints"):
        parts.append(f"- **Constraints:** {intent['constraints']}")
    return "\n".join(parts)


# Map structured-intent change_type → which skills the agent should load
# before writing any code. Auto-routes the engineering skills (vendored under
# skills/engineering/) into the relevant phase of work.
def _skill_directive(intent: dict | None) -> str:
    if not intent:
        return ""
    change_type = (intent.get("change_type") or "").strip().lower()
    if change_type == "bugfix":
        return (
            "\n**Before writing any fix, call `skill(name='diagnose')` and follow its "
            "phases (build a feedback loop, reproduce, hypothesise, instrument, "
            "fix + regression test).**\n"
        )
    if change_type in ("feature", "refactor", "performance"):
        return (
            "\n**Before writing implementation, call `skill(name='tdd')` and "
            "`skill(name='improve-codebase-architecture')`, then implement in "
            "vertical slices (one test → one minimal implementation → repeat). "
            "Apply the deletion test and prefer deep modules over shallow "
            "pass-throughs.**\n"
        )
    return ""


def build_coding_prompt(
    title: str,
    description: str,
    plan: str | None = None,
    repo_summary: str | None = None,
    ci_checks: str | None = None,
    intent: dict | None = None,
) -> str:
    intent_section = _intent_section(intent)
    skill_directive = _skill_directive(intent)
    critical_rules = _CRITICAL_RULES.format(ci_checks_section=_ci_checks_section(ci_checks))

    if plan:
        plan_section = f"\n## Approved plan\n{plan}\n"
        result = CODING_PROMPT_WITH_PLAN.format(
            title=title,
            description=description,
            plan_section=plan_section,
            intent_section=intent_section,
            skill_directive=skill_directive,
            clarification_instructions=CLARIFICATION_INSTRUCTIONS,
            critical_rules=critical_rules,
        )
    else:
        result = CODING_PROMPT_NO_PLAN.format(
            title=title,
            description=description,
            intent_section=intent_section,
            skill_directive=skill_directive,
            clarification_instructions=CLARIFICATION_INSTRUCTIONS,
            critical_rules=critical_rules,
        )
    return result + _repo_context(repo_summary)


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


_UI_CHECK_PROMPT_SUFFIX_CODE_ONLY = """

## Verdict format (required)

Emit your verdict as a single JSON object on the FIRST non-empty line of your
reply:

{
  "code_review": {"verdict": "OK" | "NOT-OK", "reasoning": "..."},
  "ui_check":   {"verdict": "SKIPPED",        "reasoning": "no UI to check"}
}

After the JSON object, you may add free-form notes. The JSON is what counts.
"""


def build_pr_independent_review_prompt_with_ui_check(
    title: str,
    description: str,
    pr_url: str,
    base_branch: str,
    *,
    server_url: str | None,
    affected_routes: list[dict],
) -> str:
    """Reviewer prompt with optional UI-check guidance.

    When a dev server is running and routes were declared, instruct the reviewer
    to drive ``browse_url`` through each route and emit a combined code+UI
    verdict. When the server isn't running, fall back to the code-only suffix
    so the JSON shape stays uniform.
    """
    base = build_pr_independent_review_prompt(title, description, pr_url, base_branch)
    if not server_url or not affected_routes:
        return base + _UI_CHECK_PROMPT_SUFFIX_CODE_ONLY
    route_lines = "\n".join(
        f"- {r.get('method', 'GET')} {r['path']} ({r.get('label', '')})"
        for r in affected_routes
    )
    return base + f"""

## UI check (required)

A dev server is running at {server_url}. The following routes are affected by this PR:
{route_lines}

For each affected route, call ``browse_url`` (e.g. ``browse_url("{server_url}/")``)
and judge the rendered output against the diff and the task description. Use
``tail_dev_server_log`` to inspect server logs if anything looks wrong.

## Verdict format (required)

Emit your verdict as a single JSON object on the FIRST non-empty line of your
reply, with this exact shape:

{{
  "code_review": {{"verdict": "OK" | "NOT-OK", "reasoning": "..."}},
  "ui_check":   {{"verdict": "OK" | "NOT-OK", "reasoning": "..."}}
}}

If the code looks bad, set ``code_review.verdict`` to "NOT-OK". If a route does
not render correctly, set ``ui_check.verdict`` to "NOT-OK". Both must be "OK"
to ship.
"""


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
    *,
    brief,  # MarketBrief — required (kw-only to prevent positional confusion)
    ux_knowledge: str | None = None,
    recent_suggestions: list[str] | None = None,
    goal: str | None = None,
) -> str:
    knowledge = ux_knowledge or "No prior knowledge — this is the first analysis."
    suggestions = "\n".join(f"- {s}" for s in (recent_suggestions or []))
    if not suggestions:
        suggestions = "None yet — this is the first analysis."

    goal_clean = (goal or "").strip()
    if goal_clean:
        goal_section = (
            "\n## Goal (the product owner's objective for this analysis)\n"
            f"{goal_clean}\n\n"
            "Every suggestion you propose must move the product toward this "
            "goal. Drop any otherwise-good ideas that don't.\n"
        )
        goal_directive = (
            "- **Goal alignment** — items that directly serve the stated goal "
            "above take priority over generic improvements."
        )
    else:
        goal_section = ""
        goal_directive = ""

    def _bullet_list(items, fmt):
        if not items:
            return "(none)"
        return "\n".join(fmt(i) for i in items)

    competitors = _bullet_list(
        brief.competitors or [],
        lambda c: f"- **{c.get('name','?')}** ({c.get('url','')}) — {c.get('why_relevant','')}",
    )
    findings = _bullet_list(
        brief.findings or [],
        lambda f: f"- **{f.get('theme','?')}**: {f.get('observation','')}  "
                  f"[sources: {', '.join(f.get('sources', []))}]",
    )
    modality_gaps = _bullet_list(
        brief.modality_gaps or [],
        lambda m: f"- **{m.get('modality','?')}**: {m.get('opportunity','')}  "
                  f"[sources: {', '.join(m.get('sources', []))}]",
    )
    strategic_themes = _bullet_list(
        brief.strategic_themes or [],
        lambda t: f"- **{t.get('theme','?')}**: {t.get('why_now','')}  "
                  f"[sources: {', '.join(t.get('sources', []))}]",
    )

    return PO_ANALYSIS_PROMPT.format(
        goal_section=goal_section,
        goal_directive=goal_directive,
        ux_knowledge=knowledge,
        recent_suggestions=suggestions,
        brief_product_category=brief.product_category or "(unknown)",
        brief_competitors=competitors,
        brief_findings=findings,
        brief_modality_gaps=modality_gaps,
        brief_strategic_themes=strategic_themes,
        brief_summary=brief.summary or "(no summary)",
    )


def build_architecture_analysis_prompt(
    architecture_knowledge: str | None = None,
    recent_suggestions: list[str] | None = None,
) -> str:
    """Build the prompt that drives architecture-mode (continuous deepening)."""
    knowledge = architecture_knowledge or (
        "No prior knowledge — this is the first architecture pass."
    )
    suggestions = "\n".join(f"- {s}" for s in (recent_suggestions or []))
    if not suggestions:
        suggestions = "None yet — this is the first analysis."
    return ARCHITECTURE_ANALYSIS_PROMPT.format(
        architecture_knowledge=knowledge,
        recent_suggestions=suggestions,
    )


MARKET_RESEARCH_PROMPT = """\
You are a market researcher producing a brief that a Product Owner agent
will read to ground its suggestions for the repo "{repo_name}".

## Phase 1 — Anchor on what the product is

Read `README.md` (first ~100 lines) and `CONTEXT.md` (if present). Then
`glob` the top-level routes/pages **by filename only** — do NOT read their
contents. Do NOT read manifest or dependency files (too long, token-wasteful,
low signal).

Output (mentally) a one-paragraph product description and an inferred
product category (e.g. "AI dev tools", "social music app", "internal
admin dashboard").

## Phase 2 — Discover competitors

Use `web_search` for the category and adjacent terms. Pick 3-5
representative competing/comparable products. For each, `fetch_url` the
landing or features page and extract what they actually offer.

## Phase 3 — Three lenses

For each lens, search the web and synthesize findings. Every claim must
carry the URL it came from.

- **Competitive lens** — what do competitors have that this repo doesn't?
- **Modality lens** — voice, vision, AI-native, multi-modal angles that
  competitors are exploring. Search "<category> voice", "<category> AI",
  etc.
- **Strategic / why-now lens** — recent launches, funding signals, public
  roadmaps, trend reports. What's the market doing right now that makes
  this product timely?

## Phase 4 — Synthesize the brief

Output a strict JSON object matching the schema below. **Hard rule:** no
claim, observation, or opportunity may appear without at least one source
URL in its `sources` field. If you cannot cite it, drop it.

## Output format (STRICT JSON — no markdown fences, no commentary)

{{
  "product_category": "Inferred category",
  "competitors": [
    {{"name": "Competitor name", "url": "https://...", "why_relevant": "..."}}
  ],
  "findings": [
    {{"theme": "...", "observation": "...", "sources": ["url1", "url2"]}}
  ],
  "modality_gaps": [
    {{"modality": "voice|vision|ai-native|multimodal|...",
      "opportunity": "...", "sources": ["url1"]}}
  ],
  "strategic_themes": [
    {{"theme": "...", "why_now": "...", "sources": ["url1"]}}
  ],
  "summary": "2-4 sentence prose digest the PO will read first."
}}

Output ONLY the JSON object. No other text.
"""


def build_market_research_prompt(repo_name: str) -> str:
    return MARKET_RESEARCH_PROMPT.format(repo_name=repo_name)


def build_verify_intent_prompt(
    task_title: str,
    task_description: str,
    diff_summary: str,
    affected_routes: list[dict],
    server_url: str | None,
) -> str:
    """Build the prompt for the intent-check agent invocation in the verify phase."""
    route_block = ""
    if affected_routes:
        lines = "\n".join(
            f"- {r.get('method', 'GET')} {r['path']} ({r.get('label', '')})"
            for r in affected_routes
        )
        route_block = f"\n\nAffected routes:\n{lines}"

    server_block = ""
    if server_url:
        server_block = (
            f"\n\nA dev server is running at {server_url}. If the task describes "
            "visual behaviour or UI changes, call `browse_url` on each affected route "
            "to confirm the rendered output matches the description. Use "
            "`tail_dev_server_log` if anything looks wrong."
        )

    return f"""You are the intent verifier. Decide whether the diff below addresses
the task as stated.

Task title: {task_title}
Task description: {task_description}

Diff summary:
{diff_summary}{route_block}{server_block}

Output your verdict on the first line of your reply, exactly one of:
- OK
- NOT-OK: <one-line reason>

Then on subsequent lines, write a short reasoning paragraph (no more than 5 lines)
covering: missing requirements, off-topic changes, partial implementations. If
you used browse_url, mention what you observed.
"""


def augment_coding_prompt_with_server(
    base_prompt: str, *, port: int | None, affected_routes: list[dict],
) -> str:
    """Append a dev-server block to the coding prompt when a server is running.

    Returns ``base_prompt`` unchanged when ``port`` is None or ``affected_routes``
    is empty — used as a passthrough when no server is active.
    """
    if not port or not affected_routes:
        return base_prompt
    route_lines = "\n".join(
        f"- {r.get('method', 'GET')} {r['path']}  ({r.get('label', '')})"
        for r in affected_routes
    )
    block = f"""

## Dev server

A dev server is running at http://localhost:{port}. Affected routes for this task:
{route_lines}

Use the `browse_url` tool to inspect rendered output as you make changes. Most
frameworks hot-reload — re-screenshot after edits to see results. Use
`tail_dev_server_log` if you need to debug server output.
"""
    return base_prompt + block
