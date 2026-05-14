"""Synthetic provider for Phase-13 fixtures — ADR-015 §16 / Phase 13.

The four Phase-13 fixtures (``stub-introduction-blocked``,
``design-approval-required-before-dispatch``,
``sub-architect-spawn-parent-answers-grill``,
``freeform-standin-decision-logged``) probe failure modes the
in-process ``agent_provider.py`` cannot exercise today — they need the
full orchestrator state machine, Postgres for ``gate_decisions``, Redis
for the ``standin.decision`` event, and the trio dispatcher. Running
that stack inside ``promptfoo eval`` is out of scope for the eval
runner (it would require booting docker-compose mid-test).

This provider gives the eval a runnable shape NOW: it reads the
fixture's ``synthetic_output.json`` and returns it as the provider
output. The assertion modules then exercise the production validators
against that synthetic payload, so the assertion logic is still
deletion-test-bound to the ADR-015 enforcement code paths.

When the fuller integration harness lands later, this provider becomes
the fallback for the four fixtures and the real ``agent_provider.py``
takes over — without any rewiring on the assertion side.

The provider keys off the ``synthetic_output_filename`` var declared in
``promptfooconfig.yaml`` (defaults to ``synthetic_output.json``) so the
"pass" and "fail" cases can be exercised by flipping one variable.
"""

from __future__ import annotations

import json
import os

# Anchor relative to the eval root so promptfoo's working directory
# doesn't matter.
_EVAL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def call_api(prompt, options, context):  # pragma: no cover — promptfoo entry point
    variables = context.get("vars", {}) if context else {}
    fixture = variables.get("fixture") or ""
    filename = variables.get("synthetic_output_filename") or "synthetic_output.json"

    if not fixture:
        return {
            "output": json.dumps({"error": "synthetic_provider: missing `fixture` var"}),
            "error": "missing fixture var",
        }

    path = os.path.join(_EVAL_ROOT, "fixtures", fixture, filename)
    try:
        with open(path) as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        return {
            "output": json.dumps(
                {"error": f"synthetic_provider: missing {filename} in fixture {fixture!r}"}
            ),
            "error": "fixture file missing",
        }
    except json.JSONDecodeError as exc:
        return {
            "output": json.dumps({"error": f"synthetic_provider: bad JSON in {path}: {exc}"}),
            "error": "fixture JSON malformed",
        }

    # Return the fixture payload verbatim — the assertion modules
    # interpret it.
    return {"output": json.dumps(payload)}
