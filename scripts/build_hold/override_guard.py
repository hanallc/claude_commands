"""Refuse state-directory changes when an installed script starts."""

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as package_root:
        exec(package_root.read(), {"__file__": package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache, lru_cache
import os
from pathlib import Path
import pwd
import sys

from scripts.build_hold import shared_state_defaults
from scripts.buildlog import store


OVERRIDE_VARIABLES: tuple[str, ...] = (
    "ADMISSION_SOCKET",
    "CONF_DIR",
    "BUILD_HOLD_DIR",
    "BUILD_HOLD_RELEASE_DIR",
    "MAC_TEST_STATE_DIR",
    "MAC_TEST_CONFIG",
    "BUILD_TARGETS_SCRIPT",
    "BUILD_TARGETS_CONFIG",
    "BUILD_TARGETS_NOW",
    # The same folder under its name before the rename, which unit_runs.environ still reads.
)
REFUSED_STATUS = 2


@lru_cache(maxsize=1)
def user_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


@dataclass(frozen=True)
class OverridesAllowed:
    """The caller can use configuration overrides."""


@dataclass(frozen=True)
class OverridesRefused:
    """The refusal line explains why the caller cannot use configuration overrides."""
    line: str


def is_installed_file(script: Path, relative: str, home: Path) -> bool:
    try:
        return os.path.samefile(script, home / ".claude" / "scripts" / relative)
    except OSError:
        return False


@dataclass(frozen=True)
class SharedStateAllowed:
    """The caller may change the checked directories."""


@dataclass(frozen=True)
class SharedStateRefused:
    """The caller must use the installed entry point for shared state."""
    line: str


class SharedStateWriteRefused(RuntimeError):
    """A checkout copy attempted to change shared build state."""

    def __init__(self, refusal: SharedStateRefused) -> None:
        self.refusal: SharedStateRefused = refusal
        super().__init__(refusal.line)


@cache
def _shared_state_roots(home: Path) -> tuple[Path, ...]:
    return tuple(path.resolve() for path in (
        home / Path(store.DEFAULT_ROOT_DISPLAY).relative_to("~"),
        home / shared_state_defaults.DEFAULT_HOLDER_DIRECTORY, home / shared_state_defaults.DEFAULT_RELEASE_DIRECTORY,
        home / shared_state_defaults.DEFAULT_ENFORCER_DIRECTORY, shared_state_defaults.DEFAULT_PROC_ROOT,
        shared_state_defaults.DEFAULT_ADMISSION_SOCKET.parent,
    ))


def shared_state_access(
    script: Path, relative: str, directories: Sequence[Path],
) -> SharedStateAllowed | SharedStateRefused:
    """Allow private state from any copy and shared state from the installed copy."""
    home = user_home()
    shared = _shared_state_roots(home)
    for directory in directories:
        actual = directory.resolve()
        if any(actual.is_relative_to(root) for root in shared):
            if not is_installed_file(script, relative, home):
                installed = home / ".claude/scripts" / relative
                return SharedStateRefused(f"{script.name}: shared build state at {actual} requires {installed}")
            break
    return SharedStateAllowed()


def require_shared_state_write(script: Path, relative: str, *directories: Path) -> None:
    result = shared_state_access(script, relative, directories)
    if isinstance(result, SharedStateRefused):
        raise SharedStateWriteRefused(result)


def require_checkout(script: Path) -> None:
    home = user_home()
    installed_root = home / ".claude" / "scripts"
    try:
        relative = script.resolve().relative_to(installed_root.resolve())
    except ValueError:
        return
    if is_installed_file(script, str(relative), home):
        raise RuntimeError(
            f"{script.name}: this suite runs from a checkout, not from the installed scripts under "
            + f"{installed_root}; run it from a worktree."
        )


def refusal(
    script: Path, relative: str, home: Path, environment: Mapping[str, str]
) -> OverridesAllowed | OverridesRefused:
    for name in OVERRIDE_VARIABLES:
        if name in environment:
            break
    else:
        if "HOME" not in environment or environment["HOME"] == str(home):
            return OverridesAllowed()
        try:
            if os.path.samefile(environment["HOME"], home):
                return OverridesAllowed()
        except OSError:
            pass
        name = "HOME"

    installed_root = home / ".claude" / "scripts"
    if not is_installed_file(script, relative, home):
        return OverridesAllowed()
    return OverridesRefused(
        f"{script.name}: {name} is set to '{environment[name]}'; "
        + f"the installed scripts under {installed_root} do not run with it. "
        + "Unset it and run again."
    )


def refuse_overrides(script: Path, relative: str) -> None:
    # With nothing set, a call took 1.3 microseconds on natedev.
    result = refusal(script, relative, user_home(), os.environ)
    if isinstance(result, OverridesRefused):
        print(result.line, file=sys.stderr)
        raise SystemExit(REFUSED_STATUS)
