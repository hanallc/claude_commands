"""Read whether a GitHub Actions worker occupies Mac build capacity."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from subprocess import DEVNULL, PIPE, TimeoutExpired, run
from typing import TYPE_CHECKING

from scripts.lib import conf
from scripts.mac_test.claim_state import MacCiActivity

if TYPE_CHECKING:
    from scripts.mac_test import eligibility


PROCESS_LIST_COMMAND = "LC_ALL=C ps -axww -o pid=,stat=,ucomm="


@dataclass(frozen=True)
class MacSshHost:
    """The configured SSH address shared by Mac targets."""

    address: str


@dataclass(frozen=True)
class UnsharedMacCi:
    """The worker has no shared Mac CI reading."""


UNSHARED_MAC_CI = UnsharedMacCi()


@dataclass
class MacCiEvidencePass:
    """Read one host once during a pass of its evidence worker."""

    host: MacSshHost
    activity: MacCiActivity = MacCiActivity.UNCHECKED


_MAC_CI_EVIDENCE_PASS: ContextVar[MacCiEvidencePass] = ContextVar("mac_ci_evidence_pass")


@contextmanager
def evidence_pass(host: MacSshHost | UnsharedMacCi) -> Generator[None]:
    if isinstance(host, UnsharedMacCi):
        yield
        return
    token = _MAC_CI_EVIDENCE_PASS.set(MacCiEvidencePass(host))
    try:
        yield
    finally:
        _MAC_CI_EVIDENCE_PASS.reset(token)


def is_runner_worker(name: str) -> bool:
    """Match the exact process name of a GitHub Actions job worker."""
    return name == "Runner.Worker"


def read_mac_ci_activity(configuration: conf.ConfigRead) -> MacCiActivity:
    """Read CI activity for this Mac evidence pass or standalone call."""
    from scripts.mac_test import eligibility

    settings = eligibility.SshProbeSettings(configuration)
    current = _MAC_CI_EVIDENCE_PASS.get(None)
    if current is None:
        return _read_mac_ci_processes(settings)
    try:
        if settings.host != current.host.address:
            return MacCiActivity.UNKNOWN
    except conf.Unusable:
        return MacCiActivity.UNKNOWN
    if current.activity is MacCiActivity.UNCHECKED:
        current.activity = _read_mac_ci_processes(settings)
    return current.activity


def _read_mac_ci_processes(settings: eligibility.SshProbeSettings) -> MacCiActivity:
    from scripts.mac_test import offload

    try:
        result = run([*offload.ssh_prefix(settings), PROCESS_LIST_COMMAND],
                     stdin=DEVNULL, stdout=PIPE, stderr=PIPE, text=True, check=False,
                     timeout=settings.probe_timeout_s)
    except (OSError, TimeoutExpired, ValueError):
        return MacCiActivity.UNKNOWN
    if result.returncode or not result.stdout.strip():
        return MacCiActivity.UNKNOWN
    activity = MacCiActivity.IDLE
    for row in result.stdout.splitlines():
        fields = row.strip().split(maxsplit=2)
        if len(fields) != 3 or not fields[0].isascii() or not fields[0].isdecimal() or fields[1][0] not in "RUSITH?Z":
            return MacCiActivity.UNKNOWN
        _, state, name = fields
        # ps reports Z for zombies and ? when it cannot read task information.
        if not state.startswith("Z") and is_runner_worker(name):
            activity = MacCiActivity.RUNNING
    return activity
