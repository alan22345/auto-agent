"""A completely unique function that shares no token run with the duplicated functions."""


def unique_helper(data):
    """Unique logic not shared with any other function."""
    output = sorted(data, key=lambda x: x)
    return output
