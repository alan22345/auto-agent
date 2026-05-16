"""Public-by-convention surface of area_b.

``PublicWidget`` has no underscore prefix → public. ``_private_helper``
has the underscore prefix → private. A cross-area caller reaching the
private helper should be flagged as an ``internal_access`` violation.
"""


class PublicWidget:
    def render(self) -> str:
        return "public"


def _private_helper() -> int:
    return 42
