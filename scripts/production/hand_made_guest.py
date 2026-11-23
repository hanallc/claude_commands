"""The Mac Linux guest route of hand_made.py.

The guest runs the argv in its numbered copy of one commit of the checkout, with the dirty files. The run
holds the guest run lock, and the place ends only after the guest proves that the run ended.
"""
from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Unpack

from scripts.build_queue.admission_jobs import HAND_MADE
from scripts.build_queue.queue_place import PopenArguments, QueuePlaceRefused
from scripts.build_targets import mac_linux
from scripts.lib import build_folders

MACHINE = mac_linux.TARGET


@dataclass(frozen=True)
class GuestPlace:
    """A guest run that a hand made attempt holds. The guest has no memory sample, so peak_bytes is 0."""
    sidecar: Path
    repo: Path
    unit: str
    checked: Mapping[Path, bytes]
    command: mac_linux.GuestCommand
    peak_bytes: int = 0

    def popen(self, argv: Sequence[str], **popen_arguments: Unpack[PopenArguments]) -> subprocess.Popen[bytes]:
        """Send the checkout and start the argv in the guest.

        The guest finds the first word in its numbered copy or on its own PATH, and refuses a word that can
        start a guest file that the check did not read. It gives the program its own variables and no input,
        so cwd, env and stdin do not apply. The output is always one pipe.
        """
        del popen_arguments
        try:
            return self.command.start(self.repo, argv, work=HAND_MADE.step, unit=self.unit, bootstrap=self.sidecar,
                                      checked=self.checked)
        except mac_linux.route_errors() as error:
            raise QueuePlaceRefused(f"{MACHINE}: guest start: {error}") from error


def run_owner(repo: Path) -> str:
    """The unit that owns a run of the checkout. A repository other than claude, or a run with no unit, is refused."""
    from scripts.build_queue.admission_client import step_identity

    try:
        mac_linux.check_repository(repo)
        unit = step_identity(HAND_MADE.step, str(repo)).unit
    except mac_linux.route_errors() as error:
        raise QueuePlaceRefused(f"{MACHINE}: {error}") from error
    if not unit:
        raise QueuePlaceRefused(f"{MACHINE}: the run has no owner unit for a numbered copy")
    return unit


@contextmanager
def mac_claim(repo: Path) -> Generator[None]:
    """Hold the Mac repository claim of the guest for the run; a refused claim takes no run."""
    from scripts.build_queue.mac_run import MacRunRefused

    with ExitStack() as stack:
        try:
            stack.enter_context(mac_linux.mac_linux_reservation(repo, HAND_MADE, HAND_MADE))
        except MacRunRefused as error:
            raise QueuePlaceRefused(f"{MACHINE}: not started: {error}") from error
        yield


@contextmanager
def guest_place(repo: Path, checked: Mapping[Path, bytes]) -> Generator[GuestPlace]:
    """Hold the guest for one run. A guest that is off or in retry backoff takes no run.

    checked holds the bytes of each program file that the check read; the guest commit must hold them.
    """
    try:
        settings = mac_linux.new_run_settings()
    except mac_linux.route_errors() as error:
        raise QueuePlaceRefused(f"{MACHINE}: {error}") from error
    if isinstance(settings, mac_linux.GuestNotStarted):
        raise QueuePlaceRefused(settings.reason)
    if isinstance(settings, str):
        raise QueuePlaceRefused(settings)
    unit = run_owner(repo)
    with (mac_claim(repo), build_folders.tool_folder(HAND_MADE.step) as folder):
        command = mac_linux.GuestCommand(settings)
        failure: BaseException | None = None
        try:
            yield GuestPlace(folder / "bootstrap.tar", repo, unit, checked, command)
        except BaseException as error:
            failure = error
            raise
        finally:
            try:
                command.finish(failure)
            except mac_linux.route_errors() as error:
                raise QueuePlaceRefused(f"{MACHINE}: {error}") from error
