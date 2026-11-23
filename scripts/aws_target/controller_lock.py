"""Lock the account stack and coordinate control of the Hana box."""
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from enum import Enum
import fcntl
import os
from pathlib import Path

from scripts.lib.file_lock import LockResult, exclusive


# Commands that need both locks take the account stack lock before the Hana resource hold.
@contextmanager
def account_stack_lock(directory: Path) -> Generator[LockResult]:
    """Take the account stack lock without waiting."""
    gate = directory / 'gate-up'
    gate.mkdir(parents=True, exist_ok=True)
    with exclusive(gate / 'controller.lock', wait=False) as access:
        yield access


@contextmanager
def control_lock(directory: Path, *, existing: bool = False) -> Generator[None, None, None]:
    if not existing:
        directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'controller.lock').open('r' if existing else 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def _hana_resource_hold(directory: Path) -> Generator[LockResult]:
    """Take the resource hold while the caller owns the controller lock."""
    # The directory inode lets plan mode take the hold without creating a file.
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield LockResult.BUSY
        else:
            yield LockResult.ACQUIRED
    finally:
        os.close(descriptor)


@contextmanager
def hana_work_lock(directory: Path, *, existing: bool = False) -> Generator[LockResult]:
    """Wait for controller work and check for a resource hold."""
    with control_lock(directory, existing=existing):
        # A resource command needs the controller lock to take the hold.
        with _hana_resource_hold(directory) as result:
            pass
        yield result


class HanaCommandAccess(Enum):
    ACQUIRED = 'acquired'
    CONTROLLER_MISSING = 'controller missing'
    CONTROLLER_BUSY = 'controller busy'
    RESOURCES_HELD = 'resources held'
    LIFECYCLE_BUSY = 'lifecycle busy'


@contextmanager
def hana_command_lock(directory: Path, *, ready: Callable[[], bool] = lambda: True,
                      wait_for_controller: bool = True, existing_controller: bool = False
                      ) -> Generator[HanaCommandAccess]:
    """Take exclusive control of Hana resources for one command."""
    path = directory / 'controller.lock'
    if existing_controller and not path.is_file():
        yield HanaCommandAccess.CONTROLLER_MISSING
        return
    if not existing_controller:
        directory.mkdir(parents=True, exist_ok=True)
    with ExitStack() as command:
        with exclusive(path, wait=wait_for_controller) as controller:
            if controller is LockResult.BUSY:
                access = HanaCommandAccess.CONTROLLER_BUSY
            else:
                with ExitStack() as candidate:
                    if candidate.enter_context(_hana_resource_hold(directory)) is LockResult.BUSY:
                        access = HanaCommandAccess.RESOURCES_HELD
                    elif not ready():
                        access = HanaCommandAccess.LIFECYCLE_BUSY
                    else:
                        _ = command.enter_context(candidate.pop_all())
                        access = HanaCommandAccess.ACQUIRED
        yield access
