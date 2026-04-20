"""Custom assertion: verify the cross-file bug was fixed at the root cause."""

import json


def get_assert(output, context):
    """Check that the root cause in pricing.py was fixed, not a bandaid in orders.py."""
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return {"pass": False, "score": 0, "reason": "Output is not valid JSON"}

    files = data.get("files", {})
    reasons = []
    score = 0.0

    # The fix MUST be in pricing.py (root cause)
    pricing = files.get("pricing.py", "")
    if not pricing:
        return {"pass": False, "score": 0.1, "reason": "pricing.py not found in output"}

    # Check that calculate_discount now returns the discount amount, not the discounted price
    # Correct: subtotal * (discount_percent / 100) or subtotal * discount_percent / 100
    if "discount_percent / 100" in pricing and "(1 -" not in pricing.replace(" ", ""):
        score += 0.5
        reasons.append("Root cause fixed: calculate_discount returns discount amount")
    elif "discount_percent / 100" in pricing:
        score += 0.2
        reasons.append("pricing.py modified but old pattern may remain")
    else:
        reasons.append("calculate_discount still looks wrong")

    # The fix should NOT add workarounds in orders.py
    orders = files.get("orders.py", "")
    if orders:
        # If orders.py was changed substantially, it may be a bandaid
        diff = data.get("diff", "")
        orders_changes = diff.count("orders.py")
        if orders_changes > 2:
            score -= 0.1
            reasons.append("Warning: orders.py was modified — possible bandaid fix")

    # Bonus: tests should still be present and unmodified
    test_file = files.get("test_orders.py", "")
    if test_file and "def test_order_with_discount" in test_file:
        score += 0.3
        reasons.append("Tests preserved")

    score = max(0.0, min(1.0, score))
    return {
        "pass": score >= 0.5,
        "score": score,
        "reason": "; ".join(reasons),
    }
