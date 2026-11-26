"""Give test runs maker folders and recover free folders before the next run."""
from __future__ import annotations

import atexit
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
import os
from pathlib import Path
import pwd
import sys
import tempfile
from typing import NewType

from scripts.lib import build_folders
from scripts.lib.live_config_guard import LiveConfigWrite, guard_live_config
from scripts.lib.session_folder import ROOT_ENV as SESSION_ROOT_ENV


RUN_FOLDER_ENV = build_folders.TEST_RUN_FOLDER_ENV
BUILDLOG_ENV = "BUILDLOG_DIR"


class SelectParentFromCheckout:
    """Select the parent when the test starts."""


# Create this value after the folder preparation and sweep.
PreparedTestRunsParent = NewType("PreparedTestRunsParent", Path)
_prepared_parent: PreparedTestRunsParent | SelectParentFromCheckout = SelectParentFromCheckout()


def prepare_test_runs_parent(checkout: Path) -> PreparedTestRunsParent:
    """Prepare the selected checkout's parent for private test folders."""
    parent = build_folders.test_runs_parent(checkout)
    parent.mkdir(parents=True, exist_ok=True)
    build_folders.sweep_temporary_build_folders(parent, wait=False)
    return PreparedTestRunsParent(parent)


def use_test_runs_parent(parent: PreparedTestRunsParent) -> None:
    """Use the timed runner's prepared parent at the unittest boundary."""
    global _prepared_parent
    _prepared_parent = parent


@contextmanager
def test_run_folder() -> Generator[Path]:
    """Give a test command a private folder in its checkout's output."""
    with _test_run_folder(prepare_test_runs_parent(Path(__file__).resolve().parents[2])) as folder:
        yield folder


@contextmanager
def _test_run_folder(parent: PreparedTestRunsParent) -> Generator[Path]:
    """Set TMPDIR for one run and restore the caller's temporary parent afterward.

    The session root is an absent folder in the run folder, so no fixture launch makes a live session folder.
    """
    previous = {name: os.environ.get(name) for name in ("TMPDIR", RUN_FOLDER_ENV, SESSION_ROOT_ENV)}
    old_tempdir = tempfile.tempdir
    request = build_folders.UniqueBuildFolder("r", build_folders.ExplicitBuildParent(parent))
    with build_folders.temporary_build_folder(request) as path:
        os.environ["TMPDIR"] = str(path)
        os.environ[RUN_FOLDER_ENV] = str(path)
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
    """Keep the bootstrap allocation until normal interpreter exit.

    A direct run with no BUILDLOG_DIR keeps its build log in the run folder, so no test writes the caller's log.
    """
    owner = os.getpid()
    caller_log = os.environ.get(BUILDLOG_ENV)
    parent = _prepared_parent
    lifetime = test_run_folder() if isinstance(parent, SelectParentFromCheckout) else _test_run_folder(parent)
    with ExitStack() as scope:
        path = scope.enter_context(lifetime)
        # HOME can change during a test. Protect the login account's config.
        live_config = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".claude/config"
        scope.enter_context(guard_live_config(live_config, path, path / "live-config-writes"))
        cleanup = scope.pop_all()
    if not caller_log:
        os.environ[BUILDLOG_ENV] = str(path / "buildlog")

    def finish() -> None:
        if os.getpid() == owner:
            if caller_log is None:
                _ = os.environ.pop(BUILDLOG_ENV, None)
            else:
                os.environ[BUILDLOG_ENV] = caller_log
            try:
                cleanup.close()
            except LiveConfigWrite as error:
                print(f"direct test: {error}", file=sys.stderr)

    _ = atexit.register(finish)
