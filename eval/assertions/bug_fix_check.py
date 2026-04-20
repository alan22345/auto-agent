"""Custom assertion: verify that specific bugs were fixed."""

import json


def get_assert(output, context):
    """Check that the bug fix was applied correctly."""
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return {"pass": False, "score": 0, "reason": "Output is not valid JSON"}

    files = data.get("files", {})
    expected_fixes = context.get("vars", {}).get("expected_fixes", "")

    if isinstance(expected_fixes, str):
        try:
            expected_fixes = json.loads(expected_fixes)
        except json.JSONDecodeError:
            expected_fixes = [expected_fixes] if expected_fixes else []

    if not expected_fixes:
        return {"pass": True, "score": 0.5, "reason": "No specific fixes to check"}

    all_content = "\n".join(files.values())
    fixes_found = 0

    reasons = []
    for fix in expected_fixes:
        if fix in all_content:
            fixes_found += 1
            reasons.append(f"Found: {fix[:60]}")
        else:
            reasons.append(f"Missing: {fix[:60]}")

    score = fixes_found / len(expected_fixes) if expected_fixes else 0

    return {
        "pass": score >= 0.5,
        "score": score,
        "reason": "; ".join(reasons),
    }
