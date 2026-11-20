"""Register a Mac build root under the record lock."""

import os
from pathlib import Path
import re
import sys

from scripts.lib.file_lock import exclusive
from scripts.mac_test.mac_gate_records import (
    LiveMacProcess, MacGateRecord, MacProcessEvidence, MacProcessIdentity, mac_gate_state,
    observe_mac_process, pid_has_ended, read_mac_gate_record,
)


JOBSERVER_PREFIX = "jobserver-"
LEGACY_NAME = re.compile(r"[1-9][0-9]*")
OWNER_NAME = re.compile(r"([1-9][0-9]*)-([1-9][0-9]*)")


class RootRefusal(Exception):
    """The gate cannot confirm that this root can start."""


def owner_evidence(identity: MacProcessIdentity) -> LiveMacProcess | MacProcessEvidence:
    observed = observe_mac_process(identity.pid)
    if isinstance(observed, LiveMacProcess) and observed.identity != identity:
        return MacProcessEvidence.ENDED
    return observed


def inherits_root(process: LiveMacProcess, root: MacGateRecord) -> bool:
    seen: set[int] = set()
    while process.identity.pid not in seen:
        if process.identity == root.identity:
            return True
        if process.parent_pid <= 0:
            return False
        seen.add(process.identity.pid)
        parent = observe_mac_process(process.parent_pid)
        if not isinstance(parent, LiveMacProcess) or parent.identity.started_us > process.identity.started_us:
            return False
        process = parent
    return False


def check_existing_roots(state: Path, own_record: Path, label: str) -> None:
    try:
        # The gate publishes a root record before it creates the jobserver pipe.
        with os.scandir(state) as state_entries:
            pipe_paths = {entry.name.removeprefix(JOBSERVER_PREFIX): Path(entry.path)
                          for entry in state_entries if entry.name.startswith(JOBSERVER_PREFIX)}
        with os.scandir(own_record.parent) as records:
            record_paths = {entry.name: Path(entry.path) for entry in records if entry.name != own_record.name}
    except OSError as error:
        raise RootRefusal("cannot enumerate the build root records and jobserver names") from error

    ended: list[Path] = []
    for name in record_paths.keys() | pipe_paths.keys():
        owner = OWNER_NAME.fullmatch(name)
        if owner is None:
            if name in pipe_paths:
                raise RootRefusal("cannot verify an existing jobserver owner")
            continue
        pid, stamp = owner.groups()
        evidence = owner_evidence(MacProcessIdentity(int(pid), int(stamp)))
        if evidence is MacProcessEvidence.ENDED:
            ended.extend(paths[name] for paths in (record_paths, pipe_paths) if name in paths)
            continue
        if name not in record_paths:
            raise RootRefusal("cannot verify an existing jobserver owner")
        record = read_mac_gate_record(record_paths[name])
        if evidence is MacProcessEvidence.UNAVAILABLE or not isinstance(record, MacGateRecord):
            raise RootRefusal("cannot verify an existing build root")
        if record.label == label:
            raise RootRefusal("placement already has a live build root")
    for name, path in record_paths.items():
        if LEGACY_NAME.fullmatch(name) and pid_has_ended(int(name)):
            ended.append(path)
    for path in ended:
        path.unlink(missing_ok=True)


def register(pid: int, ancestor: str, label: str) -> None:
    state = mac_gate_state()
    if not state.is_absolute() or any(character in str(state) for character in "\t\r\n"):
        raise RootRefusal("cannot use the Mac account state directory")
    records = state / "records"
    state.mkdir(parents=True, exist_ok=True)
    # All record writes and removals hold this lock.
    # The helper holds it only for kernel probes and small file operations.
    with exclusive(state / "registration.lock"):
        records.mkdir(exist_ok=True)
        process = observe_mac_process(pid)
        if not isinstance(process, LiveMacProcess):
            raise RootRefusal("cannot verify this build root")
        key, role = process.identity.key, "root"
        if OWNER_NAME.fullmatch(ancestor):
            root = read_mac_gate_record(records / ancestor)
            if isinstance(root, MacGateRecord) and root.label == label and inherits_root(process, root):
                key, role = ancestor, "nested"
        if role == "root":
            own = MacGateRecord(process.identity, label)
            path = records / key
            stream = path.open("x")
            try:
                with stream:
                    _ = stream.write(own.line())
                if label != "user":
                    check_existing_roots(state, path, label)
            except (OSError, RootRefusal):
                path.unlink(missing_ok=True)
                raise
    print(state, key, role, sep="\t")


def main() -> int:
    _ = os.umask(0o077)
    try:
        register(int(sys.argv[1]), sys.argv[2], sys.argv[3])
    except (OSError, KeyError, RootRefusal) as error:
        print(error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
