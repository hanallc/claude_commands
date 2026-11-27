"""Measure each output filesystem once for the build report's disk table."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Collection, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from stat import S_ISDIR
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

from scripts.lib import build_folders, conf, copy_record
from scripts.lib.cargo_output import CargoOutputRoots, CargoOutputUnavailable, read_cargo_output_roots
from scripts.lib.user_zone import user_zone

from scripts.buildlog import store
from scripts.buildlog import filesystem

if TYPE_CHECKING:
    from scripts.lint.sweep import InodeKey


class DiskRow(TypedDict):
    label: str
    bytes: int
    within: NotRequired[str]


class OutsideBuildCacheDirectory(TypedDict):
    path: str
    bytes: int
    growth_bytes: int
    largest_child_path: str | None
    largest_child_growth_bytes: int | None
    child_bytes: NotRequired[dict[str, int]]


class OutsideBuildCacheTotal(TypedDict):
    label: str
    bytes: int
    growth_bytes: int


class FilesystemCapacity(TypedDict):
    path: str
    device: str
    device_name: str
    used: int
    free: int


class DiskSnapshot(TypedDict):
    measured_at: str
    capacity_measured_at: NotRequired[str]
    host: str
    rows: list[DiskRow]
    used: int
    free: int
    floor: int | None
    output_unavailable: NotRequired[list[str]]
    filesystems: NotRequired[list[FilesystemCapacity]]
    previous_measured_at: NotRequired[str | None]
    outside_build_caches: NotRequired[list[OutsideBuildCacheDirectory]]
    outside_build_cache_totals: NotRequired[list[OutsideBuildCacheTotal]]


class MeasuredDiskSnapshot(TypedDict):
    measured_at: str
    capacity_measured_at: str
    host: str
    rows: list[DiskRow]
    used: int
    free: int
    floor: int | None
    output_unavailable: NotRequired[list[str]]
    filesystems: NotRequired[list[FilesystemCapacity]]
    previous_measured_at: str | None
    outside_build_caches: list[OutsideBuildCacheDirectory]
    outside_build_cache_totals: list[OutsideBuildCacheTotal]


MIN_OUTSIDE_DIRECTORY_BYTES = 1 << 30


FOLDERS: list[tuple[str, str]] = [
    ("~/rust", "~/rust"),
    ("~/.local/state", "~/.local/state"),
    ("/tmp", "/tmp"),
]


def scanned_folders() -> list[tuple[str, str]]:
    """List fixed folders, Hana CI runners, and the scratch root if separate."""
    folders = list(FOLDERS)
    try:
        runners = build_folders.ci_folders("hana")
    except OSError:
        runners = []
    folders.extend((f"CI runner {number}", str(path)) for number, path in runners)
    scratch = copy_record.root()
    if not any(scratch.is_relative_to(Path(path).expanduser()) for _, path in folders):
        folders.append(("scratch", str(scratch)))
    return folders


@dataclass(frozen=True)
class EarlierOutsideMeasurement:
    measured_at: str
    directory_bytes: dict[str, int]
    child_directory_bytes: dict[str, int]
    folder_bytes: dict[str, int]


@dataclass(frozen=True)
class NoEarlierOutsideMeasurement:
    pass


type PreviousOutsideMeasurement = EarlierOutsideMeasurement | NoEarlierOutsideMeasurement
NO_EARLIER_OUTSIDE_MEASUREMENT = NoEarlierOutsideMeasurement()


class InvalidFloor(ValueError):
    """A configured disk floor that cannot be measured in GiB."""


def read_floor() -> int | None:
    from scripts.lint import sweep

    config = str(conf.path("lint"))
    floor, key = sweep.floor_bytes(conf.read(Path(config)).values(), socket.gethostname())
    if floor is None and key is not None:
        raise InvalidFloor(f"{key} in {config} must be a non-negative number of GiB")
    return floor


def outside_directory_bytes(folders: list[tuple[str, str]]) -> tuple[dict[str, int], dict[str, int]]:
    """Allocated bytes under folder roots and shallow directories, excluding cargo targets."""
    from scripts.lint import sweep

    sizes: dict[str, int] = {}
    depths: dict[str, int] = {}
    seen: set[sweep.InodeKey] = set()
    for _, root in folders:
        frontier: list[tuple[str, int, tuple[str, ...]]] = [(os.path.expanduser(root), 0, ())]
        while frontier:
            directory, depth, parents = frontier.pop()
            if sweep.is_cargo_target(directory):
                continue
            counted = (*parents, directory) if depth in (0, 1, 2, 3) else parents
            if depth in (0, 1, 2, 3):
                _ = sizes.setdefault(directory, 0)
                depths[directory] = depth
            try:
                with os.scandir(directory) as entries:
                    listed = sorted(entries, key=lambda entry: entry.name)
            except OSError:
                continue
            for entry in reversed(listed):
                try:
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    frontier.append((entry.path, depth + 1, counted))
                    continue
                key = (stat.st_dev, stat.st_ino)
                if key in seen:
                    continue
                seen.add(key)
                for parent in counted:
                    sizes[parent] += stat.st_blocks * 512
    return sizes, depths


def previous_outside_measurement(snapshot: DiskSnapshot | None) -> PreviousOutsideMeasurement:
    if snapshot is None:
        return NO_EARLIER_OUTSIDE_MEASUREMENT
    rows = snapshot.get("outside_build_caches")
    totals = snapshot.get("outside_build_cache_totals")
    if not isinstance(rows, list) or not isinstance(totals, list):
        return NO_EARLIER_OUTSIDE_MEASUREMENT
    child_sizes = {path: size for row in rows for path, size in row.get("child_bytes", {}).items()}
    return EarlierOutsideMeasurement(
        snapshot["measured_at"], {row["path"]: row["bytes"] for row in rows}, child_sizes,
        {row["label"]: row["bytes"] for row in totals},
    )


def outside_directories(
    folders: list[tuple[str, str]], previous: PreviousOutsideMeasurement,
) -> tuple[str | None, list[OutsideBuildCacheDirectory], list[OutsideBuildCacheTotal]]:
    earlier = previous.measured_at if isinstance(previous, EarlierOutsideMeasurement) else None
    old_sizes = previous.directory_bytes if isinstance(previous, EarlierOutsideMeasurement) else {}
    old_child_sizes = previous.child_directory_bytes if isinstance(previous, EarlierOutsideMeasurement) else {}
    old_folder_sizes = previous.folder_bytes if isinstance(previous, EarlierOutsideMeasurement) else {}
    sizes, depths = outside_directory_bytes(folders)
    totals: list[OutsideBuildCacheTotal] = [
        {
            "label": label,
            "bytes": sizes.get(os.path.expanduser(path), 0),
            "growth_bytes": sizes.get(os.path.expanduser(path), 0) - old_folder_sizes.get(label, 0),
        }
        for label, path in folders
    ]
    children_by_parent: dict[str, dict[str, int]] = {}
    for path, size in sizes.items():
        children_by_parent.setdefault(str(Path(path).parent), {})[path] = size
    rows: list[OutsideBuildCacheDirectory] = [
        {
            "path": path,
            "bytes": size,
            "growth_bytes": size - old_sizes.get(path, 0),
            "largest_child_path": None,
            "largest_child_growth_bytes": None,
            "child_bytes": children_by_parent.get(path, {}),
        }
        for path, size in sorted(sizes.items())
        if depths[path] in (1, 2) and size >= MIN_OUTSIDE_DIRECTORY_BYTES
    ]
    for row in rows:
        children = row.get("child_bytes", {})
        if children:
            largest = max(children, key=lambda path: (children[path] - old_child_sizes.get(path, 0), path))
            row["largest_child_path"] = largest
            row["largest_child_growth_bytes"] = children[largest] - old_child_sizes.get(largest, 0)
    return earlier, rows, totals


@dataclass(frozen=True)
class _DriveFolder:
    path: Path
    children: tuple[_DriveFolder, ...] = ()


def _child_directories(path: Path, excluded: Collection[str] = ()) -> list[Path]:
    children: list[Path] = []
    with suppress(OSError), os.scandir(path) as entries:
        for entry in entries:
            with suppress(OSError):
                if entry.path not in excluded and entry.is_dir(follow_symlinks=False):
                    children.append(Path(entry.path))
    return sorted(children)


def _has_folder_metadata(path: Path) -> bool:
    for suffix in (".lock", ".lease.json"):
        with suppress(OSError):
            if path.with_name(path.name + suffix).is_file():
                return True
    return False


def _top_level_folder(path: Path, mounts: frozenset[str]) -> _DriveFolder:
    directories = _child_directories(path)
    units = tuple(_DriveFolder(unit, tuple(map(_DriveFolder, _child_directories(unit, mounts)))
                               if unit.name != "main" else ())
                  for unit in directories if str(unit) not in mounts)
    # Only split repositories identified by main, CI, or a unit's folder metadata.
    if any(unit.name in ("main", "ci") for unit in directories) or any(
        _has_folder_metadata(child.path) for unit in units for child in unit.children
    ):
        return _DriveFolder(path, units)
    return _DriveFolder(path)


def _foreign_file_mount_inodes(root: Path, mounts: set[Path]) -> set[InodeKey]:
    inodes: set[InodeKey] = set()
    if mounts:
        device = root.stat().st_dev
        for path in mounts:
            with suppress(OSError):
                entry = path.stat()
                if entry.st_dev != device and not S_ISDIR(entry.st_mode):
                    inodes.add((entry.st_dev, entry.st_ino))
    return inodes


def drive_rows(root: Path, seen: set[InodeKey], fixed_labels: Mapping[Path, str]) -> list[DiskRow]:
    """Measure the build root and list each parent before its children."""
    from scripts.lint import sweep

    mounts: set[Path] = set()
    if sys.platform == "linux":
        mounts.update(mount.point for mount in build_folders.linux_mounts()
                      if mount.point != root and mount.point.is_relative_to(root))
    excluded_mounts = frozenset(map(str, mounts))
    # The block walk uses directory exclusions. Mark foreign mounted files as seen during this walk.
    foreign_inodes = _foreign_file_mount_inodes(root, mounts) - seen
    seen.update(foreign_inodes)
    rows: list[DiskRow] = []

    def walk(folder: _DriveFolder, within: str | None = None) -> int:
        label = fixed_labels.get(folder.path, str(folder.path))
        row: DiskRow = {"label": label, "bytes": 0}
        if within is not None:
            row["within"] = within
        rows.append(row)
        child_bytes = sum(walk(child, label) for child in folder.children)
        excluded = excluded_mounts.union(str(child.path) for child in folder.children)
        row["bytes"] = child_bytes + sweep.directory_blocks(str(folder.path), seen, excluded=excluded)
        return row["bytes"]

    try:
        for path in _child_directories(root, excluded_mounts):
            _ = walk(_top_level_folder(path, excluded_mounts))
    finally:
        seen.difference_update(foreign_inodes)
    return rows


def _folder_rows(folders: list[tuple[str, str]], seen: set[InodeKey], *,
                 drive_root: Path | None = None) -> list[DiskRow]:
    from scripts.lint import sweep

    rows: list[DiskRow] = []
    for label, path in folders:
        expanded = os.path.expanduser(path)
        if drive_root is not None and drive_root.is_relative_to(resolved := Path(expanded).resolve()):
            size = sweep.directory_blocks(str(resolved), seen, excluded=(str(drive_root),))
        else:
            size = sweep.directory_blocks(expanded, seen)
        rows.append({"label": label, "bytes": size})
    return rows


def measure(
    folders: list[tuple[str, str]], usage: Callable[[], filesystem.FilesystemUsage], floor: int | None,
    previous: PreviousOutsideMeasurement = NO_EARLIER_OUTSIDE_MEASUREMENT,
) -> MeasuredDiskSnapshot:
    return _measured_snapshot(_folder_rows(folders, set()), folders, usage, floor, previous)


def _measured_snapshot(
    rows: list[DiskRow], folders: list[tuple[str, str]], usage: Callable[[], filesystem.FilesystemUsage],
    floor: int | None, previous: PreviousOutsideMeasurement,
) -> MeasuredDiskSnapshot:
    previous_measured_at, outside_build_caches, outside_build_cache_totals = outside_directories(folders, previous)
    current_usage = usage()
    measured_at = store.utc_iso(time.time())
    return {
        "measured_at": measured_at,
        "capacity_measured_at": measured_at,
        "host": store.host_name(),
        "rows": rows,
        "used": current_usage.used,
        "free": current_usage.free,
        "floor": floor,
        "previous_measured_at": previous_measured_at,
        "outside_build_caches": outside_build_caches,
        "outside_build_cache_totals": outside_build_cache_totals,
    }


def capacity_rows(paths: list[Path], measured: tuple[tuple[int, filesystem.FilesystemUsage], ...] = ()) -> list[FilesystemCapacity]:
    rows: list[FilesystemCapacity] = []
    seen: set[str] = set()
    for path in paths:
        existing = path.expanduser().resolve()
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        try:
            usage = dict(measured).get(existing.stat().st_dev)
            if usage is None:
                usage = filesystem.filesystem_usage(str(existing))
        except OSError:
            continue
        identity = usage.device if usage.device != "unavailable" else str(existing.stat().st_dev)
        if identity in seen:
            continue
        seen.add(identity)
        rows.append({"path": str(existing), "device": identity, "device_name": usage.device_name,
                     "used": usage.used, "free": usage.free})
    return rows


UNREADABLE_FILE = "cargo-output-unreadable.json"


@dataclass(frozen=True)
class UnreadableCheckout:
    """A checkout Cargo could not read, keyed by its manifest's change time and size."""

    manifest_mtime_ns: int
    manifest_size: int
    reason: str


def manifest_key(checkout: Path) -> tuple[int, int] | None:
    try:
        status = (checkout / "Cargo.toml").stat()
    except OSError:
        return None
    return status.st_mtime_ns, status.st_size


def unreadable_path() -> Path:
    from scripts.lint import sweep

    return sweep.cargo_output_cache_path().with_name(UNREADABLE_FILE)


def saved_unreadable() -> dict[Path, UnreadableCheckout]:
    try:
        rows = cast(object, json.loads(unreadable_path().read_text()))
    except (OSError, ValueError):
        return {}
    saved: dict[Path, UnreadableCheckout] = {}
    for path, value in (cast(dict[str, object], rows).items() if isinstance(rows, dict) else ()):
        if not isinstance(value, dict):
            continue
        fields = cast(dict[str, object], value)
        mtime, size, reason = fields.get("manifest_mtime_ns"), fields.get("manifest_size"), fields.get("reason")
        if isinstance(mtime, int) and isinstance(size, int) and isinstance(reason, str):
            saved[Path(path)] = UnreadableCheckout(mtime, size, reason)
    return saved


def save_unreadable(rows: dict[Path, UnreadableCheckout]) -> None:
    from scripts.lib.atomic_file import write_text_atomic

    path = unreadable_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps({str(checkout): {
        "manifest_mtime_ns": row.manifest_mtime_ns, "manifest_size": row.manifest_size, "reason": row.reason,
    } for checkout, row in sorted(rows.items())}, indent=1) + "\n")


def mapped_output_paths(folders: list[tuple[str, str]]) -> tuple[list[Path], list[str]]:
    """Use the saved output roots, and ask Cargo only for checkouts that no saved row maps.

    A checkout that Cargo could not read is read again only after its manifest changes.
    """
    from scripts.lint import sweep

    saved = sweep.saved_cargo_output_roots()
    mappings = list((*saved.workspaces, *saved.history))
    checkouts: list[str] = []
    _ = sweep.target_dirs([path for _, path in folders], checkouts=checkouts)
    mapped = {item.workspace_root.resolve() for item in saved.workspaces}
    found: list[CargoOutputRoots] = []
    unavailable: list[str] = []
    before = saved_unreadable()
    unreadable: dict[Path, UnreadableCheckout] = {}
    # A member crate sorts after its workspace, so one read covers the workspace.
    for checkout in sorted({Path(path).resolve() for path in checkouts}):
        if checkout in mapped or not mapped.isdisjoint(checkout.parents):
            continue
        key = manifest_key(checkout)
        earlier = before.get(checkout)
        if earlier is not None and key == (earlier.manifest_mtime_ns, earlier.manifest_size):
            unreadable[checkout] = earlier
            unavailable.append(f"{checkout}: {earlier.reason}")
            continue
        roots = read_cargo_output_roots(checkout)
        if isinstance(roots, CargoOutputUnavailable):
            unavailable.append(f"{checkout}: {roots.reason}")
            if key is not None:
                unreadable[checkout] = UnreadableCheckout(*key, roots.reason)
        else:
            found.append(roots)
            mapped.add(roots.workspace_root)
    if found:
        # The next walk reads these rows and skips the Cargo reads.
        sweep.save_cargo_output_roots(sweep.CargoOutputIndex(tuple(found)))
    if unreadable != before:
        save_unreadable(unreadable)
    return sorted({path for roots in (*mappings, *found)
                   for path in (roots.target_directory, roots.build_directory)}), unavailable


def _lower_walk_priority() -> None:
    with suppress(OSError):
        os.setpriority(os.PRIO_PROCESS, 0, 19)
    if sys.platform == "linux":
        with suppress(OSError):
            _ = subprocess.run(["ionice", "-c3", "-p", str(os.getpid())], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def snapshot() -> MeasuredDiskSnapshot:
    previous_snapshot = read_snapshot()
    floor = read_floor()
    settings = conf.read("buildlog")
    walk_minutes = settings.whole("disk_walk_minutes")
    walk_hour = settings.whole("disk_walk_hour")
    if (previous_snapshot is not None and "filesystems" in previous_snapshot
            and "outside_build_caches" in previous_snapshot and "outside_build_cache_totals" in previous_snapshot
            and "previous_measured_at" in previous_snapshot):
        now = datetime.now(user_zone())
        request = store.read_disk_walk_request()
        requested = (not isinstance(request, store.DiskWalkRequestState)
                     and datetime.fromisoformat(request["requested_at"]) > datetime.fromisoformat(previous_snapshot["measured_at"]))
        if not requested and (now.timestamp() - datetime.fromisoformat(previous_snapshot["measured_at"]).timestamp() < walk_minutes * 60
                              or now.hour != walk_hour):
            usage = filesystem.filesystem_usage("/")
            capacities: list[FilesystemCapacity] = []
            for row in previous_snapshot["filesystems"]:
                try:
                    current = filesystem.filesystem_usage(row["path"])
                except OSError:
                    capacities.append(row)
                else:
                    capacities.append({**row, "used": current.used, "free": current.free})
            return {
                **previous_snapshot, "used": usage.used, "free": usage.free, "floor": floor,
                "filesystems": capacities, "capacity_measured_at": store.utc_iso(now.timestamp()),
                "outside_build_caches": previous_snapshot["outside_build_caches"],
                "outside_build_cache_totals": previous_snapshot["outside_build_cache_totals"],
                "previous_measured_at": previous_snapshot["previous_measured_at"],
            }
    _lower_walk_priority()
    previous = previous_outside_measurement(previous_snapshot)
    root = build_folders.build_root().resolve()
    folders = scanned_folders()
    fixed_labels = {Path(path).expanduser().resolve(): label for label, path in folders}
    seen: set[InodeKey] = set()
    drive = drive_rows(root, seen, fixed_labels)
    external = [(label, path) for label, path in folders
                if not Path(path).expanduser().resolve().is_relative_to(root)]
    cache_report_roots = set(fixed_labels)
    external_roots = cache_report_roots.copy()
    outputs, unavailable = mapped_output_paths(folders)
    capacity_paths = [Path("/")]
    if root.is_dir():
        capacity_paths.append(root)
    capacity_paths.extend(Path(path) for _, path in folders)
    for output in outputs:
        resolved = output.resolve()
        exists = False
        with suppress(OSError):
            exists = resolved.is_dir()
        if exists:
            capacity_paths.append(resolved)
        # The outside-cache report keeps the saved mapping as an input.
        if output not in cache_report_roots and cache_report_roots.isdisjoint(output.parents):
            folders.append((str(output), str(output)))
            cache_report_roots.add(resolved)
        if (exists and not resolved.is_relative_to(root)
                and resolved not in external_roots and external_roots.isdisjoint(resolved.parents)):
            external.append((str(output), str(resolved)))
            external_roots.add(resolved)
    cache = os.environ.get("SCCACHE_DIR")
    if cache and not any(Path(cache).expanduser().is_relative_to(Path(path).expanduser()) for _, path in folders):
        folders.append(("sccache", cache))
        capacity_paths.append(Path(cache))
        if not Path(cache).expanduser().resolve().is_relative_to(root):
            external.append(("sccache", cache))
    measured: list[tuple[int, filesystem.FilesystemUsage]] = []

    def root_usage() -> filesystem.FilesystemUsage:
        usage = filesystem.filesystem_usage("/")
        measured.append((Path("/").stat().st_dev, usage))
        return usage

    result = _measured_snapshot([*drive, *_folder_rows(external, seen, drive_root=root)],
                                folders, root_usage, floor, previous)
    with suppress(OSError):
        device = root.stat().st_dev
        if device != root.parent.stat().st_dev:
            usage = filesystem.filesystem_usage(str(root))
            measured.append((device, usage))
            named_bytes = sum(row["bytes"] for row in drive if "within" not in row)
            result["rows"].insert(len(drive), {"label": f"{root} other", "bytes": max(0, usage.used - named_bytes)})
    result["filesystems"] = capacity_rows(capacity_paths, tuple(measured))
    result["output_unavailable"] = unavailable
    return result


def write_snapshot(snapshot: DiskSnapshot | MeasuredDiskSnapshot) -> None:
    path = store.root() / store.DISK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")
    _ = partial.write_text(json.dumps(snapshot) + "\n")
    _ = partial.replace(path)


def read_snapshot() -> DiskSnapshot | None:
    try:
        return cast(DiskSnapshot, json.loads((store.root() / store.DISK_NAME).read_text()))
    except (OSError, ValueError):
        return None
