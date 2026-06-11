"""Small text helpers."""

import re


def slugify(text: str) -> str:
    """Lowercase, strip non-alphanumerics, hyphen-join: 'Blue Mug!' -> 'blue-mug'."""
    cleaned = re.sub(r"[^a-z0-9]+", "-", text.lower())
    return cleaned.strip("-")


def truncate_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + "..."
