"""area_a callers — one clean cross-area edge + one internal-access breach."""

from area_b.public_api import PublicWidget, _private_helper


def use_public() -> str:
    # Clean cross-area edge — PublicWidget is part of area_b's public
    # surface (no underscore prefix).
    return PublicWidget().render()


def use_private() -> int:
    # Internal-access violation — _private_helper is implicitly private.
    return _private_helper()
