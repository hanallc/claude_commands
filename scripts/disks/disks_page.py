"""Publish the Disks page beside its data file and give its link, or start a measure for it."""
from __future__ import annotations

import argparse
import enum
import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypedDict, cast

from scripts.lib import conf, file_lock
from scripts.lib.atomic_file import write_text_atomic
from scripts.review_links import page_frame, review_link

PAGE_NAME = "disks.html"
DATA_FILE = "disks.json"
LOCK_FILE = "disks.lock"
# The page posts to this name beside it to ask for a measure.
MEASURE_NAME = "measure"
NAME_KEY = "name."
REPOSITORY = Path(__file__).resolve().parents[2]


class MeasureStart(enum.Enum):
    STARTED = "started"
    RUNNING = "running"
    CURRENT = "current"


class PageSettings(TypedDict):
    slider_min_gib: float
    slider_default_gib: float
    cache_seconds: int
    poll_seconds: int
    wait_seconds: int
    names: dict[str, str]


def output_folder() -> Path:
    return Path(os.path.expanduser(conf.read("disks").text("output_folder")))


def page_settings() -> PageSettings:
    """The config values the page reads: the slider, the cache time, the measure wait and each drive's name."""
    settings = conf.read("disks")
    drives = settings.words("drives")
    names = {mount: name for mount in drives if (name := settings.values().get(NAME_KEY + mount))}
    return {"slider_min_gib": settings.number("slider.min_gib"), "slider_default_gib": settings.number("slider.default_gib"),
            "cache_seconds": settings.whole("cache_seconds"), "poll_seconds": settings.whole("page.poll_seconds"),
            "wait_seconds": settings.whole("measure.timeout_seconds") * len(drives), "names": names}


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
    """Publish the page with the current config, and return its URL for config/console.conf."""
    page = publish(output_folder(), page_settings())
    return review_link.snapshot_url(review_link.review_root().resolve(), page.resolve())


def measure_path(root: Path) -> str:
    """The server path that starts a measure: the measure name beside the page."""
    return "/" + (output_folder().resolve() / MEASURE_NAME).relative_to(root.resolve()).as_posix()


def measured_at(folder: Path) -> float | None:
    try:
        data = cast(object, json.loads((folder / DATA_FILE).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    value = cast(dict[str, object], data).get("measured_at") if isinstance(data, dict) else None
    return float(value) if isinstance(value, int | float) else None


def launch_measure() -> None:
    """Start measure.py in its own session; a thread reaps it, so the server keeps no finished child."""
    process = subprocess.Popen([sys.executable, "-m", "scripts.disks.measure"], cwd=REPOSITORY, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    threading.Thread(target=process.wait, name="disks-measure", daemon=True).start()


def start_measure(now: float, launch: Callable[[], None] = launch_measure) -> MeasureStart:
    """Start a measure unless the data is current or a measure runs. The caller never waits for the measure."""
    folder = output_folder()
    last = measured_at(folder)
    if last is not None and now - last < conf.read("disks").whole("cache_seconds"):
        return MeasureStart.CURRENT
    folder.mkdir(parents=True, exist_ok=True)
    with file_lock.exclusive(folder / LOCK_FILE, wait=False) as lock:
        if lock is file_lock.LockResult.BUSY:
            return MeasureStart.RUNNING
    launch()
    return MeasureStart.STARTED


def main(argv: Sequence[str] | None = None) -> int:
    _ = argparse.ArgumentParser(description="Publish the Disks page and print its link.").parse_args(argv)
    print(page_url())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
