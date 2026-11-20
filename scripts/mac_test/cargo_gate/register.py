"""Publish a Mac build root before checking for overlapping roots."""

import os
from pathlib import Path
import re
import sys

from scripts.mac_test.mac_gate_records import (
    LiveMacProcess, MacGateRecord, MacProcessEvidence, mac_gate_state, observe_mac_process, read_mac_gate_record,
)


JOBSERVER_PREFIX = "jobserver-"
OWNER_NAME = re.compile(r"([1-9][0-9]*)(?:-([A-Za-z0-9_]+))?")


class RootRefusal(Exception):
    """The gate cannot confirm that this root can start."""


def owner_evidence(name: str) -> LiveMacProcess | MacProcessEvidence:
    owner = OWNER_NAME.fullmatch(name)
    if owner is None:
        return MacProcessEvidence.UNAVAILABLE
    pid, stamp = owner.groups()
    observed = observe_mac_process(int(pid))
    if isinstance(observed, LiveMacProcess):
        if stamp is None or not stamp.isdecimal():
            return MacProcessEvidence.UNAVAILABLE
        if observed.identity.key != name:
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
    ended: list[Path] = []
    for path in own_record.parent.iterdir():
        if path == own_record or OWNER_NAME.fullmatch(path.name) is None:
            continue
        evidence = owner_evidence(path.name)
        if evidence is MacProcessEvidence.ENDED:
            ended.append(path)
            continue
        record = read_mac_gate_record(path)
        if isinstance(evidence, MacProcessEvidence) or not isinstance(record, MacGateRecord):
            raise RootRefusal("cannot verify an existing build root")
        if record.label == label:
            raise RootRefusal("placement already has a live build root")
    for path in state.glob(f"{JOBSERVER_PREFIX}*"):
        evidence = owner_evidence(path.name.removeprefix(JOBSERVER_PREFIX))
        if evidence is MacProcessEvidence.UNAVAILABLE:
            raise RootRefusal("cannot verify an existing jobserver owner")
        if evidence is MacProcessEvidence.ENDED:
            ended.append(path)
    for path in ended:
        path.unlink(missing_ok=True)


def register(pid: int, ancestor: str, label: str) -> None:
    state = mac_gate_state()
    if not state.is_absolute() or any(character in str(state) for character in "\t\r\n"):
        raise RootRefusal("cannot use the Mac account state directory")
    records = state / "records"
    records.mkdir(parents=True, exist_ok=True)
    process = observe_mac_process(pid)
    if not isinstance(process, LiveMacProcess):
        raise RootRefusal("cannot verify this build root")
    if OWNER_NAME.fullmatch(ancestor):
        root = read_mac_gate_record(records / ancestor)
        if isinstance(root, MacGateRecord) and root.label == label and inherits_root(process, root):
            print(state, ancestor, "nested", sep="\t")
            return
    own = MacGateRecord(process.identity, label)
    path = records / own.identity.key
    # Publish before checking other records. Two roots for one placement cannot both pass.
    stream = path.open("x")
    try:
        with stream:
            _ = stream.write(own.line())
        if label != "user":
            check_existing_roots(state, path, label)
    except (OSError, RootRefusal):
        path.unlink(missing_ok=True)
        raise
    print(state, own.identity.key, "root", sep="\t")


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
