"""Money formatting helpers. All amounts are integer cents."""


def format_cents(cents: int) -> str:
    """1234 -> '$12.34'; negative amounts keep the sign before the $."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100}.{cents % 100:02d}"


def split_evenly(total_cents: int, parts: int) -> list[int]:
    base = total_cents // parts
    remainder = total_cents - base * parts
    return [base + (1 if i < remainder else 0) for i in range(parts)]
