"""Copy selected helper folders into a private fixture home."""

from __future__ import annotations

from functools import cache
from pathlib import Path
import os
import shutil
from time import perf_counter
from typing import NamedTuple

from scripts.lib.checkout_test_support import runtime_files


SOURCE = Path(__file__).resolve().parents[2]
COPY_HELPERS = (
    "scripts/lib", "scripts/buildlog", "scripts/copies", "scripts/mac_test", "scripts/message",
    "scripts/production", "scripts/agents", "scripts/direct", "scripts/hooks", "scripts/lint", "scripts/herdr",
    "scripts/build_hold", "scripts/build_queue", "scripts/build_targets", "scripts/test_times", "scripts/aws_target",
)
VERIFY_HELPERS = (*COPY_HELPERS, "scripts/whoami", "scripts/notify")
VERIFY_TEST_ENVIRONMENT = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "TZ", "TMPDIR", "TERM",
    # Keep the test runner's Python process settings in fixture subprocesses.
    "PYTHON_CPU_COUNT", "PYTHONDONTWRITEBYTECODE",
})


def verify_test_environment() -> dict[str, str]:
    """Read the allowed caller settings and set the fixture locale."""
    environment = {name: value for name, value in os.environ.items() if name in VERIFY_TEST_ENVIRONMENT}
    environment["LANG"] = environment["LC_ALL"] = "C"
    return environment


class CopyTime(NamedTuple):
    seconds: float
    files: int
    bytes: int


COPY_TIMES: list[CopyTime] = []


@cache
def helper_files(folders: tuple[str, ...]) -> tuple[Path, ...]:
    """Use the shared runtime catalog for the selected folders."""
    selected = tuple(Path(name) for name in ("scripts/lib", *folders))
    return tuple(relative for relative in runtime_files()
                 if any(relative.is_relative_to(folder) for folder in selected))


def copy_helpers(home: Path, *folders: str) -> Path:
    """Copy helper folders without replacing the fixture's existing stubs."""
    began = perf_counter()
    installed = home / ".claude"
    installed.mkdir(parents=True, exist_ok=True)
    count = size = 0
    files = helper_files(tuple(folders))
    selected_files = frozenset(files)
    for relative in files:
        destination = installed / relative
        if destination.is_symlink():
            target = Path(os.readlink(destination))
            if target.is_absolute() or not destination.resolve().is_relative_to(installed):
                raise ValueError(f"The fixture helper path leaves the copied tree: {destination}")
            continue
        for parent in destination.parents:
            if parent == home:
                break
            if parent.is_symlink():
                raise ValueError(f"The fixture helper path is a link: {parent}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            source = SOURCE / relative
            target = Path(os.readlink(source)) if source.is_symlink() else None
            resolved = source.resolve()
            inside = resolved.is_relative_to(SOURCE) and resolved.relative_to(SOURCE) in selected_files
            if target is not None and resolved.is_dir():
                if (target.is_absolute() or not resolved.is_relative_to(SOURCE)
                        or not any(member.is_relative_to(resolved.relative_to(SOURCE)) for member in files)):
                    raise ValueError(f"The fixture helper directory has no private copy: {source}")
                copied_directory = installed / resolved.relative_to(SOURCE)
                destination.symlink_to(os.path.relpath(copied_directory, destination.parent), target_is_directory=True)
            elif target is not None and not target.is_absolute() and inside:
                destination.symlink_to(target)
            else:
                _ = shutil.copy2(source, destination)
            count += 1
            size += destination.lstat().st_size
    COPY_TIMES.append(CopyTime(perf_counter() - began, count, size))
    return installed / "scripts"

