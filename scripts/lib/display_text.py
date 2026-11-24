"""Make outside text safe to print on one line."""
from __future__ import annotations


def display_field(value: str) -> str:
    """Remove control characters and join all whitespace into single spaces."""
    return " ".join("".join(" " if character.isspace() else character
                            for character in value if character.isprintable() or character.isspace()).split())
