"""Dog — exercises inherits + statically-resolvable self-call + cross-module call."""

from agent_area.base import Animal, helper


class Dog(Animal):
    def speak(self) -> str:
        return "woof"

    def describe(self) -> str:
        # self.speak() -> Dog.speak (statically resolvable, same class)
        sound = self.speak()
        # helper() -> agent_area.base.helper (statically resolvable, imported)
        n = helper()
        return f"{sound}:{n}:{sound}"


def make_dog() -> Dog:
    # Dog() is a class-instantiation call — resolved to Dog (constructor).
    return Dog()
