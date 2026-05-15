"""Registry-style dynamic dispatch site — Phase 2 must *count* this, not
resolve it. Phase 3 will fill the missing edge via LLM gap-fill.
"""

HANDLERS = {}


def register(name):
    def deco(fn):
        HANDLERS[name] = fn
        return fn

    return deco


def dispatch(name, payload):
    # Dynamic dispatch: callee selected at runtime from HANDLERS dict.
    return HANDLERS[name](payload)


@register("ping")
def _ping(payload):
    return payload
