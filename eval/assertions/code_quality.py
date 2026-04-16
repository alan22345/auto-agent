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

    # Has a diff (0.3)
    if diff.strip():
        score += 0.3
        reasons.append("Files were modified")

    # Check diff size (0.15)
    diff_lines = len(diff.splitlines())
    if 1 < diff_lines < 500:
        score += 0.15
        reasons.append(f"Reasonable diff size ({diff_lines} lines)")
    elif diff_lines >= 500:
        score += 0.05
        reasons.append(f"Large diff ({diff_lines} lines) - may indicate over-editing")

    # Check tool usage efficiency (0.2)
    tool_calls = data.get("tool_calls", 0)
    if 0 < tool_calls <= 20:
        score += 0.2
        reasons.append(f"Efficient tool usage ({tool_calls} calls)")
    elif 20 < tool_calls <= 40:
        score += 0.1
        reasons.append(f"Moderate tool usage ({tool_calls} calls)")
    elif tool_calls > 40:
        score += 0.05
        reasons.append(f"Excessive tool usage ({tool_calls} calls) - over-exploring")

    # Token efficiency (0.15)
    tokens = data.get("tokens", {})
    total_tokens = tokens.get("input", 0) + tokens.get("output", 0)
    if 0 < total_tokens < 50000:
        score += 0.15
        reasons.append(f"Low token usage: {total_tokens}")
    elif 0 < total_tokens < 100000:
        score += 0.1
        reasons.append(f"Moderate token usage: {total_tokens}")
    elif total_tokens >= 100000:
        score += 0.05
        reasons.append(f"High token usage: {total_tokens}")

    # Timing (0.2) — only for agent provider
    elapsed = data.get("elapsed_seconds", 0)
    if elapsed > 0:
        if elapsed < 60:
            score += 0.2
            reasons.append(f"Fast: {elapsed}s")
        elif elapsed < 120:
            score += 0.1
            reasons.append(f"Moderate speed: {elapsed}s")
        else:
            score += 0.05
            reasons.append(f"Slow: {elapsed}s")
    else:
        # CLI provider or no timing data — don't penalize
        score += 0.1

    return {
        "pass": score >= 0.4,
        "score": score,
        "reason": "; ".join(reasons),
    }
