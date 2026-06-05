"""Utilities module — contains both a used and an unused exported function."""


def used_helper() -> str:
    """This function is called from consumer.py — should NOT be flagged."""
    return "used"


def unused_helper() -> str:
    """This function is exported but never called from outside — should be flagged."""
    return "unused"
