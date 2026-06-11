"""NAV-2 scorer: the gold-tier fix must land in pricing.py, not call sites."""

from __future__ import annotations

import json


def get_assert(output: str, context: dict) -> dict:
    try:
        payload = json.loads(output)
    except (TypeError, ValueError):
        return {"pass": False, "score": 0.0, "reason": "provider output is not JSON"}
    pricing = payload.get("files", {}).get("app/domain/pricing.py", "")
    diff = payload.get("diff", "")
    fixed_in_pricing = '"gold"' in pricing or "'gold'" in pricing or ".lower()" in pricing
    touched_pricing = "app/domain/pricing.py" in diff
    ok = fixed_in_pricing and touched_pricing
    return {
        "pass": ok,
        "score": 1.0 if ok else 0.0,
        "reason": f"root cause fixed in pricing.py: {fixed_in_pricing}",
    }
