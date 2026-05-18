"""Base classes used by the agent_area fixture."""


class Animal:
    def speak(self) -> str:
        return "..."

    def move(self) -> str:
        return "moves"


def helper() -> int:
    return 1
