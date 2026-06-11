"""Tests for the dedup-trap scorer (eval/assertions/dedup_trap.py, ADR-025)."""

from __future__ import annotations

import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "eval", "assertions"))

from dedup_trap import get_assert  # noqa: E402

_CONTEXT = {
    "vars": {
        "trap_helper": "retry_with_backoff",
        "trap_module": "app/utils/retry.py",
        "trap_dup_pattern": r"def\s+\w*(retry|backoff)\w*\s*\(",
    }
}


def _output(diff: str, files: dict | None = None) -> str:
    return json.dumps({"diff": diff, "files": files or {}})


def test_reuse_without_duplicate_scores_full():
    diff = (
        "+from app.utils.retry import retry_with_backoff\n"
        "+    charge_id = retry_with_backoff(_attempt, attempts=3, retry_on=(TransientPaymentError,))\n"
    )
    verdict = get_assert(_output(diff), _CONTEXT)
    assert verdict["pass"] is True
    assert verdict["score"] == 1.0


def test_duplicate_definition_scores_zero():
    diff = (
        "+def _retry(fn, attempts=3):\n"
        "+    for attempt in range(attempts):\n"
        "+        try:\n"
        "+            return fn()\n"
        "+        except TransientPaymentError:\n"
        "+            time.sleep(0.1 * 2 ** attempt)\n"
    )
    verdict = get_assert(_output(diff), _CONTEXT)
    assert verdict["pass"] is False
    assert verdict["score"] == 0.0


def test_reuse_plus_duplicate_scores_partial():
    diff = (
        "+from app.utils.retry import retry_with_backoff\n"
        "+def charge_with_backoff(fn):\n"
        "+    return retry_with_backoff(fn)\n"
    )
    verdict = get_assert(_output(diff), _CONTEXT)
    assert verdict["pass"] is False
    assert verdict["score"] == 0.4


def test_inlined_logic_scores_low():
    diff = (
        "+    for attempt in range(3):\n"
        "+        try:\n"
        "+            return gateway.charge(order.customer.id, amount)\n"
        "+        except TransientPaymentError:\n"
        "+            time.sleep(0.1)\n"
    )
    verdict = get_assert(_output(diff), _CONTEXT)
    assert verdict["pass"] is False
    assert verdict["score"] == 0.2


def test_helper_definition_in_own_module_is_not_reuse():
    """The helper appearing only in changed_files for its own module
    (e.g. agent touched utils/retry.py) doesn't count as reuse."""
    files = {"app/utils/retry.py": "def retry_with_backoff(fn):\n    ..."}
    verdict = get_assert(_output("", files), _CONTEXT)
    assert verdict["score"] == 0.2


def test_non_json_output_fails_cleanly():
    verdict = get_assert("not json", _CONTEXT)
    assert verdict["pass"] is False
    assert "JSON" in verdict["reason"]
