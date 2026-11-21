"""Update the send keys after a local review release."""
from __future__ import annotations

from scripts.message.review_hold import ReviewMessage


def record_keys(held: tuple[ReviewMessage, ...], fallback_recipient: str, at: float) -> None:
    from scripts.message.send import record_released_acceptances

    record_released_acceptances(held, fallback_recipient, at)
