"""Supply private registered images for review write fixtures."""
from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from unittest import mock

from scripts.production import change_review, change_routing
from scripts.production.change_review import Visible
from scripts.review_links import image_store


def registered_fixture_image(root: Path, identity: str = "") -> str:
    """Register an image under an explicit private root."""
    if identity and image_store.change_image(root, identity):
        return identity
    root.mkdir(parents=True, exist_ok=True)
    image = root / "fixture.png"
    _ = image.write_bytes(b"private review image")
    if identity:
        with mock.patch("secrets.token_hex", return_value=identity):
            return image_store.register_file(image, root / image_store.CHANGE_FOLDER)["id"]
    return image_store.register_file(image, root / image_store.CHANGE_FOLDER)["id"]


def fixture_image() -> str:
    """Register a real image under the private test root."""
    if not os.environ.get("REVIEW_LINKS_ROOT"):
        raise ValueError("The fixture requires a private REVIEW_LINKS_ROOT")
    return registered_fixture_image(change_routing.review_root())


def pictured_changes(changes: Sequence[Visible | change_review.PublicationChange]) -> list[Visible | change_review.PublicationChange]:
    """Supply private images for visible changes in write fixtures."""
    result: list[Visible | change_review.PublicationChange] = []
    for change in changes:
        item = change.item if isinstance(change, change_review.PublicationChange) else change
        pictured = item if item.image else replace(item, image=fixture_image())
        result.append(replace(change, item=pictured) if isinstance(change, change_review.PublicationChange) else pictured)
    return result


def pictured_publication(change: change_review.PublicationChange) -> change_review.PublicationChange:
    """Supply an image before a fixture shares a publication change."""
    return change if change.item.image else replace(change, item=replace(change.item, image=fixture_image()))


def fixture_row_image(path: Path, row: str) -> str:
    """Keep a fixture row's image or supply a private image."""
    try:
        existing = next((item.get(change_review.IMAGE, "") for item in change_review.review_rows(path)
                         if item["#"] == row), "")
    except (change_review.ChangeReviewError, OSError):
        existing = ""
    return existing or fixture_image()


def pictured_review_lines(message: str) -> str:
    """Supply private images for valid trailer lines in write fixtures."""
    lines: list[str] = []
    for line in message.split("\n"):
        if line.startswith("Change-Review:"):
            item = change_review.parse(line.partition(":")[2])
            if isinstance(item, Visible) and not item.image:
                line += f" | image {fixture_image()}"
        lines.append(line)
    return "\n".join(lines)


