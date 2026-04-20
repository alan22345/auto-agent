"""Custom assertion: evaluate architecture decisions and trade-off reasoning.

Checks that the agent:
1. Made meaningful changes (not just cosmetic)
2. Preserved existing tests (didn't break backwards compat)
3. Explained trade-offs in commit messages or code comments
4. Didn't over-engineer (kept changes proportional to the task)
5. Maintained code readability
"""

import json
import re


def get_assert(output, context):
    """Evaluate the architectural quality of changes."""
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return {"pass": False, "score": 0, "reason": "Output is not valid JSON"}

    if "error" in data and data["error"]:
        return {"pass": False, "score": 0, "reason": f"Error: {data['error']}"}

    diff = data.get("diff", "")
    files = data.get("files", {})
    agent_output = data.get("agent_output", "") or data.get("cli_output", "")

    if not diff.strip():
        return {"pass": False, "score": 0.1, "reason": "No changes made"}

    score = 0.0
    reasons = []

    # 1. Meaningful structural changes (not just renaming/formatting)
    structural_indicators = [
        r"class\s+\w+",          # New classes
        r"async\s+def\s+\w+",    # Async functions
        r"def\s+\w+",            # New functions
        r"import\s+",            # New imports
        r"@\w+",                 # Decorators
    ]
    structural_count = 0
    for pattern in structural_indicators:
        if re.search(pattern, diff):
            structural_count += 1
    if structural_count >= 2:
        score += 0.2
        reasons.append(f"Structural changes detected ({structural_count} patterns)")
    elif structural_count >= 1:
        score += 0.1
        reasons.append("Minor structural changes")

    # 2. Test preservation — existing test file should not be deleted or gutted
    test_files_present = any("test_" in f for f in files)
    if test_files_present:
        score += 0.15
        reasons.append("Test files preserved")
    else:
        reasons.append("Warning: no test files found in output")

    # 3. Trade-off reasoning — look for explanatory comments or commit messages
    all_content = diff + agent_output
    tradeoff_indicators = [
        "trade-off", "tradeoff", "trade off",
        "chose", "because", "alternative",
        "approach", "consideration", "constraint",
        "backwards compat", "backward compat",
        "readable", "readability", "maintainable",
        "performance", "complexity",
        "pros", "cons",
    ]
    tradeoff_count = sum(1 for t in tradeoff_indicators if t.lower() in all_content.lower())
    if tradeoff_count >= 3:
        score += 0.25
        reasons.append(f"Strong trade-off reasoning ({tradeoff_count} indicators)")
    elif tradeoff_count >= 1:
        score += 0.1
        reasons.append(f"Some trade-off reasoning ({tradeoff_count} indicators)")
    else:
        reasons.append("No trade-off reasoning detected")

    # 4. Proportionality — changes should be substantial but not excessive
    diff_lines = len(diff.strip().splitlines())
    if 10 < diff_lines < 300:
        score += 0.15
        reasons.append(f"Proportional changes ({diff_lines} diff lines)")
    elif diff_lines >= 300:
        score += 0.05
        reasons.append(f"Possibly over-engineered ({diff_lines} diff lines)")
    elif diff_lines <= 10:
        score += 0.05
        reasons.append(f"Very minimal changes ({diff_lines} diff lines)")

    # 5. Code quality signals
    quality_signals = ["type hint", "docstring", "# ", "\"\"\"", "->"]
    quality_count = sum(1 for s in quality_signals if s in diff)
    if quality_count >= 2:
        score += 0.15
        reasons.append("Good code quality signals")
    elif quality_count >= 1:
        score += 0.1
        reasons.append("Some code quality signals")

    # 6. Didn't introduce anti-patterns
    anti_patterns = [
        "TODO", "FIXME", "HACK", "XXX",
        "pass  #", "# noqa",
        "type: ignore",
    ]
    anti_count = sum(1 for a in anti_patterns if a in diff)
    if anti_count == 0:
        score += 0.1
        reasons.append("No anti-patterns introduced")
    else:
        reasons.append(f"Warning: {anti_count} anti-pattern(s) in diff")

    score = min(1.0, score)
    return {
        "pass": score >= 0.4,
        "score": round(score, 2),
        "reason": "; ".join(reasons),
    }
