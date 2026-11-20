"""Share the Mac gate owner identity with the Builds page."""

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import pwd
import re
import sys


PLACEMENT_FORM = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
PLACEMENT_PREFIX = "placement "


@dataclass(frozen=True)
class MacProcessIdentity:
    pid: int
    started_us: int

    @property
    def key(self) -> str:
        return f"{self.pid}-{self.started_us}"


@dataclass(frozen=True)
class LiveMacProcess:
    identity: MacProcessIdentity
    parent_pid: int


class MacProcessEvidence(Enum):
    ENDED = "the process ended"
    UNAVAILABLE = "the process identity is unavailable"


def pid_has_ended(pid: int) -> bool:
    """Use the kernel existence check to confirm that a PID is absent."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (OSError, OverflowError):
        pass
    return False


def observe_mac_process(pid: int) -> LiveMacProcess | MacProcessEvidence:
    """Report exit only when the kernel confirms it."""
    if sys.platform != "darwin":
        return MacProcessEvidence.UNAVAILABLE
    try:
        from scripts.lib import darwin_process
    except ImportError:
        return MacProcessEvidence.UNAVAILABLE
    try:
        info = darwin_process.read_process_info(pid, include_zombies=True)
    except darwin_process.DarwinProcessInfoUnavailable:
        return MacProcessEvidence.ENDED if pid_has_ended(pid) else MacProcessEvidence.UNAVAILABLE
    if info.status == darwin_process.SZOMB:
        return MacProcessEvidence.ENDED
    return LiveMacProcess(MacProcessIdentity(info.pid, info.started_us), info.parent_pid)


def mac_gate_account_home() -> Path:
    return Path(pwd.getpwuid(os.geteuid()).pw_dir)


def mac_gate_state() -> Path:
    return mac_gate_account_home() / ".local/state/mac-gate"


@dataclass(frozen=True)
class MacGateRecord:
    identity: MacProcessIdentity
    label: str

    def line(self) -> str:
        return f"{self.identity.pid}\t{self.identity.started_us}\t{self.label}\n"


class MacGateRecordRead(Enum):
    UNAVAILABLE = "the root record is unavailable"


def read_mac_gate_record(path: Path) -> MacGateRecord | MacGateRecordRead:
    try:
        if not path.is_file() or path.is_symlink():
            return MacGateRecordRead.UNAVAILABLE
        line = path.read_text()
        pid, started, label = line.removesuffix("\n").split("\t")
        identity = MacProcessIdentity(int(pid), int(started))
    except (OSError, ValueError):
        return MacGateRecordRead.UNAVAILABLE
    record = MacGateRecord(identity, label)
    placement = label.removeprefix(PLACEMENT_PREFIX)
    if (identity.pid > 0 and identity.started_us > 0 and path.name == identity.key and line == record.line()
            and (label == "user" or (placement != label and PLACEMENT_FORM.fullmatch(placement)))):
        return record
    return MacGateRecordRead.UNAVAILABLE
