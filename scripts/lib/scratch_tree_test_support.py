"""Scan a temporary fixture folder, and refuse a folder that shares files with a checkout.

The affected-module selection treats a call to these functions as no repository scan.
The run-time check makes that rule safe: a call with a checkout folder fails at once.
"""

from collections.abc import Iterator, Mapping
import os
from pathlib import Path
import pwd
import subprocess

from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


# The checkout that holds this module, and the live checkout. A fixture can change HOME, so read the account home.
CHECKOUTS = (Path(__file__).resolve().parents[2], Path(pwd.getpwuid(os.getuid()).pw_dir, ".claude").resolve())
GIT_REDIRECTS = frozenset({"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"})


def _refuse_checkout(root: Path, checkouts: tuple[Path, ...]) -> None:
    """Raise ValueError when the root is in a checkout, or holds one."""
    resolved = root.resolve()
    for checkout in checkouts:
        if resolved.is_relative_to(checkout) or checkout.is_relative_to(resolved):
            raise ValueError(f"The scan root {root} is not a temporary fixture folder: it overlaps the checkout {checkout}")


def _scratch_paths(root: Path, pattern: str, *, recursive: bool) -> Iterator[Path]:
    """Check the root and the pattern before the glob scans the folder."""
    checkouts = tuple(checkout.resolve() for checkout in CHECKOUTS)
    _refuse_checkout(root, checkouts)
    checked_directories = {root}
    parts = Path(pattern).parts
    if ".." in parts:
        raise ValueError(f"The scan pattern {pattern!r} contains a parent directory")
    if recursive:
        parts = ("**", *parts)
    for index, part in enumerate(parts[:-1], 1):
        # Path.glob skips directory symlinks when it expands **.
        if part != "**":
            for directory in root.glob("/".join(parts[:index]) + "/"):
                if directory not in checked_directories:
                    _refuse_checkout(directory, checkouts)
                    checked_directories.add(directory)
    paths = root.rglob(pattern) if recursive else root.glob(pattern)
    return _checked_paths(paths, checkouts, checked_directories)


def _checked_paths(paths: Iterator[Path], checkouts: tuple[Path, ...],
                   checked_directories: set[Path]) -> Iterator[Path]:
    """Check each path before returning it to the caller."""
    for path in paths:
        if path not in checked_directories:
            parent = path.parent
            if parent not in checked_directories:
                _refuse_checkout(parent, checkouts)
                checked_directories.add(parent)
            # A child of a checked directory can reach a checkout through a symlink.
            if path.is_symlink():
                _refuse_checkout(path, checkouts)
        yield path


def scratch_glob(root: Path, pattern: str) -> Iterator[Path]:
    """Give root.glob(pattern) for a temporary fixture folder."""
    return _scratch_paths(root, pattern, recursive=False)


def scratch_rglob(root: Path, pattern: str = "*") -> Iterator[Path]:
    """Give root.rglob(pattern) for a temporary fixture folder."""
    return _scratch_paths(root, pattern, recursive=True)


def scratch_ls_files(root: Path, *arguments: str,
                     environment: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run `git ls-files` in a temporary fixture repository. A failed command raises CalledProcessError."""
    _refuse_checkout(root, tuple(checkout.resolve() for checkout in CHECKOUTS))
    # These variables point git at another repository, so git would not read the root.
    redirects = sorted(set(environment if environment is not None else os.environ) & GIT_REDIRECTS)
    if redirects:
        raise ValueError(f"The git environment sets {', '.join(redirects)}, so git ls-files cannot read only the scan root {root}")
    return subprocess.run(["git", "ls-files", *arguments], cwd=root, env=environment,
                          capture_output=True, text=True, check=True, timeout=TEST_HANG_GUARD_SECS)
