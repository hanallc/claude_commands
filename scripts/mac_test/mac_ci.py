"""Read whether a GitHub Actions worker occupies Mac build capacity."""

from pathlib import Path
from subprocess import DEVNULL, PIPE, TimeoutExpired, run

from scripts.lib import conf
from scripts.mac_test.claim_state import MacCiActivity


PROCESS_LIST_COMMAND = "LC_ALL=C ps -axww -o pid=,stat=,ucomm="


def is_runner_worker(executable: str) -> bool:
    """Match the exact executable name of a GitHub Actions job worker."""
    return Path(executable).name == "Runner.Worker"


def read_mac_ci_activity(configuration: conf.ConfigRead) -> MacCiActivity:
    """Read the live process list with the configured SSH probe timeout."""
    from scripts.mac_test import eligibility, offload

    settings = eligibility.SshProbeSettings(configuration)
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
        _, state, executable = fields
        # ps reports Z for zombies and ? when it cannot read task information.
        if not state.startswith("Z") and is_runner_worker(executable):
            activity = MacCiActivity.RUNNING
    return activity
