"""Serve the placement disk proof of concept as a console page, read from its source file on each request."""
from __future__ import annotations

from pathlib import Path

from scripts.lib import conf
from scripts.review_links import change_page

PAGE_NAME = "placement-disk.html"
PAGE_PATH = f"/{change_page.CONSOLE_FOLDER}/{PAGE_NAME}"


def page_url() -> str:
    """Return the fixed page path for config/console.conf."""
    return PAGE_PATH


def source() -> Path:
    """The page file named by placement_disk.file in config/console.conf."""
    try:
        return Path(conf.read("console").text("placement_disk.file")).expanduser()
    except RuntimeError as error:
        raise ValueError(f"placement_disk.file: {error}") from error


def render() -> bytes:
    """Read the page file now, so an edit shows after a refresh."""
    return source().read_bytes()
