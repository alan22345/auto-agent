"""NAV-3 scorer: priority gone from inventory.py and every changed caller."""

from __future__ import annotations

import json


def get_assert(output: str, context: dict) -> dict:
    try:
        payload = json.loads(output)
    except (TypeError, ValueError):
        return {"pass": False, "score": 0.0, "reason": "provider output is not JSON"}
    files = payload.get("files", {})
    inventory = files.get("app/domain/inventory.py", "")
    leftovers = [path for path, content in files.items() if "priority" in content]
    ok = inventory != "" and "priority" not in inventory and not leftovers
    return {
        "pass": ok,
        "score": 1.0 if ok else 0.0,
        "reason": f"files still mentioning priority: {leftovers or 'none'}",
    }
