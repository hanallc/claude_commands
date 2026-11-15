"""Measure each configured drive into the Disks page data file.

One measure at a time: a second call while a measure runs exits at once.
Each drive's `du` runs at the lowest CPU and disk priority under a memory cap.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypedDict

from scripts.buildlog import disk, filesystem
from scripts.lib import conf, file_lock
from scripts.lib.atomic_file import write_text_atomic

DATA_FILE = "disks.json"
LOCK_FILE = "disks.lock"
# du exits 1 when it cannot read some folders; the folders it read are still valid.
DU_PARTIAL_EXIT = 1
LOCK_BUSY_EXIT = 3


class Folder(TypedDict):
    name: str
    size: int
    other: int
    children: list[Folder]


class Drive(TypedDict):
    mount: str
    total: NotRequired[int]
    used: NotRequired[int]
    free: NotRequired[int]
    floor: NotRequired[int | None]
    tree: NotRequired[Folder]
    error: NotRequired[str]


class DisksData(TypedDict):
    measured_at: float
    keep_bytes: int
    drives: list[Drive]


@dataclass(frozen=True)
class MeasureSettings:
    drives: tuple[str, ...]
    keep_bytes: int
    memory_max: str
    timeout_seconds: int
    output_folder: Path


def read_settings() -> MeasureSettings:
    settings = conf.read("disks")
    return MeasureSettings(settings.words("drives"), settings.whole("measure.keep_bytes"),
                           settings.text("measure.memory_max"), settings.whole("measure.timeout_seconds"),
                           Path(os.path.expanduser(settings.text("output_folder"))))


Runner = Callable[[Sequence[str], int], subprocess.CompletedProcess[str]]


def run_command(argv: Sequence[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout_seconds, check=False)


def du_command(mount: str, settings: MeasureSettings) -> list[str]:
    """du on one filesystem, at idle disk priority and the lowest CPU priority, inside a memory-capped scope."""
    return ["systemd-run", "--user", "--scope", "--quiet", "-p", f"MemoryMax={settings.memory_max}", "--",
            "nice", "-n", "19", "ionice", "-c3",
            "du", "-x", "-B1", f"--threshold={settings.keep_bytes}", mount]


def parse_du(output: str) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for line in output.splitlines():
        size, separator, path = line.partition("\t")
        if separator and size.isdigit():
            sizes[os.path.normpath(path)] = int(size)
    return sizes


def folder_tree(mount: str, sizes: dict[str, int], keep_bytes: int) -> Folder:
    """Build the folder tree: each folder's parent is its nearest listed ancestor.

    `other` is the folder's size less its listed children: its own files and its folders below the limit.
    """
    root_path = os.path.normpath(mount)
    nodes: dict[str, Folder] = {root_path: {"name": root_path, "size": sizes.get(root_path, 0), "other": 0,
                                            "children": []}}
    for path in sorted((path for path in sizes if path != root_path), key=lambda path: path.count(os.sep)):
        parent = os.path.dirname(path)
        while parent not in nodes and parent != os.path.dirname(parent):
            parent = os.path.dirname(parent)
        if parent not in nodes:
            continue
        node: Folder = {"name": os.path.relpath(path, parent), "size": sizes[path], "other": 0, "children": []}
        nodes[parent]["children"].append(node)
        nodes[path] = node
    root = nodes[root_path]
    if root_path not in sizes:
        root["size"] = sum(child["size"] for child in root["children"])
    finish(root)
    for child in root["children"]:
        collapse(child, keep_bytes)
    return root


def finish(node: Folder) -> None:
    node["children"].sort(key=lambda child: (-child["size"], child["name"]))
    for child in node["children"]:
        finish(child)
    node["other"] = max(0, node["size"] - sum(child["size"] for child in node["children"]))


def collapse(node: Folder, keep_bytes: int) -> None:
    """Join a folder with its only child when the folder's own files are below the limit, as home/natepiano."""
    while len(node["children"]) == 1 and node["other"] < keep_bytes:
        child = node["children"][0]
        node["name"] = f"{node['name']}/{child['name']}"
        node["other"] += child["other"]
        node["children"] = child["children"]
    for child in node["children"]:
        collapse(child, keep_bytes)


def drive_floors(mounts: Sequence[str]) -> dict[int, int | None]:
    """The sweep's free-space floor of each drive's filesystem, by device."""
    from scripts.lint import sweep

    floor = disk.read_floor()
    floors = sweep.filesystem_floors(floor or 0, mounts, lint_settings=conf.read("lint"))
    return {entry.device: None if entry.root_device and floor is None else entry.floor_bytes for entry in floors}


def measure_drive(mount: str, settings: MeasureSettings, floors: dict[int, int | None], runner: Runner) -> Drive:
    drive: Drive = {"mount": mount}
    try:
        usage = filesystem.filesystem_usage(mount)
        device = os.stat(mount).st_dev
    except OSError as error:
        drive["error"] = f"cannot read the drive: {error}"
        return drive
    if isinstance(usage.total, int):
        drive["total"] = usage.total
    drive["used"], drive["free"] = usage.used, usage.free
    drive["floor"] = floors.get(device)
    try:
        result = runner(du_command(mount, settings), settings.timeout_seconds)
    except subprocess.TimeoutExpired:
        drive["error"] = f"the measure stopped after {settings.timeout_seconds} seconds"
        return drive
    except OSError as error:
        drive["error"] = f"the measure did not start: {error}"
        return drive
    sizes = parse_du(result.stdout)
    if result.returncode not in (0, DU_PARTIAL_EXIT) or not sizes:
        last = (result.stderr.strip().splitlines() or ["no output"])[-1]
        drive["error"] = f"du exited {result.returncode}: {last}"
        return drive
    drive["tree"] = folder_tree(mount, sizes, settings.keep_bytes)
    return drive


def measure(settings: MeasureSettings, runner: Runner = run_command,
            floors: Callable[[Sequence[str]], dict[int, int | None]] = drive_floors) -> int:
    settings.output_folder.mkdir(parents=True, exist_ok=True)
    with file_lock.exclusive(settings.output_folder / LOCK_FILE, wait=False) as lock:
        if lock is file_lock.LockResult.BUSY:
            print("a measure is already running")
            return LOCK_BUSY_EXIT
        found = floors(settings.drives)
        data: DisksData = {"measured_at": time.time(), "keep_bytes": settings.keep_bytes,
                           "drives": [measure_drive(mount, settings, found, runner) for mount in settings.drives]}
        write_text_atomic(settings.output_folder / DATA_FILE, json.dumps(data) + "\n", new_file_mode=0o644)
    print(f"measured {len(settings.drives)} drives on {socket.gethostname()}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    _ = argparse.ArgumentParser(description=__doc__).parse_args(argv)
    return measure(read_settings())


if __name__ == "__main__":
    raise SystemExit(main())
