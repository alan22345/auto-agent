"""Router — calls into agent_area to exercise cross-area edges."""

from agent_area.dog import make_dog


def run_route():
    dog = make_dog()
    return dog.describe()
