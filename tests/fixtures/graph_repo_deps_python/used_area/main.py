"""Source file that imports:
- used_pkg  (declared, imported -> must NOT appear in any dead_code finding)
- undeclared_pkg  (NOT declared in pyproject.toml -> undeclared_dependency)
- os  (stdlib -> must NOT be flagged)
- first_party_area  (first-party -> must NOT be flagged)
unused_pkg is declared but never imported -> unused_dependency.
"""

import os
import used_pkg
import undeclared_pkg
from first_party_area import helper


def do_work() -> str:
    """Uses the imported packages to keep the imports live in the AST."""
    _ = os.getcwd()
    _ = used_pkg
    _ = undeclared_pkg
    _ = helper
    return "done"
