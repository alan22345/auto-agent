"""Exercises relative imports (``from .. import base``)."""

from ..base import Animal


class Cat(Animal):
    def speak(self) -> str:
        return "meow"
