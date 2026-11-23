"""The Mac Linux guest route of hand_made.py.

The queue gives the run to the guest by its mac-linux line. The guest runs the argv in its numbered copy of
one commit of the checkout, with the dirty files. The run holds the Mac repository claim and the guest run
lock, and the place ends only after the guest proves that the run ended.
"""
from __future__ import annotations

from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import time

from scripts.build_queue import admission_protocol as wire
from scripts.build_queue.admission_jobs import HAND_MADE
from scripts.build_queue.placement import MAC_LINUX_NO_ANSWER
from scripts.build_targets import mac_linux
from scripts.production.hand_made_route import (Line, MachineFailed, MachineRun, Output, Placed, Program, Refused,
                                                in_checkout, note, start_unknown, stream)

MACHINE = mac_linux.TARGET
# The queue ends a place when its holder ends, so a place with live guest work stays until then.
STILL_LIVE = "the guest run may still be live; the queue ends the place when this run ends"


class GuestFailed(MachineFailed):
    """The guest took no run; the queue reads the guest no answer reason and waits before it gives the guest a run."""

    def __init__(self, text: str) -> None:
        super().__init__(text, MAC_LINUX_NO_ANSWER)


def linked_path(path: Path, repo: Path) -> bool:
    """A path in the checkout with .. or a symbolic link in it, from the checkout down."""
    relative = path.relative_to(repo)
    if ".." in relative.parts:
        return True
    return any((repo / Path(*relative.parts[:end])).is_symlink() for end in range(1, len(relative.parts) + 1))


def run_owner(repo: Path) -> str:
    """The unit that owns a run of the checkout. A repository other than claude, or a run with no unit, is refused."""
    from scripts.build_queue.admission_client import step_identity

    try:
        mac_linux.check_repository(repo)
        unit = step_identity(HAND_MADE.step, str(repo)).unit
    except mac_linux.route_errors() as error:
        raise GuestFailed(f"{MACHINE}: {error}") from error
    if not unit:
        raise GuestFailed(f"{MACHINE}: the run has no owner unit for a numbered copy")
    return unit


def guest_refusal(program: Program) -> str:
    """The reason that the guest takes no run of the command, before the model call; empty for none.

    The guest runs the command in its own copy of one commit of the checkout. Each program file must be a
    file of the checkout that the copy holds at the same path. A guest that is off takes no run; a guest in
    retry backoff is no refusal, and the queue line waits for the backoff end.
    """
    try:
        settings = mac_linux.new_run_settings()
    except mac_linux.route_errors() as error:
        return f"{MACHINE}: {error}"
    if isinstance(settings, str):
        return settings
    absolute = [word for word in program.argv if os.path.isabs(word)]
    if absolute:
        return (f"{MACHINE} takes no absolute path in the command, because the path names a file of the guest: "
                + f"{', '.join(absolute)}; give a path in the checkout or a program name")
    paths = [path for path, _ in program.checked]
    outside = [str(path) for path in paths if not in_checkout(path, program.repo)]
    if outside:
        return (f"{MACHINE} runs its own copy of each program file outside the checkout, and the check reads only "
                + f"the natedev copy: {', '.join(outside)}; use a file in the checkout")
    linked = [str(path) for path in paths if linked_path(path, program.repo)]
    if linked:
        return (f"{MACHINE} takes no program file with a symbolic link or .. in its path in the checkout, because the "
                + f"guest copy can hold another file there: {', '.join(linked)}; give the real path")
    try:
        _ = run_owner(program.repo)
    except MachineFailed as error:
        return str(error)
    return ""


def run_settings() -> mac_linux.GuestSettings:
    """The guest settings for a new run. A guest that is off or in retry backoff takes no run."""
    try:
        settings = mac_linux.new_run_settings()
    except mac_linux.route_errors() as error:
        raise GuestFailed(f"{MACHINE}: {error}") from error
    if isinstance(settings, mac_linux.GuestNotStarted):
        raise GuestFailed(settings.reason)
    if isinstance(settings, str):
        raise GuestFailed(settings)
    return settings


@contextmanager
def mac_claim(repo: Path) -> Generator[None]:
    """Hold the Mac repository claim of the guest for the run; a refused claim takes no run."""
    from scripts.build_queue.mac_run import MacRunRefused

    with ExitStack() as stack:
        try:
            stack.enter_context(mac_linux.mac_linux_reservation(repo, HAND_MADE, HAND_MADE))
        except MacRunRefused as error:
            raise GuestFailed(f"not started: {error}") from error
        yield


@dataclass
class GuestRun:
    """One run of the argv in the guest, with the bootstrap file in the folder of the attempt.

    The guest has no memory sample, so peak_bytes is None.
    """
    command: mac_linux.GuestCommand
    program: Program
    unit: str
    bootstrap: Path
    status: int | None = None
    started_at: float | None = None

    @property
    def peak_bytes(self) -> int | None:
        return None

    def checked_bytes(self) -> dict[Path, bytes]:
        """The bytes of each checked file; the guest commit must hold them. A file that changed takes no run."""
        found: dict[Path, bytes] = {}
        for path, sha256 in self.program.checked:
            try:
                data = path.read_bytes()
            except OSError as error:
                raise GuestFailed(f"guest start: {path} cannot be read: {error.strerror or error}") from error
            if hashlib.sha256(data).hexdigest() != sha256:
                raise GuestFailed(f"guest start: {path} changed after the check")
            found[path] = data
        return found

    def run(self, output: Output) -> int:
        """Send the checkout and run the argv in the guest.

        The guest finds the first word in its numbered copy or on its own PATH, and refuses a word that can
        start a guest file that the check did not read. It gives the program its own variables and no input.
        The output is one pipe.
        """
        try:
            process = self.command.start(self.program.repo, self.program.argv, work=HAND_MADE.step, unit=self.unit,
                                         bootstrap=self.bootstrap, checked=self.checked_bytes())
        except mac_linux.route_errors() as error:
            proven = self.command.process is None or isinstance(
                error, (mac_linux.GuestNotLocked, mac_linux.GuestRefusedCommand))
            if not proven:
                raise start_unknown(MACHINE, f"the guest got the command and gave no start line: {error}") from error
            raise GuestFailed(f"guest start: {error}") from error
        self.started_at = time.time()
        with process:
            self.status = stream(process, output)
        return self.status

    def unproven(self, failure: BaseException | None) -> str:
        """Prove the end of the guest run; the cause when the guest gives no proof, or no text."""
        try:
            self.command.finish(failure)
        except mac_linux.route_errors() as error:
            return f"{MACHINE}: {error}"
        return ""


@contextmanager
def guest_route(line: Line, placed: Placed, program: Program) -> Generator[MachineRun]:
    """Hold the guest for one run. A guest that takes no run before the program starts raises MachineFailed."""
    if not isinstance(placed, wire.Assignment):
        raise MachineFailed("the queue gave the guest a local grant")
    settings = run_settings()
    unit = run_owner(program.repo)
    with mac_claim(program.repo):
        guest = GuestRun(mac_linux.GuestCommand(settings), program, unit, line.folder / "bootstrap.tar")
        try:
            yield guest
        except MachineFailed as failure:
            unproven = guest.unproven(failure)
            if unproven:
                raise Refused(unproven) from failure
            raise
        except BaseException as error:
            unproven = guest.unproven(error)
            if unproven:
                note(f"{unproven}; {STILL_LIVE}")
            else:
                line.close(placed, None, f"the guest run stopped: {error or type(error).__name__}")
            raise
        unproven = guest.unproven(None)
        if unproven:
            note(f"{unproven}; {STILL_LIVE}")
            return
        line.close(placed, guest.status, "")
