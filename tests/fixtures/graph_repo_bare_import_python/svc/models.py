"""First-party models module — imported by bare leaf name in app.py."""


class User:
    def __init__(self, name: str) -> None:
        self.name = name
