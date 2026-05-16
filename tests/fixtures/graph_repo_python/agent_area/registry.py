"""Registry-style dynamic dispatch site — Phase 2 must *count* this, not
resolve it. Phase 3 fills the missing edge via LLM gap-fill.

Phase 4 adds a decorated sibling (``decorated_ping_handler``) below the
bare handlers; the parser must descend through ``decorated_definition``
and the registered handler should appear as a graph node with its
decorator captured.
"""


def ping_handler(payload):
    return payload


def pong_handler(payload):
    return payload * 2


HANDLERS = {
    "ping": ping_handler,
    "pong": pong_handler,
}


def register(name):
    def deco(fn):
        HANDLERS[name] = fn
        return fn

    return deco


def dispatch(name, payload):
    # Dynamic dispatch: callee selected at runtime from HANDLERS dict.
    return HANDLERS[name](payload)


@register("decorated_ping")
def decorated_ping_handler(payload):
    return payload
