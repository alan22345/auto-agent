"""Custom assertion: check that the agent actually modified files."""

import json


def get_assert(output, context):
    """Verify the agent produced a meaningful diff."""
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return {"pass": False, "score": 0, "reason": "Output is not valid JSON"}

    # Check for errors
    if "error" in data and data["error"]:
        return {"pass": False, "score": 0, "reason": f"Error: {data['error']}"}

    # Check that a diff was produced
    diff = data.get("diff", "")
    if not diff.strip():
        return {"pass": False, "score": 0.1, "reason": "No file changes were made"}

    # Basic scoring
    score = 0.0
    reasons = []

    # Has a diff
    if diff.strip():
        score += 0.4
        reasons.append("Files were modified")

    # Check diff size (not too small, not too large)
    diff_lines = len(diff.splitlines())
    if 1 < diff_lines < 500:
        score += 0.2
        reasons.append(f"Reasonable diff size ({diff_lines} lines)")
    elif diff_lines >= 500:
        score += 0.1
        reasons.append(f"Large diff ({diff_lines} lines) - may indicate over-editing")

    # Check tool usage (agent provider only)
    tool_calls = data.get("tool_calls", 0)
    if tool_calls > 0:
        score += 0.2
        reasons.append(f"Used {tool_calls} tool calls")

    # Check for token efficiency
    tokens = data.get("tokens", {})
    total_tokens = tokens.get("input", 0) + tokens.get("output", 0)
    if 0 < total_tokens < 100000:
        score += 0.2
        reasons.append(f"Token usage: {total_tokens}")

    return {
        "pass": score >= 0.4,
        "score": score,
        "reason": "; ".join(reasons),
    }
