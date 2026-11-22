"""Measure each configured drive into the Disks page data file.

One measure at a time: a second call while a measure runs exits at once.
Each drive's dust walk runs at the lowest CPU priority under a memory cap.
The drives run at the same time.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePath
from typing import NotRequired, TypedDict, cast

from scripts.buildlog import disk, filesystem
from scripts.disks import disks_page
from scripts.disks.disks_page import DATA_FILE, LOCK_FILE
from scripts.lib import conf, file_lock
from scripts.lib.atomic_file import write_text_atomic

LOCK_BUSY_EXIT = 3
MOUNT_TABLE = Path("/proc/self/mountinfo")
# The mount table writes a space, a tab, a newline and a backslash in a path as an octal escape.
MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


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
    generation: NotRequired[int]
    measured_at: float
    render_seconds: float
    keep_bytes: int
    drives: list[Drive]


@dataclass(frozen=True)
class Mount:
    """One line of the mount table: the file system, its folder that the mount shows, and where it shows."""

    device: str
    root: PurePath
    point: PurePath


def read_mounts(table: Path = MOUNT_TABLE) -> tuple[Mount, ...]:
    """This machine's mounts in mount order, or none where the system has no mount table, as on the Mac."""
    try:
        lines = table.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()

    def path(text: str) -> PurePath:
        return PurePath(MOUNT_ESCAPE.sub(lambda match: chr(int(match[1], 8)), text))

    return tuple(Mount(fields[2], path(fields[3]), path(fields[4])) for fields in map(str.split, lines) if len(fields) > 4)


def drive_path(path: PurePath, mounts: Sequence[Mount]) -> PurePath:
    """Where the measure finds a path. A path under a mount of a subfolder, such as /session on natedev,
    is measured under the mount of its whole file system, such as /home/natepiano/session."""
    # The last mount at a point covers the earlier ones.
    holding = max(((len(mount.point.parts), index, mount) for index, mount in enumerate(mounts)
                   if mount.point == path or mount.point in path.parents), default=None)
    if holding is None or holding[2].root == PurePath("/"):
        return path
    mount = holding[2]
    whole = next((other for other in mounts if other.device == mount.device and other.root == PurePath("/")), None)
    return whole.point / mount.root.relative_to("/") / path.relative_to(mount.point) if whole else path


def tree_size(data: DisksData | None, path: PurePath) -> int | None:
    """A path's measured size: 0 when its drive lists no such folder, None when no measured drive holds it."""
    drives = sorted(data["drives"] if data else [], key=lambda drive: len(drive["mount"]), reverse=True)
    for drive in drives:
        mount = PurePath(drive["mount"])
        if path != mount and mount not in path.parents:
            continue
        node: Folder | None = drive.get("tree")
        if node is None:
            return None
        parts = path.relative_to(mount).parts
        while parts:
            # A chain of single children is one node, such as "home/natepiano".
            match = next((child for child in node["children"]
                          if PurePath(child["name"]).parts[:len(parts)] == parts[:len(PurePath(child["name"]).parts)]), None)
            if match is None:
                return 0
            node, parts = match, parts[len(PurePath(match["name"]).parts):]
        return node["size"]
    return None


def read_data() -> DisksData | None:
    """The last measure, or None before the first measure."""
    try:
        return cast(DisksData, json.loads((disks_page.output_folder() / DATA_FILE).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class MeasureSettings:
    drives: tuple[str, ...]
    keep_bytes: int
    memory_max: str
    timeout_seconds: int
    output_folder: Path
    max_depth: int
    max_lines: int


def read_settings() -> MeasureSettings:
    settings = conf.read("disks")
    return MeasureSettings(settings.words("drives"), settings.whole("measure.keep_bytes"),
                           settings.text("measure.memory_max"), settings.whole("measure.timeout_seconds"),
                           Path(os.path.expanduser(settings.text("output_folder"))),
                           settings.whole("measure.max_depth"), settings.whole("measure.max_lines"))


Runner = Callable[[Sequence[str], int], subprocess.CompletedProcess[str]]


def run_command(argv: Sequence[str], timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    if shutil.which("dust") is None:
        raise FileNotFoundError("dust is not on PATH")
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout_seconds, check=False)


def dust_command(mount: str, settings: MeasureSettings) -> list[str]:
    """Walk one filesystem with dust under a memory cap at the lowest CPU priority."""
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect", "-p", f"MemoryMax={settings.memory_max}", "--",
            "nice", "-n", "19", "dust", "-x", "-D", "-j", "-P", "-o", "b",
            "-z", str(settings.keep_bytes - 1),  # Dust keeps sizes above this value, so include the floor.
            "-d", str(settings.max_depth), "-n", str(settings.max_lines),
            "--config", str(Path(__file__).with_name("dust.toml")), mount]


class DustFolder(TypedDict):
    name: str
    size: str
    children: list[DustFolder]


def parse_dust(output: str) -> dict[str, int]:
    sizes: dict[str, int] = {}
    pending = [cast(DustFolder, json.loads(output))]
    while pending:
        folder = pending.pop()
        sizes[os.path.normpath(folder["name"])] = int(folder["size"].removesuffix("B"))
        pending.extend(folder["children"])
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
        result = runner(dust_command(mount, settings), settings.timeout_seconds)
    except subprocess.TimeoutExpired:
        drive["error"] = f"the measure stopped after {settings.timeout_seconds} seconds"
        return drive
    except OSError as error:
        drive["error"] = f"the measure did not start: {error}"
        return drive
    if result.returncode != 0 or not result.stdout.strip():
        last = (result.stderr.strip().splitlines() or ["no output"])[-1]
        drive["error"] = f"dust exited {result.returncode}: {last}"
        return drive
    sizes = parse_dust(result.stdout)
    drive["tree"] = folder_tree(mount, sizes, settings.keep_bytes)
    return drive


@dataclass(frozen=True)
class MeasureReservation:
    generation: int
    lock: file_lock.ExclusiveLock


class TimerMeasure(Enum):
    REQUEST = "request"


def measure(settings: MeasureSettings, runner: Runner = run_command,
            floors: Callable[[Sequence[str]], dict[int, int | None]] = drive_floors,
            *, reservation: MeasureReservation | TimerMeasure = TimerMeasure.REQUEST) -> int:
    settings.output_folder.mkdir(parents=True, exist_ok=True)
    active = settings.output_folder / disks_page.ACTIVE_FILE
    coordination = settings.output_folder / disks_page.COORD_FILE
    if isinstance(reservation, TimerMeasure):
        with file_lock.exclusive(coordination):
            lock = file_lock.take_exclusive(settings.output_folder / LOCK_FILE)
            if lock is file_lock.LockResult.BUSY:
                print("a measure is already running")
                return LOCK_BUSY_EXIT
            try:
                generation = disks_page.published_generation(settings.output_folder) + 1
                write_text_atomic(active, str(generation) + "\n", new_file_mode=0o644)
            except BaseException:
                lock.release()
                raise
            reservation = MeasureReservation(generation, lock)
    data: DisksData = {"generation": reservation.generation, "measured_at": 0.0, "render_seconds": 0.0,
                       "keep_bytes": settings.keep_bytes, "drives": []}
    complete = False
    try:
        started = time.perf_counter()
        found = floors(settings.drives)
        with ThreadPoolExecutor(max_workers=len(settings.drives)) as pool:
            drives = [pool.submit(measure_drive, mount, settings, found, runner) for mount in settings.drives]
            data["drives"] = [drive.result() for drive in drives]
        data["render_seconds"] = time.perf_counter() - started
        complete = True
    finally:
        with file_lock.exclusive(coordination):
            try:
                if complete:
                    data["measured_at"] = time.time()
                    write_text_atomic(settings.output_folder / DATA_FILE, json.dumps(data) + "\n", new_file_mode=0o644)
            finally:
                try:
                    active.unlink(missing_ok=True)
                finally:
                    reservation.lock.release()
    print(f"measured {len(settings.drives)} drives on {socket.gethostname()}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--generation", type=int, default=0)
    _ = parser.add_argument("--lock-fd", type=int, default=-1)
    args = parser.parse_args(argv, namespace=MeasureArguments())
    if args.generation == 0 and args.lock_fd == -1:
        return measure(read_settings())
    if args.generation <= 0 or args.lock_fd < 0:
        parser.error("--generation and --lock-fd must specify one reservation")
    return measure(read_settings(), reservation=MeasureReservation(args.generation, file_lock.ExclusiveLock(args.lock_fd)))


class MeasureArguments(argparse.Namespace):
    generation: int = 0
    lock_fd: int = -1


if __name__ == "__main__":
    raise SystemExit(main())
