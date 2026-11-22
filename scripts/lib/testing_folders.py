"""Give test runs maker folders and recover free folders before the next run."""
from __future__ import annotations

import atexit
from collections.abc import Generator
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile

from scripts.lib import build_folders
from scripts.lib.session_folder import ROOT_ENV as SESSION_ROOT_ENV


RUN_FOLDER_ENV = build_folders.TEST_RUN_FOLDER_ENV
BUILDLOG_ENV = "BUILDLOG_DIR"


class SelectParentFromCheckout:
    """Select the parent when the test starts."""


_prepared_parent: Path | SelectParentFromCheckout = SelectParentFromCheckout()


def prepare_test_runs_parent(checkout: Path) -> Path:
    """Prepare the selected checkout's parent for private test folders."""
    parent = build_folders.test_runs_parent(checkout)
    parent.mkdir(parents=True, exist_ok=True)
    build_folders.sweep_temporary_build_folders(parent, wait=False)
    return parent


def use_test_runs_parent(parent: Path) -> None:
    """Use the timed runner's prepared parent at the unittest boundary."""
    global _prepared_parent
    _prepared_parent = parent


@contextmanager
def test_run_folder() -> Generator[Path]:
    """Give a test command a private folder in its checkout's output."""
    parent = prepare_test_runs_parent(Path(__file__).resolve().parents[2])
    with _test_run_folder(parent) as folder:
        yield folder


@contextmanager
def _test_run_folder(parent: Path) -> Generator[Path]:
    """Set TMPDIR for one run and restore the caller's temporary parent afterward.

    A run with no BUILDLOG_DIR keeps its build log in the run folder, so no test writes the caller's log.
    The session root is an absent folder in the run folder, so no fixture launch makes a live session folder.
    """
    previous = {name: os.environ.get(name) for name in ("TMPDIR", RUN_FOLDER_ENV, BUILDLOG_ENV, SESSION_ROOT_ENV)}
    old_tempdir = tempfile.tempdir
    request = build_folders.UniqueBuildFolder("r", build_folders.ExplicitBuildParent(parent))
    with build_folders.temporary_build_folder(request) as path:
        os.environ["TMPDIR"] = str(path)
        os.environ[RUN_FOLDER_ENV] = str(path)
        if not previous[BUILDLOG_ENV]:
            os.environ[BUILDLOG_ENV] = str(path / "buildlog")
        os.environ[SESSION_ROOT_ENV] = str(path / "session")
        tempfile.tempdir = None
        try:
            yield path
        finally:
            for name, value in previous.items():
                if value is None:
                    _ = os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            tempfile.tempdir = old_tempdir


def start_test_run() -> None:
    """Keep the bootstrap allocation until normal interpreter exit."""
    owner = os.getpid()
    parent = _prepared_parent
    lifetime = _test_run_folder(parent) if isinstance(parent, Path) else test_run_folder()
    _ = lifetime.__enter__()

    def finish() -> None:
        if os.getpid() == owner:
            _ = lifetime.__exit__(None, None, None)

    _ = atexit.register(finish)
