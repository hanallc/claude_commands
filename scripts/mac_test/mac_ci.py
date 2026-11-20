"""Read whether a GitHub Actions worker occupies Mac build capacity."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from subprocess import DEVNULL, PIPE, TimeoutExpired, run
from threading import Lock
from time import time
from typing import TYPE_CHECKING

from scripts.lib import conf
from scripts.mac_test.claim_state import MacCiActivity, MacCiReading, NO_MAC_CI_READING, NoMacCiReading

if TYPE_CHECKING:
    from scripts.mac_test import eligibility


PROCESS_LIST_COMMAND = "LC_ALL=C ps -axww -o pid=,stat=,ucomm="


@dataclass
class MacCiHostReading:
    """Serialize refreshes of the CI reading for one SSH host."""

    reading: MacCiReading | NoMacCiReading = NO_MAC_CI_READING
    lock: Lock = field(default_factory=Lock)


class MacCiReadings:
    """Share one CI reading per configured Mac SSH host among the target workers."""

    def __init__(self, now: Callable[[], float]) -> None:
        self._now: Callable[[], float] = now
        self._hosts: dict[str, MacCiHostReading] = {}
        self._lock: Lock = Lock()

    def read(self, configuration: conf.ConfigRead, max_age: float) -> MacCiReading:
        from scripts.mac_test import eligibility

        settings = eligibility.SshProbeSettings(configuration)
        try:
            host = settings.host
        except conf.Unusable:
            return MacCiReading(MacCiActivity.UNKNOWN, self._now())
        with self._lock:
            entry = self._hosts.get(host)
            if entry is None:
                entry = MacCiHostReading()
                self._hosts[host] = entry
        with entry.lock:
            reading = entry.reading
            if isinstance(reading, MacCiReading) and reading.is_fresh(self._now(), max_age):
                return reading
            entry.reading = _read_mac_ci_activity(settings, self._now)
            return entry.reading


def is_runner_worker(name: str) -> bool:
    """Match the exact process name of a GitHub Actions job worker."""
    return name == "Runner.Worker"


def read_mac_ci_activity(configuration: conf.ConfigRead) -> MacCiReading:
    """Read CI activity for a standalone capacity check."""
    from scripts.mac_test import eligibility

    return _read_mac_ci_activity(eligibility.SshProbeSettings(configuration), time)


def _read_mac_ci_activity(settings: eligibility.SshProbeSettings, now: Callable[[], float]) -> MacCiReading:
    observed_at = now()
    return MacCiReading(_read_mac_ci_processes(settings), observed_at)


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
