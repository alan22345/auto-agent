"""Entry point that imports first-party siblings by bare leaf name (legacy Flask style)
and also imports a genuinely-undeclared external to verify we still catch real violations.
"""

import models  # bare leaf import of svc/models.py
import totallyfake  # genuinely undeclared external — must still be flagged
from routes import handler  # bare leaf import of svc/routes.py


def main() -> None:
    user = models.User("alice")
    result = handler()
    _ = totallyfake
    print(user.name, result)
