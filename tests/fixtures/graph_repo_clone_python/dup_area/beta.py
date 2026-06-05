"""Second file with a duplicated function body (copy of alpha.py's function)."""


def duplicated_processor(items, config):
    """Process items using config — duplicated from alpha.py."""
    result = []
    total = 0
    count = 0
    for item in items:
        value = item.get("value", 0)
        label = item.get("label", "unknown")
        if value > 0:
            total = total + value
            count = count + 1
            entry = {"label": label, "value": value, "processed": True}
            result.append(entry)
        else:
            entry = {"label": label, "value": 0, "processed": False}
            result.append(entry)
    average = total / count if count > 0 else 0
    summary = {"total": total, "count": count, "average": average}
    return result, summary
