"""Consumer module — imports utils and calls used_helper."""

from used_area.utils import used_helper


def do_work() -> str:
    """Calls used_helper from utils — keeps that function non-dead."""
    return used_helper()
