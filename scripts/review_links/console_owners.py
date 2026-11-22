"""Validate owner settings for console pages."""
from __future__ import annotations

from collections.abc import Mapping

from scripts.production import staff
from scripts.review_links import page_frame


def owner_config_problems(settings: Mapping[str, str]) -> tuple[str, ...]:
    """Check the staff IDs for the console pages and the default owner."""
    default_key = "owner.default"
    review_key = "page.review.owner"
    problems: list[str] = []
    for key in (default_key, *(f"page.{page}.owner" for page in sorted(page_frame.OWNER_PAGES))):
        try:
            _ = staff.native_id(settings.get(key, ""))
        except staff.StaffRefused:
            problems.append(f"{key}: Expected a staff UUID.")
    if settings.get(default_key) != settings.get(review_key):
        problems.append(f"{default_key}: Use the same staff ID as {review_key}.")
    return tuple(problems)
