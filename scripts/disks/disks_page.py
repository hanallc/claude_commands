"""Publish the Disks page beside its data file, or print its link."""
from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import TypedDict

from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.review_links import page_frame, review_link

PAGE_NAME = "disks.html"
NAME_KEY = "name."


class PageSettings(TypedDict):
    slider_min_gib: float
    slider_default_gib: float
    cache_seconds: int
    names: dict[str, str]


def output_folder() -> Path:
    return Path(os.path.expanduser(conf.read("disks").text("output_folder")))


def page_settings() -> PageSettings:
    """The config values the page reads: the slider, the cache time and each drive's name."""
    settings = conf.read("disks")
    names = {mount: name for mount in settings.words("drives") if (name := settings.values().get(NAME_KEY + mount))}
    return {"slider_min_gib": settings.number("slider.min_gib"), "slider_default_gib": settings.number("slider.default_gib"),
            "cache_seconds": settings.whole("cache_seconds"), "names": names}


def publish(folder: Path, settings: PageSettings) -> Path:
    source = Path(__file__).with_name(PAGE_NAME).read_text(encoding="utf-8")
    # A '</' in a name must not end the script element.
    page = (source.replace("__PAGE_FRAME_STYLE__", page_frame.STYLE)
            .replace("__DISKS_SETTINGS__", json.dumps(settings).replace("</", "<\\/")))
    folder.mkdir(parents=True, exist_ok=True)
    page_path = folder / PAGE_NAME
    write_text_atomic(page_path, page, new_file_mode=0o644)
    return page_path


def page_url() -> str:
    """Return the page URL without publishing it."""
    page = output_folder().resolve() / PAGE_NAME
    if not page.is_file():
        raise FileNotFoundError(f"the Disks page is not published: {page}")
    return review_link.snapshot_url(review_link.review_root().resolve(), page)


class Arguments(argparse.Namespace):
    command: str = "link"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("command", choices=("publish", "link"))
    if parser.parse_args(argv, namespace=Arguments()).command == "publish":
        _ = publish(output_folder(), page_settings())
    print(page_url())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
