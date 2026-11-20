"""Read whether a GitHub Actions worker occupies Mac build capacity."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from subprocess import DEVNULL, PIPE, TimeoutExpired, run
from threading import Lock, TIMEOUT_MAX
from time import monotonic, time
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
        started_at = monotonic()
        from scripts.mac_test import eligibility

        settings = eligibility.SshProbeSettings(configuration)
        try:
            host = settings.host
            deadline = started_at + settings.probe_timeout_s
        except conf.Unusable:
            return MacCiReading(MacCiActivity.UNKNOWN, self._now())
        if not _acquire_before(self._lock, deadline):
            return MacCiReading(MacCiActivity.UNKNOWN, self._now())
        try:
            entry = self._hosts.get(host)
            if entry is None:
                entry = MacCiHostReading()
                self._hosts[host] = entry
        finally:
            self._lock.release()
        if not _acquire_before(entry.lock, deadline):
            return MacCiReading(MacCiActivity.UNKNOWN, self._now())
        try:
            reading = entry.reading
            if isinstance(reading, MacCiReading) and reading.is_fresh(self._now(), max_age):
                return reading
            if monotonic() >= deadline:
                return MacCiReading(MacCiActivity.UNKNOWN, self._now())
            entry.reading = _read_mac_ci_activity(settings, self._now, lambda: deadline - monotonic())
            return entry.reading
        finally:
            entry.lock.release()


def _acquire_before(lock: Lock, deadline: float) -> bool:
    remaining = deadline - monotonic()
    return remaining > 0 and lock.acquire(timeout=min(remaining, TIMEOUT_MAX))


def is_runner_worker(name: str) -> bool:
    """Match the exact process name of a GitHub Actions job worker."""
    return name == "Runner.Worker"


def read_mac_ci_activity(configuration: conf.ConfigRead) -> MacCiReading:
    """Read CI activity for a standalone capacity check."""
    from scripts.mac_test import eligibility

    settings = eligibility.SshProbeSettings(configuration)
    return _read_mac_ci_activity(settings, time, lambda: settings.probe_timeout_s)


def _read_mac_ci_activity(
    settings: eligibility.SshProbeSettings, now: Callable[[], float], ssh_timeout: Callable[[], float],
) -> MacCiReading:
    observed_at = now()
    return MacCiReading(_read_mac_ci_processes(settings, ssh_timeout), observed_at)


def _read_mac_ci_processes(settings: eligibility.SshProbeSettings, ssh_timeout: Callable[[], float]) -> MacCiActivity:
    from scripts.mac_test import offload

    try:
        command = [*offload.ssh_prefix(settings), PROCESS_LIST_COMMAND]
        timeout = ssh_timeout()
        if timeout <= 0:
            return MacCiActivity.UNKNOWN
        result = run(command, stdin=DEVNULL, stdout=PIPE, stderr=PIPE, text=True, check=False, timeout=timeout)
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
