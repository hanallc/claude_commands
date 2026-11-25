"""Measure producer folders and report each crossing of the size limit."""
from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast
from uuid import uuid4

from scripts.lib import conf
from scripts.lib.atomic_file import write_text_atomic
from scripts.maintenance.schedule import schedule_directory
from scripts.message import send_result
from scripts.production import staff


SCRIPTS = Path(__file__).resolve().parents[1]
BYTES_PER_MB = 1_000_000
RETRY_WINDOW_MINUTES = 365 * 24 * 60
DELIVERY_SECONDS = 5
SEND_SECONDS = 10
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@dataclass(frozen=True)
class LimitCrossing:
    message_key: str
    accepted: bool = False
    attempts: int = 0


@dataclass(frozen=True)
class MeasuredProducerFolder:
    staff_id: staff.StaffId
    path: Path
    size_bytes: int


def load_crossings(path: Path) -> dict[staff.StaffId, LimitCrossing]:
    try:
        saved = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}
    if not isinstance(saved, dict):
        raise ValueError(f"{path}: expected crossings by staff ID")
    crossings: dict[staff.StaffId, LimitCrossing] = {}
    for identity, value in cast(dict[str, object], saved).items():
        if not isinstance(value, dict):
            raise ValueError(f"{path}: invalid crossing for staff ID {identity}")
        fields = cast(dict[str, object], value)
        key, accepted = fields.get("message_key"), fields.get("accepted")
        attempts = fields.get("attempts", 0)
        if (not isinstance(key, str) or not key or not isinstance(accepted, bool)
                or type(attempts) is not int or attempts < 0):
            raise ValueError(f"{path}: invalid crossing for staff ID {identity}")
        crossings[staff.StaffId(identity)] = LimitCrossing(key, accepted, attempts)
    return crossings


def save_crossings(path: Path, crossings: dict[staff.StaffId, LimitCrossing]) -> None:
    saved = {identity: {"message_key": crossing.message_key, "accepted": crossing.accepted,
                        "attempts": crossing.attempts}
             for identity, crossing in crossings.items()}
    write_text_atomic(path, json.dumps(saved, sort_keys=True) + "\n", new_file_mode=0o600)


def _open_measured_directory(parent: int, name: str, observed: os.stat_result) -> int:
    directory = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
    try:
        if not os.path.samestat(observed, os.fstat(directory)):
            raise OSError(f"directory changed during measurement: {name!r}")
    except BaseException:
        os.close(directory)
        raise
    return directory


def _folder_bytes(root_directory: int) -> int:
    total = 0
    stack = [(root_directory, iter(os.listdir(root_directory)))]
    try:
        while stack:
            directory, names = stack[-1]
            name = next(names, None)
            if name is None:
                _ = stack.pop()
                if directory != root_directory:
                    os.close(directory)
                continue
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = _open_measured_directory(directory, name, info)
                try:
                    stack.append((child, iter(os.listdir(child))))
                except BaseException:
                    os.close(child)
                    raise
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
    finally:
        for directory, _ in stack[1:]:
            os.close(directory)
    return total


def _notify(folder: MeasuredProducerFolder, limit_mb: int, crossing: LimitCrossing) -> None:
    whole, fraction = divmod(folder.size_bytes, BYTES_PER_MB)
    amount = f"{whole}.{fraction:06}".rstrip("0").rstrip(".")
    address = f"staff:{folder.staff_id}"
    message = f"The producer folder {str(folder.path)!r} uses {amount} MB. The limit is {limit_mb} MB."
    try:
        result = subprocess.run(
            [str(SCRIPTS / "lib/py"), str(SCRIPTS / "message/send.py"), "--to", address,
             "--from", "agent-notes", "--text", message, "--key", crossing.message_key,
             "--recipient-key", address, "--repeat-minutes", str(RETRY_WINDOW_MINUTES),
             "--timeout", str(DELIVERY_SECONDS)],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, timeout=SEND_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise OSError(f"send.py exceeded its {SEND_SECONDS}-second process limit.") from error
    outcome = send_result.send_outcome(result.returncode, result.stdout)
    if isinstance(outcome, send_result.ConfirmedSendOutcome):
        if outcome.header.outcome in ("sent", "skipped", "queued", "kept"):
            return
        raise OSError(f"send.py {outcome.header.outcome}. {outcome.header.detail}")
    raise OSError(f"send.py did not confirm delivery (exit {result.returncode})")


def _warn(identity: staff.StaffId, folder: Path, error: OSError | ValueError) -> None:
    print(f"Folder size {identity} {str(folder)!r}. {' '.join(str(error).splitlines())}", flush=True)


def _measure_producers(root: Path) -> list[MeasuredProducerFolder]:
    measurements: list[MeasuredProducerFolder] = []
    for producer in staff.active(staff.snapshot(), "producer"):
        if not isinstance(producer.doc, staff.LiveDoc):
            print(f"Folder size {producer.id}. The staff row has no folder.", flush=True)
            continue
        path = producer.doc.path
        if path == root or not path.is_relative_to(root):
            continue
        folder = path
        try:
            components = path.relative_to(root).parts
            if ".." in components:
                raise ValueError("expected a producer folder below the configured root")
            directory = os.open(root, DIRECTORY_FLAGS)
            try:
                for component in components[:-1]:
                    child = os.open(component, DIRECTORY_FLAGS, dir_fd=directory)
                    os.close(directory)
                    directory = child
                name = components[-1]
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISREG(info.st_mode):
                    folder = path.parent
                    if folder == root:
                        continue
                elif stat.S_ISDIR(info.st_mode):
                    child = _open_measured_directory(directory, name, info)
                    os.close(directory)
                    directory = child
                else:
                    raise ValueError("expected a regular document file or folder")
                size = _folder_bytes(directory)
            finally:
                os.close(directory)
        except (OSError, ValueError) as error:
            _warn(producer.id, folder, error)
            continue
        measurements.append(MeasuredProducerFolder(producer.id, folder, size))
    return measurements


def _check_producers(root: Path, limit_mb: int, path: Path) -> None:
    crossings = load_crossings(path)
    previous = crossings.copy()
    notices: list[MeasuredProducerFolder] = []
    for folder in _measure_producers(root):
        identity = folder.staff_id
        if folder.size_bytes <= limit_mb * BYTES_PER_MB:
            _ = crossings.pop(identity, None)
            continue
        if identity not in crossings:
            crossings[identity] = LimitCrossing(f"producer-folder-size:{uuid4().hex}")
        if not crossings[identity].accepted:
            notices.append(folder)
    if crossings != previous:
        save_crossings(path, crossings)

    # Give notices with fewer attempts priority after an interrupted pass.
    notices.sort(key=lambda folder: crossings[folder.staff_id].attempts)
    for folder in notices:
        identity = folder.staff_id
        crossing = crossings[identity]
        try:
            _notify(folder, limit_mb, crossing)
        except OSError as error:
            _warn(identity, folder.path, error)
        else:
            crossing = replace(crossing, accepted=True)
        crossings[identity] = replace(crossing, attempts=crossing.attempts + 1)
        save_crossings(path, crossings)


def run() -> None:
    """Read the current limit and check all recorded producer folders."""
    config = conf.read("staff")
    limit_mb = config.value("producer_folder_limit_mb", conf.positive_whole)
    root = Path(config.text("producer_folder_root")).expanduser().absolute()
    directory = schedule_directory("job")
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "folder-size.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another producer folder size check is running.", flush=True)
            return
        _check_producers(root, limit_mb, directory / "folder-size.json")
